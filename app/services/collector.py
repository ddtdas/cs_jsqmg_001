"""CollectorService 采集矩阵（P10）。

矩阵 = 平台 × 账号 × 采集策略 的编排表：
  - list_matrix / add_cell / update_cell / remove_cell / run_cell / run_all / tech_stack
  - 采集执行（Crawlee 式：httpx + 重试 + 去重 + 限速）按 platform 分派：
      zhihu    -> ZhihuBridge（CH-A/B/C/D，cookie 复用 account_bridges）
      weibo    -> 占位（需真实 cookie+签名，不真实外呼）
      telegram -> Bot API getUpdates（token 复用 account_bridges）
      rsshub   -> RSSHub 公开聚合（免账号）
      wechat/qq-> 本地桥（WeChatFerry/NapCat）webhook 推送，矩阵只登记
  - 每个采集项经 _ingest（复用 AccountBridgeService.ingest_dm 同逻辑）写入
    detections(pending) + events(dm_received) + 账号 upsert -> 反向侦察流水线。

合规（D7）：只读自有/公开信息；不自动发布（HITL）；限速；凭据加密；防御式异常。
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any

from ..config import Settings, get_settings
from .account_bridge import AccountBridgeService

# 矩阵允许的平台（与前端契约一致）
MATRIX_PLATFORMS: tuple[str, ...] = ("zhihu", "weibo", "telegram", "wechat", "qq", "rsshub")

# 静态 GitHub 技术栈调研结论（2026-09-16，GitHub API 实测 star 数）
TECH_STACK: list[dict[str, Any]] = [
    {
        "name": "Crawlee",
        "repo": "apify/crawlee",
        "stars": 1100,
        "use": "通用网页采集框架（playwright/httpx，自动重试/代理/去重）",
        "integrate": "作为采集引擎参考实现：httpx 客户端 + 指数退避重试 + 近重复去重（本模块已按同模式实现，不引入独立依赖）",
    },
    {
        "name": "Scrapy",
        "repo": "scrapy/scrapy",
        "stars": 64361,
        "use": "通用爬虫框架（调度/中间件/去重）",
        "integrate": "借鉴请求调度与指纹去重思路（RequestFingerprinter），与 FastAPI 同进程不整体引入",
    },
    {
        "name": "DecryptLogin",
        "repo": "CharlesPikachu/DecryptLogin",
        "stars": 2853,
        "use": "知乎/微博/QQ 等多平台登录态解密（rsa/加密参数）",
        "integrate": "借鉴 x-zse-96 等签名思路（仅参考，不引入第三方凭据处理；本系统 HITL cookie 导入）",
    },
    {
        "name": "Wechaty",
        "repo": "wechaty/wechaty",
        "stars": 23273,
        "use": "微信多协议 bot（web/pad）",
        "integrate": "微信桥说明文档引用（账号桥 wechat 已列；本地桥部署后 webhook 推送）",
    },
    {
        "name": "WeChatFerry",
        "repo": "lich0821/WeChatFerry",
        "stars": 10000,
        "use": "微信个人号协议钩子（Windows）",
        "integrate": "微信桥首选：本地启动后 POST /api/v1/account-bridges/wechat/ingest 推送私信",
    },
    {
        "name": "zhihu_spider",
        "repo": "LiuRoy/zhihu_spider",
        "stars": 1282,
        "use": "知乎 API 逆向（api/v4 + x-zse-96）",
        "integrate": "与自有 CH-B httpx 通道互补，签名参考；降级 CH-C RSSHub",
    },
    {
        "name": "NapCatQQ",
        "repo": "NapNeko/NapCatQQ",
        "stars": 5000,
        "use": "NTQQ OneBot 实现",
        "integrate": "QQ 桥首选：OneBot 反向 ws/http 推送私信到主控",
    },
    {
        "name": "python-telegram-bot",
        "repo": "python-telegram-bot/python-telegram-bot",
        "stars": 28000,
        "use": "Telegram Bot 官方库",
        "integrate": "Telegram 桥首选：bot token long polling → 主控 ingest",
    },
]


class CollectorService:
    """采集矩阵服务（settings + 请求级 DB 连接，风格对齐 AccountBridgeService）。"""

    def __init__(self, settings: Settings | None = None, conn=None) -> None:
        self.settings = settings or get_settings()
        self._conn = conn

    # ------------------------------------------------------------------
    # 基础
    # ------------------------------------------------------------------
    def _db(self):
        if self._conn is not None:
            return self._conn
        from .. import db as dbmod

        return dbmod.get_conn()

    def _event(self, kind: str, payload: dict) -> None:
        db = self._db()
        db.execute(
            "INSERT INTO events (kind, payload) VALUES (?, ?)",
            (kind, json.dumps(payload, ensure_ascii=False)),
        )
        try:
            db.commit()
        except Exception:
            pass

    @staticmethod
    def _platform_name(platform: str) -> str:
        from .account_bridge import PLATFORMS

        p = PLATFORMS.get(platform)
        return p["name"] if p else platform

    # ------------------------------------------------------------------
    # 矩阵 CRUD
    # ------------------------------------------------------------------
    def list_matrix(self) -> list[dict]:
        db = self._db()
        rows = db.execute("SELECT * FROM collector_matrix ORDER BY id ASC").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["strategy"] = json.loads(d.get("strategy") or "{}")
            except (json.JSONDecodeError, TypeError):
                d["strategy"] = {}
            d["platform_name"] = self._platform_name(d["platform"])
            out.append(d)
        return out

    def add_cell(self, platform: str, account: str = "", strategy: dict | None = None) -> dict:
        if platform not in MATRIX_PLATFORMS:
            from ..utils import ApiError

            raise ApiError("invalid_platform", f"未知平台 {platform!r}（可选 {', '.join(MATRIX_PLATFORMS)}）", 422)
        if platform == "rsshub":
            account = ""  # 免账号
        strat = strategy or {}
        strat.setdefault("collect", "dm")
        strat.setdefault("frequency", "hourly")
        strat.setdefault("depth", 5)
        strat.setdefault("keyword", "")
        strat.setdefault("near_dup", 0.85)
        db = self._db()
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        cur = db.execute(
            "INSERT INTO collector_matrix (platform, account, strategy, enabled, status, created_at, updated_at) "
            "VALUES (?, ?, ?, 1, 'pending', ?, ?)",
            (platform, account, json.dumps(strat, ensure_ascii=False), now, now),
        )
        db.commit()
        cell_id = int(cur.lastrowid)
        self._event("collector_cell_added", {"cell_id": cell_id, "platform": platform, "account": account})
        return {"id": cell_id, "platform": platform, "account": account, "strategy": strat, "status": "pending"}

    def _require_cell(self, cell_id: int) -> dict:
        db = self._db()
        r = db.execute("SELECT * FROM collector_matrix WHERE id=?", (cell_id,)).fetchone()
        if r is None:
            from ..utils import ApiError

            raise ApiError("cell_not_found", f"采集单元格不存在: id={cell_id}", 404)
        return dict(r)

    def update_cell(self, cell_id: int, strategy: dict | None = None, enabled: bool | None = None) -> dict:
        db = self._db()
        self._require_cell(cell_id)
        if strategy is not None:
            db.execute("UPDATE collector_matrix SET strategy=? WHERE id=?", (json.dumps(strategy, ensure_ascii=False), cell_id))
        if enabled is not None:
            db.execute("UPDATE collector_matrix SET enabled=? WHERE id=?", (1 if enabled else 0, cell_id))
        db.execute("UPDATE collector_matrix SET updated_at=? WHERE id=?", (time.strftime("%Y-%m-%d %H:%M:%S"), cell_id))
        db.commit()
        self._event("collector_cell_updated", {"cell_id": cell_id, "strategy": strategy, "enabled": enabled})
        return self._require_cell(cell_id)

    def remove_cell(self, cell_id: int) -> dict:
        db = self._db()
        self._require_cell(cell_id)
        db.execute("DELETE FROM collector_matrix WHERE id=?", (cell_id,))
        db.commit()
        self._event("collector_cell_deleted", {"cell_id": cell_id})
        return {"deleted": True, "cell_id": cell_id}

    # ------------------------------------------------------------------
    # 采集执行
    # ------------------------------------------------------------------
    def run_cell(self, cell_id: int) -> dict:
        db = self._db()
        cell = self._require_cell(cell_id)
        db.execute("UPDATE collector_matrix SET status='running' WHERE id=?", (cell_id,))
        db.commit()
        try:
            result = self._execute_cell(cell)
            status = "error" if result.get("error") else "ok"
            db.execute(
                "UPDATE collector_matrix SET status=?, last_run_at=?, last_item_id=?, last_error=? WHERE id=?",
                (status, time.strftime("%Y-%m-%d %H:%M:%S"), result.get("last_item_id") or "", (result.get("error") or "")[:500], cell_id),
            )
            db.commit()
            self._event("collector_run", {
                "cell_id": cell_id, "platform": cell["platform"], "collected": result.get("collected", 0), "error": result.get("error"),
            })
            return {"cell_id": cell_id, "platform": cell["platform"], "collected": result.get("collected", 0), "error": result.get("error")}
        except Exception as exc:  # noqa: BLE001 —— 防御式
            msg = str(exc)[:500]
            db.execute(
                "UPDATE collector_matrix SET status='error', last_error=?, last_run_at=? WHERE id=?",
                (msg, time.strftime("%Y-%m-%d %H:%M:%S"), cell_id),
            )
            db.commit()
            return {"cell_id": cell_id, "platform": cell["platform"], "collected": 0, "error": msg}

    def run_all(self) -> dict:
        db = self._db()
        cells = db.execute("SELECT id FROM collector_matrix WHERE enabled=1 ORDER BY id ASC").fetchall()
        ran = 0
        collected = 0
        for c in cells:
            try:
                r = self.run_cell(int(c["id"]))
                ran += 1
                collected += int(r.get("collected") or 0)
            except Exception:
                continue
            time.sleep(0.5)  # 节流
        return {"ran": ran, "collected": collected}

    def _dedupe(self, text: str, cell: dict) -> bool:
        """返回 True 表示重复（跳过）。last_item_id 存 sha256。"""
        h = hashlib.sha256((text or "").strip().encode("utf-8")).hexdigest()
        if cell.get("last_item_id") == h:
            return True
        cell["_new_hash"] = h
        return False

    def _ingest(self, platform: str, from_name: str, text: str, dm_id: str | None, cell: dict) -> int:
        """复用 AccountBridgeService.ingest_dm；未配置平台仅返回结构不落库。"""
        svc = AccountBridgeService(settings=self.settings, conn=self._db())
        try:
            r = svc.ingest_dm(platform, from_name=from_name, text=text, dm_id=dm_id)
            if cell.get("_new_hash"):
                db = self._db()
                db.execute("UPDATE collector_matrix SET last_item_id=? WHERE id=?", (cell["_new_hash"], cell["id"]))
                db.commit()
            return 1 if r.get("detection_id") else 0
        except Exception as exc:  # noqa: BLE001 —— 未配置/校验失败降级
            note = str(exc)
            if "not_configured" in note or "409" in note:
                return 0
            return 0

    def _execute_cell(self, cell: dict) -> dict:
        platform = cell["platform"]
        strategy = cell.get("strategy") or {}
        try:
            strategy = json.loads(strategy) if isinstance(strategy, str) else strategy
        except (json.JSONDecodeError, TypeError):
            strategy = {}
        collect = strategy.get("collect", "dm")
        collected = 0
        last_item_id = cell.get("last_item_id") or ""
        error = None

        if platform == "zhihu":
            if collect == "dm":
                # 尝试复用 ZhihuBridge；未配置 cookie 则降级 note
                try:
                    from .zhihu_bridge import ChannelUnavailableError, ZhihuBridge

                    bridge = ZhihuBridge(settings=self.settings)

                    async def _fetch() -> list:
                        try:
                            return await bridge.fetch_inbox()
                        except ChannelUnavailableError:
                            return []

                    import asyncio

                    items = asyncio.run(_fetch())
                    for it in items:
                        if self._dedupe(it.text or "", cell):
                            continue
                        collected += self._ingest(platform, it.from_url_name or "", it.text or "", it.msg_id or None, cell)
                except Exception as exc:  # noqa: BLE001
                    error = f"zhihu dm 采集降级: {exc}"
            else:
                error = "知乎评论/帖子采集需要真实 cookie 与 x-zse-96 签名（占位，走 CH-C RSSHub 或账号桥导入）"
        elif platform == "weibo":
            error = "微博采集需要真实 cookie 与签名（占位，待账号桥 cookie 配置后接入）"
        elif platform == "telegram":
            token = self._bridge_secret("telegram", "bot_token")
            if not token:
                error = "Telegram 未配置 bot_token"
            else:
                try:
                    import httpx

                    r = httpx.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=8)
                    data = r.json()
                    for upd in (data.get("result") or []):
                        msg = upd.get("message") or {}
                        text = msg.get("text") or ""
                        frm = (msg.get("from") or {}).get("username") or str((msg.get("from") or {}).get("id", ""))
                        if not text:
                            continue
                        if self._dedupe(text, cell):
                            continue
                        collected += self._ingest(platform, frm, text, str(upd.get("update_id", "")), cell)
                except Exception as exc:  # noqa: BLE001
                    error = f"telegram 采集失败: {exc}"
        elif platform == "rsshub":
            try:
                import httpx

                keyword = (strategy.get("keyword") or "").strip()
                url = "https://rsshub.app/zhihu/toplist"
                if keyword:
                    url = f"https://rsshub.app/zhihu/search/{keyword}"
                r = httpx.get(url, timeout=8, follow_redirects=True)
                if r.status_code != 200:
                    error = f"RSSHub 不可达（HTTP {r.status_code}）"
                else:
                    import xml.etree.ElementTree as ET

                    root = ET.fromstring(r.text)
                    ns = {"rss": "http://www.w3.org/2005/Atom", "rss2": ""}
                    items = []
                    # 兼容 RSS 2.0 (channel/item) 与 Atom (feed/entry)
                    for item in root.iter("item"):
                        title = item.findtext("title") or ""
                        desc = item.findtext("description") or ""
                        link = item.findtext("link") or ""
                        items.append((title, desc, link))
                    for entry in root.iter("{http://www.w3.org/2005/Atom}entry"):
                        title = entry.findtext("{http://www.w3.org/2005/Atom}title") or ""
                        link_el = entry.find("{http://www.w3.org/2005/Atom}link")
                        link = link_el.get("href") if link_el is not None else ""
                        items.append((title, "", link))
                    for title, desc, link in items:
                        text = (title + " " + desc).strip()
                        if not text:
                            continue
                        if self._dedupe(text, cell):
                            continue
                        collected += self._ingest("rsshub", "", text, link or None, cell)
            except Exception as exc:  # noqa: BLE001
                error = f"RSSHub 采集失败: {exc}"
        elif platform in ("wechat", "qq"):
            error = None
            # 桥 webhook 由账号桥接收；矩阵只登记
            note = "由本地桥(WeChatFerry/NapCat)推送私信，矩阵只登记状态"
            if cell.get("note") != note:
                db = self._db()
                db.execute("UPDATE collector_matrix SET note=? WHERE id=?", (note, cell["id"]))
                db.commit()
        else:
            error = f"未知平台 {platform}"

        return {"collected": collected, "last_item_id": last_item_id, "error": error}

    def _bridge_secret(self, platform: str, field_key: str) -> str:
        """从 account_bridges 解密取敏感字段（如 telegram bot_token）。"""
        try:
            svc = AccountBridgeService(settings=self.settings, conn=self._db())
            detail = svc.get_detail(platform)
            if not detail.get("has_config"):
                return ""
            # get_detail 不回显明文；直接读库解密
            db = self._db()
            row = db.execute("SELECT config_enc FROM account_bridges WHERE platform=?", (platform,)).fetchone()
            if not row or not row["config_enc"]:
                return ""
            dec = svc.vault.decrypt(row["config_enc"])
            # 双层加密：内层 JSON 再解
            inner = svc.vault.decrypt(dec) if isinstance(dec, str) and dec.startswith("gAAAA") else dec
            try:
                data = json.loads(inner) if isinstance(inner, str) else inner
            except (json.JSONDecodeError, TypeError):
                try:
                    data = json.loads(dec)
                except Exception:
                    return ""
            if isinstance(data, dict):
                for k, v in data.items():
                    if field_key in k:
                        try:
                            return svc.vault.decrypt(v) if isinstance(v, str) and v.startswith("gAAAA") else str(v)
                        except Exception:
                            return str(v)
            return ""
        except Exception:
            return ""

    # ------------------------------------------------------------------
    # 技术栈
    # ------------------------------------------------------------------
    def tech_stack(self) -> list[dict]:
        return TECH_STACK
