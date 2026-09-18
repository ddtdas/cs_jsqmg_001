"""账号桥接服务（P9）：平台注册表 + 配置加解密 + 连通测试 + 私信接入 + agent 一键导入。

对应设计《PLAN-配置账号与私信接入.md》第三节：为前端"配置账号"页面提供后端能力——
各平台（知乎/微信/QQ/微博/抖音/Telegram/Discord）账号接入 AI 的条件、开源项目调研结论、
敏感凭据 Fernet 加密落库（复用 ZhihuBridge.CookieVault，data/secret.key 同源）、
连通探测、以及私信接入核心 ingest_dm（桥接器 webhook → detections + events → 反向侦察流水线）。

合规（D7）：只读自有账号私信；凭据加密存储、明文不落库、不回显；不自动发布（HITL）。
"""

from __future__ import annotations

import hashlib
import json
import time

from ..config import Settings, get_settings
from ..utils import ApiError
from .account_intel import AccountIntelService
from .audit_log import write_gate_log
from .zhihu_bridge import CookieVault, ZhihuBridge

# ---------------------------------------------------------------------------
# 平台注册表（静态调研结论，供前端矩阵渲染 / 校验 / config_fields 动态表单）
# ---------------------------------------------------------------------------
PLATFORMS: dict[str, dict] = {
    "zhihu": {
        "name": "知乎",
        "ai_ready": True,
        "access": "登录cookie+网页接口(playwright/httpx x-zse-96)",
        "projects": [{"name": "ZhihuBridge(自有)", "note": "CH-A/B/C/D 四通道"}],
        "config_fields": [
            {"name": "cookie", "label": "登录 Cookie", "type": "password", "secret": True},
            {"name": "from_url_name", "label": "对方知乎 url_token", "type": "text", "optional": True},
        ],
    },
    "wechat": {
        "name": "微信",
        "ai_ready": True,
        "access": "本地协议桥(WeChatFerry/wcferry)→webhook推送私信",
        "projects": [
            {"name": "WeChatFerry", "note": "个人微信协议钩子, 读好友消息"},
            {"name": "wechaty", "note": "多协议 chatbot 框架"},
        ],
        "config_fields": [
            {"name": "bridge_webhook_url", "label": "桥接 webhook 地址", "type": "url"},
            {"name": "bridge_token", "label": "桥接鉴权 token", "type": "password", "secret": True},
        ],
    },
    "qq": {
        "name": "QQ",
        "ai_ready": True,
        "access": "NTQQ OneBot 机器人→反向ws/http推送私信",
        "projects": [
            {"name": "NapCatQQ", "note": "NTQQ OneBot 实现"},
            {"name": "Lagrange.OneBot", "note": "跨平台 OneBot"},
            {"name": "go-cqhttp", "note": "经典 OneBot(维护放缓)"},
        ],
        "config_fields": [
            {"name": "onebot_ws_url", "label": "OneBot 反向 ws/http 地址", "type": "url"},
            {"name": "browser_token", "label": "OneBot access token", "type": "password", "secret": True},
        ],
    },
    "weibo": {
        "name": "微博",
        "ai_ready": True,
        "access": "登录cookie+Playwright读私信(受限)",
        "projects": [{"name": "Playwright桥(复用zhihu模式)", "note": "网页模拟"}],
        "config_fields": [
            {"name": "cookie", "label": "登录 Cookie", "type": "password", "secret": True},
        ],
    },
    "douyin": {
        "name": "抖音",
        "ai_ready": False,
        "access": "无开放私信API, 网页登录态受限(不建议)",
        "projects": [],
        "config_fields": [],
    },
    "telegram": {
        "name": "Telegram",
        "ai_ready": True,
        "access": "官方Bot API(开放)→long polling→主控",
        "projects": [
            {"name": "python-telegram-bot", "note": "官方库"},
            {"name": "Telethon", "note": "用户API"},
        ],
        "config_fields": [
            {"name": "bot_token", "label": "Bot Token", "type": "password", "secret": True},
        ],
    },
    "discord": {
        "name": "Discord",
        "ai_ready": True,
        "access": "官方Bot API(开放)→gateway→主控",
        "projects": [{"name": "discord.py", "note": "官方库"}],
        "config_fields": [
            {"name": "bot_token", "label": "Bot Token", "type": "password", "secret": True},
        ],
    },
}

# 敏感字段键名子串（save_config 命中即 Fernet 加密，明文不落库）
_SENSITIVE_HINTS = ("cookie", "token", "password", "secret")

# test() 结构校验必填字段（config_fields 中带 secret=True 的默认必填，此处显式声明可读）
_REQUIRED_FIELDS: dict[str, list[str]] = {
    "zhihu": ["cookie"],
    "wechat": ["bridge_webhook_url"],
    "qq": ["onebot_ws_url"],
    "weibo": ["cookie"],
    "douyin": [],
    "telegram": ["bot_token"],
    "discord": ["bot_token"],
}


def _is_sensitive(key: str) -> bool:
    """按键名判定敏感字段（含 cookie/token/password/secret 即加密）。"""
    low = str(key).lower()
    return any(h in low for h in _SENSITIVE_HINTS)


class AccountBridgeService:
    """账号桥接服务（settings + 请求级 DB 连接，风格对齐 ZhihuBridge）。"""

    def __init__(self, settings: Settings | None = None, conn=None,
                 vault: CookieVault | None = None) -> None:
        self.settings = settings or get_settings()
        self._conn = conn
        self.vault = vault or CookieVault(self.settings)

    # ------------------------------------------------------------------ 基础
    def _db(self):
        return self._conn

    @staticmethod
    def _require_platform(platform: str) -> dict:
        entry = PLATFORMS.get(platform)
        if entry is None:
            raise ApiError(
                "unknown_platform",
                f"未知平台 {platform!r}（可选 {', '.join(PLATFORMS)}）",
                404,
            )
        return entry

    def _row(self, platform: str):
        return self._db().execute(
            "SELECT * FROM account_bridges WHERE platform=?", (platform,)
        ).fetchone()

    def _decrypt_config(self, config_enc: str) -> dict:
        """外层 Fernet 解密 + 内层敏感字段逐个解密，返回明文配置 dict（仅内部/测试使用）。"""
        try:
            raw = self.vault.decrypt(config_enc)
            data = json.loads(raw)
        except (ValueError, json.JSONDecodeError):
            return {}
        out: dict = {}
        for k, v in data.items():
            if _is_sensitive(k) and isinstance(v, str):
                try:
                    out[k] = self.vault.decrypt(v)
                except ValueError:
                    out[k] = v  # 解密失败按原文（外层已解密，内层异常不崩）
            else:
                out[k] = v
        return out

    # ------------------------------------------------------------------ 查询
    def list_matrix(self) -> list[dict]:
        """全平台矩阵（不含 config_enc/明文）。"""
        rows = {
            r["platform"]: r
            for r in self._db().execute("SELECT * FROM account_bridges").fetchall()
        }
        out: list[dict] = []
        for pid, p in PLATFORMS.items():
            r = rows.get(pid)
            out.append({
                "platform": pid,
                "name": p["name"],
                "ai_ready": p["ai_ready"],
                "access": p["access"],
                "projects": p["projects"],
                "status": r["status"] if r else "unconfigured",
                "last_sync": r["last_sync"] if r else None,
            })
        return out

    def get_detail(self, platform: str) -> dict:
        """单平台详情（含 config_fields；不含明文，仅 has_config）。"""
        p = self._require_platform(platform)
        r = self._row(platform)
        return {
            "platform": platform,
            "name": p["name"],
            "ai_ready": p["ai_ready"],
            "access": p["access"],
            "projects": p["projects"],
            "config_fields": p["config_fields"],
            "status": r["status"] if r else "unconfigured",
            "last_sync": r["last_sync"] if r else None,
            "has_config": bool(r and (r["config_enc"] or "").strip()),
        }

    # ------------------------------------------------------------------ 配置
    def save_config(self, platform: str, fields: dict, reason: str = "配置账号桥接") -> dict:
        """保存配置：敏感字段 Fernet 双层加密落库 → status=configured → events + gate_logs 审计。

        校验：平台未知 404；ai_ready=false 422；字段名不在 config_fields 白名单 422。
        """
        p = self._require_platform(platform)
        if not p["ai_ready"]:
            raise ApiError(
                "not_configurable",
                f"{p['name']} 平台不支持账号配置（ai_ready=false，仅只读展示）",
                422,
            )
        if not isinstance(fields, dict) or not fields:
            raise ApiError("empty_fields", "fields 不能为空", 422)
        allowed = {f["name"] for f in p["config_fields"]}
        unknown = [k for k in fields if k not in allowed]
        if unknown:
            raise ApiError(
                "unknown_field",
                f"未知配置字段: {', '.join(unknown)}（允许: {', '.join(sorted(allowed))}）",
                422,
            )

        stored: dict = {}
        secrets: list[str] = []
        for k, v in fields.items():
            if _is_sensitive(k) and v:
                stored[k] = self.vault.encrypt(str(v))
                secrets.append(k)
            else:
                stored[k] = str(v) if v is not None else ""
        config_enc = self.vault.encrypt(json.dumps(stored, ensure_ascii=False))

        conn = self._db()
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        row = self._row(platform)
        before = {
            "platform": platform,
            "status": row["status"] if row else "unconfigured",
            "has_config": bool(row and (row["config_enc"] or "").strip()),
        }
        if row is None:
            conn.execute(
                "INSERT INTO account_bridges (platform, display_name, config_enc, status, last_sync, note, updated_at) "
                "VALUES (?, ?, ?, 'configured', ?, NULL, ?)",
                (platform, p["name"], config_enc, now, now),
            )
        else:
            conn.execute(
                "UPDATE account_bridges SET display_name=?, config_enc=?, status='configured', note=NULL, updated_at=? "
                "WHERE platform=?",
                (p["name"], config_enc, now, platform),
            )
        after = {
            "platform": platform,
            "status": "configured",
            "has_config": True,
            "fields": sorted(stored.keys()),
            "secrets": sorted(secrets),
        }
        write_gate_log(
            conn,
            action="account_bridge.configure",
            before=json.dumps(before, ensure_ascii=False),
            after=json.dumps(after, ensure_ascii=False),
            reason=reason or "配置账号桥接",
            payload_json=json.dumps({"platform": platform, "fields": sorted(stored.keys())}, ensure_ascii=False),
        )
        conn.execute(
            "INSERT INTO events (kind, payload) VALUES ('account_bridge_configured', ?)",
            (json.dumps({
                "platform": platform,
                "status": "configured",
                "fields": sorted(stored.keys()),
                "secrets_encrypted": len(secrets),
            }, ensure_ascii=False),),
        )
        conn.commit()
        return {
            "platform": platform,
            "status": "configured",
            "fields": sorted(stored.keys()),
            "encrypted_fields": sorted(secrets),
            "has_config": True,
            "note": "敏感字段已 Fernet 加密存储（config_enc 双层加密），明文不落库",
        }

    def delete_config(self, platform: str, reason: str = "删除账号桥接配置") -> dict:
        """清空 config_enc；status=unconfigured；写事件 + gate_logs 审计。"""
        p = self._require_platform(platform)
        conn = self._db()
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        row = self._row(platform)
        before = {
            "platform": platform,
            "status": row["status"] if row else "unconfigured",
            "has_config": bool(row and (row["config_enc"] or "").strip()),
        }
        if row is None:
            conn.execute(
                "INSERT INTO account_bridges (platform, display_name, status, updated_at) VALUES (?, ?, 'unconfigured', ?)",
                (platform, p["name"], now),
            )
        else:
            conn.execute(
                "UPDATE account_bridges SET config_enc=NULL, status='unconfigured', note=NULL, updated_at=? "
                "WHERE platform=?",
                (now, platform),
            )
        after = {"platform": platform, "status": "unconfigured", "has_config": False}
        write_gate_log(
            conn,
            action="account_bridge.delete",
            before=json.dumps(before, ensure_ascii=False),
            after=json.dumps(after, ensure_ascii=False),
            reason=reason or "删除账号桥接配置",
            payload_json=json.dumps({"platform": platform}, ensure_ascii=False),
        )
        conn.execute(
            "INSERT INTO events (kind, payload) VALUES ('account_bridge_deleted', ?)",
            (json.dumps({"platform": platform, "status": "unconfigured"}, ensure_ascii=False),),
        )
        conn.commit()
        return {"platform": platform, "status": "unconfigured", "config_cleared": True}

    # ------------------------------------------------------------------ 连通测试
    def test(self, platform: str) -> dict:
        """尽力探测：结构校验 + 有 URL/token 的 http GET 连通（timeout 5s），不实际登录。

        - 未配置 → 409 not_configured；必填字段缺失 → 422 field_required
        - 探测失败（ok=false）→ status='error'；成功 → status='configured'
        - 任何网络/解析异常都吞掉转 ok=false，绝不抛 500
        """
        p = self._require_platform(platform)
        if not p["ai_ready"]:
            raise ApiError(
                "not_configurable",
                f"{p['name']} 平台不支持账号配置（ai_ready=false），禁止测试",
                422,
            )
        row = self._row(platform)
        if row is None or not (row["config_enc"] or "").strip():
            raise ApiError("not_configured", f"{platform} 平台账号桥接未配置", 409)

        fields = self._decrypt_config(row["config_enc"])
        missing = [k for k in _REQUIRED_FIELDS.get(platform, [])
                   if not str(fields.get(k) or "").strip()]
        if missing:
            raise ApiError(
                "field_required",
                f"{platform} 缺少必填配置字段: {', '.join(missing)}",
                422,
            )

        start = time.perf_counter()
        try:
            ok, detail = self._probe(platform, fields)
        except Exception as exc:  # 兜底：探测实现缺陷也不崩
            ok, detail = False, f"探测异常: {type(exc).__name__}: {exc}"
        latency_ms = int(round((time.perf_counter() - start) * 1000))

        conn = self._db()
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "UPDATE account_bridges SET status=?, last_sync=?, note=?, updated_at=? WHERE platform=?",
            ("configured" if ok else "error", now, detail, now, platform),
        )
        conn.commit()
        return {"ok": ok, "detail": detail, "latency_ms": latency_ms}

    def _probe(self, platform: str, fields: dict) -> tuple[bool, str]:
        """按平台探测：cookie 类仅解密+解析；URL/token 类做 http GET 连通（timeout 5s）。"""
        import httpx  # 惰性：httpx 为可选依赖（缺失时按结构校验兜底）

        if platform in ("zhihu", "weibo"):
            cookie = str(fields.get("cookie") or "")
            try:
                parsed = ZhihuBridge.parse_cookie(cookie)
            except ValueError as exc:
                return False, f"cookie 结构校验失败: {exc}"
            return True, f"cookie 结构校验通过（{len(parsed)} 项），未实际登录"

        if platform == "telegram":
            token = str(fields.get("bot_token") or "")
            url = f"https://api.telegram.org/bot{token}/getMe"
            try:
                with httpx.Client(timeout=5.0) as client:
                    resp = client.get(url)
                data = resp.json() if "application/json" in resp.headers.get("content-type", "") else {}
                if resp.status_code == 200 and data.get("ok"):
                    user = (data.get("result") or {}).get("username", "?")
                    return True, f"Telegram Bot API 可达，bot 用户名 @{user}"
                desc = (data or {}).get("description") or f"HTTP {resp.status_code}"
                return False, f"Telegram Bot API 校验失败: {desc}"
            except Exception as exc:
                return False, f"Telegram 网络探测失败: {type(exc).__name__}: {exc}"

        if platform == "discord":
            token = str(fields.get("bot_token") or "")
            try:
                with httpx.Client(timeout=5.0) as client:
                    resp = client.get(
                        "https://discord.com/api/v10/users/@me",
                        headers={"Authorization": f"Bot {token}"},
                    )
                if resp.status_code == 200:
                    return True, "Discord Bot API 可达（GET /users/@me 200）"
                return False, f"Discord Bot API 可达但凭据无效（HTTP {resp.status_code}）"
            except Exception as exc:
                return False, f"Discord 网络探测失败: {type(exc).__name__}: {exc}"

        # wechat / qq：URL 连通（ws:// 端点无法用 http GET，结构有效即视为可配）
        key = "bridge_webhook_url" if platform == "wechat" else "onebot_ws_url"
        url = str(fields.get(key) or "").strip()
        if url.startswith(("ws://", "wss://")):
            return True, "WebSocket 端点结构有效（http GET 无法探测 ws，连通性由桥进程建立时验证）"
        try:
            with httpx.Client(timeout=5.0, follow_redirects=True) as client:
                resp = client.get(url)
            if resp.status_code < 500:
                return True, f"HTTP GET {url} 可达（{resp.status_code}）"
            return False, f"HTTP GET {url} -> {resp.status_code}"
        except Exception as exc:
            return False, f"连通探测失败（{url}）: {type(exc).__name__}: {exc}"

    # ------------------------------------------------------------------ 私信接入
    def ingest_dm(self, platform: str, from_name: str = "", text: str = "",
                  dm_id: str | None = None, payload: dict | None = None) -> dict:
        """私信接入核心：桥接器 webhook → detections(pending) + account_intel upsert + events。

        - 平台未配置 → 409 not_configured；text 为空 → 422
        - from 非空 → AccountIntelService().check upsert accounts 画像（失败跳过并记 note）
        - 返回 {detection_id, platform, from, status:'pending'}，供反向侦察流水线消费
        """
        self._require_platform(platform)
        row = self._row(platform)
        if row is None or not (row["config_enc"] or "").strip():
            raise ApiError("not_configured", f"{platform} 平台账号桥接未配置，请先保存配置", 409)
        text = (text or "").strip()
        if not text:
            raise ApiError("empty_text", "text 不能为空", 422)

        conn = self._db()
        cur = conn.execute(
            "INSERT INTO detections (source, platform, text_hash, content, status) "
            "VALUES (?, ?, ?, ?, 'pending')",
            (platform, platform,
             hashlib.sha256(text.encode("utf-8")).hexdigest(),
             text[:4000]),
        )
        detection_id = int(cur.lastrowid)

        note = ""
        from_name = (from_name or "").strip()
        intel: dict | None = None
        if from_name:
            try:
                intel = AccountIntelService().check(conn, from_name)
            except ApiError as exc:
                note = f"account_intel upsert 跳过: {exc.message}"
            except Exception as exc:
                note = f"account_intel upsert 跳过: {type(exc).__name__}: {exc}"

        event_payload = {
            "detection_id": detection_id,
            "from": from_name or None,
            "platform": platform,
            "dm_id": dm_id or None,
        }
        if payload:
            event_payload["payload"] = payload
        conn.execute(
            "INSERT INTO events (kind, payload) VALUES ('dm_received', ?)",
            (json.dumps(event_payload, ensure_ascii=False),),
        )
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "UPDATE account_bridges SET last_sync=?, note=?, updated_at=? WHERE platform=?",
            (now, note or None, now, platform),
        )
        conn.commit()
        return {
            "detection_id": detection_id,
            "platform": platform,
            "from": from_name or None,
            "status": "pending",
            "account_intel": intel,
        }

    # ------------------------------------------------------------------ agent 一键导入
    def agent_import(self, platform: str) -> dict:
        """生成本地 agent（DSH/Claude Code/Codex）一键导入配置。"""
        p = self._require_platform(platform)
        hint = {
            "zhihu": "ZhihuBridge 读私信后 POST /api/v1/account-bridges/zhihu/ingest 推送私信",
            "wechat": "WeChatFerry 启动后 POST /api/v1/account-bridges/wechat/ingest 推送私信",
            "qq": "NapCatQQ OneBot 收到私信后 POST /api/v1/account-bridges/qq/ingest",
            "weibo": "Playwright 桥抓到私信后 POST /api/v1/account-bridges/weibo/ingest",
            "douyin": "抖音无开放私信 API，不支持桥接（ai_ready=false）",
            "telegram": "python-telegram-bot long polling 收到私信后 POST /api/v1/account-bridges/telegram/ingest",
            "discord": "discord.py gateway 收到私信后 POST /api/v1/account-bridges/discord/ingest",
        }.get(platform, f"桥接器收到私信后 POST /api/v1/account-bridges/{platform}/ingest")
        mcp_port = int(self.settings.port) + 1  # mcp/server.py --http 默认 9201，主控 9200
        return {
            "platform": platform,
            "mcp_streamable_http": {
                "server_name": f"canary-{platform}",
                "url": f"http://127.0.0.1:{mcp_port}/mcp",
                "headers": {
                    "Authorization": "Bearer <AF_API_KEY>",
                    "Content-Type": "application/json",
                },
            },
            "env_vars": ["AF_API_KEY"],
            "bridge_script_hint": hint,
            "ingest_endpoint": f"/api/v1/account-bridges/{platform}/ingest",
        }
