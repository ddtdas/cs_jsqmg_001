"""ZhihuBridge 知乎四通道适配器（P2）。

对应主方案《01_蜜罐反诈V2_双形态技术方案与实施计划.md》§5.2/5.3/5.4 与接口示意
`示例与模板/zhihu_bridge_接口示意.py`，统一接口供业务层调用，屏蔽通道差异：

  CH-A Playwright 浏览器会话（主通道）—— cookie 导入/会话管理/读私信中心；
         headless 可选（AF_ZHIHU_CH_A_HEADLESS）；playwright 缺失时健康返回
         ok=false 并标注，系统不崩、自动降级。
  CH-B httpx 私有 API 模拟（备用）—— api/v4 低敏读操作；遇 x-zse-96 等
         签名校验特征标记 degraded 并自动降级 CH-A（§5.2）。
  CH-C RSSHub 只读公开聚合 —— fetch_profile / search 的公开信号补充，
         零登录（AF_ZHIHU_RSSHUB_BASE 可配，留空=未配置）。
  CH-D 手动导入（保底）—— ingest_manual() 粘贴文本/JSON → 写入 detections
         表（status='pending'），由 P3 检测流水线消费；演示与离线必用。

合规红线（D7 / §5.4）：
  * 只读自有/公开信息；不扫手机号/定位/关注图谱外数据；
  * HITL（D3 / §5.3）：generate_trap_draft 只产草稿文案 + 指纹 UUID + 埋设指引，
    永不自动发布——发布动作由用户手动完成；
  * 全局令牌桶限速（AF_ZHIHU_RATE_LIMIT，默认 20 请求/分）+ 失败指数退避；
  * cookie 一律 Fernet 加密存储（data/secret.key 自动生成或 AF_ZHIHU_ENCRYPT_KEY），
    明文不落库（zhihu_sessions.cookie_enc）。
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import random
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..config import Settings, get_settings
from .. import db as dbmod
from .config_reader import cfg_float

# ---------------------------------------------------------------------------
# 通道标识
# ---------------------------------------------------------------------------
CHANNELS: tuple[str, ...] = ("ch-a", "ch-b", "ch-c", "ch-d")

# P3-9/R7（L2-B）：导入层近重复判定阈值（overlap，0-1）。
# - 默认 0.85：仅跳过"近乎原样重发"（同一句 + 标点/时间戳/虚词微调）的文本；
#   明显改写的新消息会作为新证据入库（反诈取证语义：每次新消息都应被记录）。
# - 阈值可经 configs 表热更新：zhihu.ingest_near_dup_threshold（config_reader 接线）。
# - 导入层不做 SimHash（hamming）判定：短中文文本 hamming≤8 过于激进，
#   会把不同骗术但词汇相近的文本误判重复；近重复精判收敛在 scan/_persist 层。
INGEST_NEAR_DUP_THRESHOLD = 0.85


class ChannelUnavailableError(RuntimeError):
    """通道不可用信号：携带 channel 与原因，上层据此降级下一通道（§5.2）。"""

    def __init__(self, channel: str, note: str = "") -> None:
        super().__init__(f"channel {channel} unavailable: {note}")
        self.channel = channel
        self.note = note


class RateLimitExceeded(RuntimeError):
    """令牌桶超限：请求被拒（调用方可按策略重试/排队）。"""

    def __init__(self, note: str = "请求频率超限（令牌桶）") -> None:
        super().__init__(note)
        self.note = note


# ---------------------------------------------------------------------------
# 数据类（对齐接口示意）
# ---------------------------------------------------------------------------
@dataclass
class ChannelHealth:
    channel: str
    ok: bool
    note: str = ""
    degraded: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class InboxItem:
    msg_id: str
    from_url_name: str
    text: str
    created_at: str = ""
    raw: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ProfileSignals:
    url_name: str
    registered_at: str | None = None
    activity_level: float | None = None    # 0-1
    content_consistency: float | None = None
    trap_hit_ids: list[str] = field(default_factory=list)
    note: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# 频率控制：令牌桶（真实实现，线程安全）
# ---------------------------------------------------------------------------
class TokenBucket:
    """线程安全令牌桶（§5.4 全局限速）。

    容量 = 每分钟配额；按 (容量/60) 个/秒匀速补充。try_acquire 立即可判定
    （超限即拒），acquire 支持阻塞排队（供调度器长任务使用）。
    """

    def __init__(self, capacity: int, refill_per_second: float | None = None) -> None:
        self.capacity = max(1, int(capacity))
        self.refill_rate = (
            refill_per_second if refill_per_second is not None else self.capacity / 60.0
        )
        self._tokens = float(self.capacity)
        self._updated = time.monotonic()
        self._lock = threading.Lock()

    def _replenish(self) -> None:
        now = time.monotonic()
        self._tokens = min(
            self.capacity, self._tokens + (now - self._updated) * self.refill_rate
        )
        self._updated = now

    def try_acquire(self, n: int = 1) -> bool:
        """尝试取 n 个令牌；不足返回 False（不阻塞）。"""
        with self._lock:
            self._replenish()
            if self._tokens >= n:
                self._tokens -= n
                return True
            return False

    def acquire(self, n: int = 1, timeout: float = 30.0) -> bool:
        """阻塞取令牌（排队），超时返回 False。调度器/长任务用。"""
        deadline = time.monotonic() + timeout if timeout > 0 else None
        while True:
            if self.try_acquire(n):
                return True
            if deadline is not None and time.monotonic() >= deadline:
                return False
            time.sleep(0.05)

    @property
    def available(self) -> float:
        with self._lock:
            self._replenish()
            return round(self._tokens, 3)

    def snapshot(self) -> dict:
        return {"capacity": self.capacity, "available": self.available}


# ---------------------------------------------------------------------------
# 失败指数退避（真实实现）
# ---------------------------------------------------------------------------
class BackoffManager:
    """失败指数退避（§5.4）：每个通道连续失败后按 base*2^attempt 退避，封顶 cap。

    record_failure 之后，该通道在 next_allowed 之前视为“冷却中”；
    due() 为 False 时上层应跳过该通道。record_success 重置计数。
    """

    def __init__(
        self, base: float = 1.0, factor: float = 2.0, cap: float = 60.0, jitter: bool = True
    ) -> None:
        self.base = base
        self.factor = factor
        self.cap = cap
        self.jitter = jitter
        self._attempts: dict[str, int] = {}
        self._next_allowed: dict[str, float] = {}
        self._lock = threading.Lock()

    def _delay(self, attempts: int) -> float:
        d = min(self.cap, self.base * (self.factor ** max(0, attempts - 1)))
        if self.jitter:
            d *= random.uniform(0.5, 1.0)
        return d

    def record_failure(self, channel: str) -> float:
        with self._lock:
            n = self._attempts.get(channel, 0) + 1
            self._attempts[channel] = n
            delay = self._delay(n)
            self._next_allowed[channel] = time.monotonic() + delay
            return delay

    def record_success(self, channel: str) -> None:
        with self._lock:
            self._attempts.pop(channel, None)
            self._next_allowed.pop(channel, None)

    def due(self, channel: str) -> bool:
        with self._lock:
            nxt = self._next_allowed.get(channel, 0.0)
            return time.monotonic() >= nxt

    def wait_seconds(self, channel: str) -> float:
        with self._lock:
            nxt = self._next_allowed.get(channel, 0.0)
            return max(0.0, nxt - time.monotonic())

    def snapshot(self) -> dict:
        with self._lock:
            now = time.monotonic()
            return {
                "channels": {
                    ch: {
                        "attempts": self._attempts.get(ch, 0),
                        "cooldown_sec": round(max(0.0, self._next_allowed.get(ch, 0.0) - now), 1),
                    }
                    for ch in set(self._attempts) | set(self._next_allowed)
                }
            }


# ---------------------------------------------------------------------------
# 全局限速/退避单例（P1-4：修复"每请求新建 ZhihuBridge → 令牌桶/退避状态清零"）
# 主方案 §5.4/§11「全局令牌桶（默认 20 请求/分）+ 失败指数退避」：
#   - get_global_limiter/get_global_backoff 为模块级懒加载单例，所有 ZhihuBridge
#     共享同一实例（容量按 settings.zhihu_rate_limit 读取，配置变更自动重建）；
#   - reset_global_limits() 为测试隔离钩子（清空单例，重建时读最新配置）。
# ---------------------------------------------------------------------------
_limits_lock = threading.Lock()
_global_limiter: TokenBucket | None = None
_global_limiter_capacity: int = 0
_global_backoff: BackoffManager | None = None


def get_global_limiter(settings: Settings | None = None) -> TokenBucket:
    """全局令牌桶单例（容量取 settings.zhihu_rate_limit，变更自动重建）。"""
    global _global_limiter, _global_limiter_capacity
    s = settings or get_settings()
    capacity = max(1, int(s.zhihu_rate_limit))
    with _limits_lock:
        if _global_limiter is None or _global_limiter_capacity != capacity:
            _global_limiter = TokenBucket(
                capacity=capacity, refill_per_second=capacity / 60.0
            )
            _global_limiter_capacity = capacity
        return _global_limiter


def get_global_backoff() -> BackoffManager:
    """全局失败指数退避单例。"""
    global _global_backoff
    with _limits_lock:
        if _global_backoff is None:
            _global_backoff = BackoffManager()
        return _global_backoff


def reset_global_limits() -> None:
    """清空全局限速/退避单例（测试隔离钩子，P1-4）。"""
    global _global_limiter, _global_limiter_capacity, _global_backoff
    with _limits_lock:
        _global_limiter = None
        _global_limiter_capacity = 0
        _global_backoff = None


# ---------------------------------------------------------------------------
# cookie 加密（Fernet）
# ---------------------------------------------------------------------------
class CookieVault:
    """Fernet cookie 加解密（§5.4：cookie 加密存储，禁止明文落库）。

    密钥来源优先级：
      1) AF_ZHIHU_ENCRYPT_KEY（Fernet key，44 字符 base64 urlsafe）
      2) AF_ZHIHU_KEY_FILE（独立密钥文件路径，P3-8：可与数据目录分离，便于权限隔离）
      3) {data_dir}/secret.key 幂等自动生成（首次启动创建，之后复用）
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.key_path: Path = self._resolve_key_path()
        self._key = self._load_or_create_key()
        from cryptography.fernet import Fernet  # 惰性导入：仅此处需要 cryptography

        self._fernet = Fernet(self._key)

    def _resolve_key_path(self) -> Path:
        """密钥文件路径：AF_ZHIHU_KEY_FILE 优先（相对路径基于项目根），否则 data/secret.key。"""
        override = (self.settings.zhihu_key_file or "").strip()
        if override:
            p = Path(override)
            if not p.is_absolute():
                p = Path(__file__).resolve().parent.parent / p
            return p
        return dbmod.data_dir(self.settings) / "secret.key"

    def _load_or_create_key(self) -> bytes:
        from cryptography.fernet import Fernet  # 惰性：验证 key 合法性

        if self.settings.zhihu_encrypt_key:
            raw = self.settings.zhihu_encrypt_key.strip()
            return raw.encode("utf-8") if isinstance(raw, str) else raw
        if self.key_path.exists():
            key = self.key_path.read_text(encoding="utf-8").strip()
            if key:
                return key.encode("utf-8")
        key = Fernet.generate_key()  # type: ignore[attr-defined]
        self.key_path.parent.mkdir(parents=True, exist_ok=True)
        self.key_path.write_text(key.decode("ascii") + "\n", encoding="utf-8")
        return key

    def encrypt(self, plaintext: str) -> str:
        """加密任意文本（cookie 字符串），返回 base64 token（可存 TEXT 列）。"""
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")

    def decrypt(self, token: str) -> str:
        """解密；token 非法时抛 ValueError（调用方按通道失效处理）。"""
        try:
            return self._fernet.decrypt(token.encode("ascii")).decode("utf-8")
        except Exception as exc:  # InvalidToken 等
            raise ValueError(f"Fernet token 解密失败: {exc}") from exc


# ---------------------------------------------------------------------------
# 蜜饵离线模板（D5 降级链：LLM 不可用时模板变体池；P4 会接 LLM 伪装度校验）
# ---------------------------------------------------------------------------
TRAP_TEMPLATES: dict[str, list[str]] = {
    "pig_butchering": [
        "最近在关注加密货币，感觉行情要来了，你之前研究过这块吗？",
        "我有个朋友做外盘期货赚了不少，他说新手也能跟着学，你怎么看？",
    ],
    "fake_investment": [
        "看到一个项目收益率挺高的，说是内部渠道才有名额，有点心动想试试。",
        "听说最近打新中签率很高，跟着导师做能稳一点，真的假的？",
    ],
    "job_scam": [
        "看到个兼职说足不出户日结佣金，但要先垫付做任务，这靠谱吗？",
        "有人拉我做刷单返利，说是做完一单立返，你遇到过这种吗？",
    ],
    "impersonation": [
        "接到电话说我涉嫌洗钱要配合调查，还要把钱转到安全账户，该信吗？",
    ],
    "other": [
        "感觉今天运势不错，要不要一起研究点副业门路？",
        "你听说过最近有个内测项目吗？说是名额有限先到先得。",
    ],
}
_SCAM_KEYWORD_HINTS = (
    "稳赚", "高回报", "内幕", "导师", "包赔", "保证金", "垫付", "刷单",
    "安全账户", "洗钱", "公检法", "中奖", "提现", "手续费",
)


# ---------------------------------------------------------------------------
# ZhihuBridge 主类
# ---------------------------------------------------------------------------
class ZhihuBridge:
    """知乎多通道适配器（§5.2 统一接口）。

    用法：
      bridge = ZhihuBridge()                      # 默认 settings + 线程局部 DB 连接
      await bridge.fetch_inbox()                  # CH-A → CH-B 自动降级
      bridge.ingest_manual("粘贴的对话文本...")    # CH-D 保底入检测流水线
      await bridge.health()                       # 四通道健康探活
    """

    def __init__(
        self,
        settings: Settings | None = None,
        conn=None,
        vault: CookieVault | None = None,
        limiter: TokenBucket | None = None,
        backoff: BackoffManager | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self._conn = conn  # 可为 None：方法内部回退 thread-local 连接
        self.vault = vault or CookieVault(self.settings)
        # P1-4：默认共享全局单例（令牌桶/退避跨请求保留；显式传入的自定义实例仍优先，供测试）
        self.limiter = limiter or get_global_limiter(self.settings)
        self.backoff = backoff or get_global_backoff()

    # ------------------------------------------------------------------
    # DB 会话（zhihu_sessions）读写
    # ------------------------------------------------------------------
    def _db(self):
        return self._conn if self._conn is not None else dbmod.get_conn()

    def get_session(self, channel: str) -> dict | None:
        row = self._db().execute(
            "SELECT * FROM zhihu_sessions WHERE channel = ?", (channel,)
        ).fetchone()
        return dict(row) if row else None

    def upsert_session(
        self,
        channel: str,
        *,
        cookie_enc: str | None = None,
        status: str | None = None,
        health: str | None = None,
        last_sync: str | None = None,
    ) -> None:
        """幂等写 zhihu_sessions（select-then-update，不依赖 channel 唯一约束）。

        只写加密 cookie，原文不落库。
        """
        conn = self._db()
        row = conn.execute(
            "SELECT id FROM zhihu_sessions WHERE channel = ?", (channel,)
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO zhihu_sessions (channel, cookie_enc, status, health, last_sync) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    channel,
                    cookie_enc,
                    status or "inactive",
                    health or "unknown",
                    last_sync,
                ),
            )
        else:
            sets: list[str] = []
            vals: list = []
            if cookie_enc is not None:
                sets.append("cookie_enc = ?")
                vals.append(cookie_enc)
            if status is not None:
                sets.append("status = ?")
                vals.append(status)
            if health is not None:
                sets.append("health = ?")
                vals.append(health)
            if last_sync is not None:
                sets.append("last_sync = ?")
                vals.append(last_sync)
            if sets:
                conn.execute(
                    f"UPDATE zhihu_sessions SET {', '.join(sets)} WHERE channel = ?",
                    (*vals, channel),
                )
        conn.commit()

    # ------------------------------------------------------------------
    # 频率控制与退避（真实实现：所有对外请求先过闸）
    # ------------------------------------------------------------------
    def _gate(self, channel: str) -> None:
        """每个对外请求前的限速闸：令牌不足抛 RateLimitExceeded；通道退避未到则拒绝。"""
        if not self.backoff.due(channel):
            raise RateLimitExceeded(
                f"通道 {channel} 退避冷却中（{self.backoff.wait_seconds(channel):.1f}s）"
            )
        if not self.limiter.try_acquire(1):
            raise RateLimitExceeded(
                f"令牌桶超限（剩余 {self.limiter.available:.1f}/{self.limiter.capacity}）"
            )

    def _fail(self, channel: str) -> None:
        """记录一次失败并启动指数退避。"""
        self.backoff.record_failure(channel)

    def _mark_degraded(self, channel: str, note: str) -> None:
        """把通道会话标记为 degraded（zhihu_sessions.status/health），供 /zhihu 页面展示。"""
        self.upsert_session(channel, status="degraded", health="degraded")
        self._fail(channel)

    # ------------------------------------------------------------------
    # cookie 管理（统一入口；加密后写库）
    # ------------------------------------------------------------------
    @staticmethod
    def parse_cookie(cookie: str, scheme: str = "header") -> list[dict]:
        """把用户粘贴的 cookie 解析为 Playwright/浏览器格式的 cookie 列表。

        scheme="header"："name=value; name2=value2" 字符串（浏览器 DevTools 复制格式）。
        scheme="json"：JSON 数组 [{name, value, domain?, path?, expires?}] 或 {name: value}。
        """
        raw = (cookie or "").strip()
        if not raw:
            raise ValueError("cookie 不能为空")
        if scheme == "json":
            data = json.loads(raw)
            if isinstance(data, dict):
                return [
                    {"name": k, "value": str(v), "domain": ".zhihu.com", "path": "/"}
                    for k, v in data.items()
                ]
            if isinstance(data, list):
                out = []
                for item in data:
                    if not isinstance(item, dict) or "name" not in item:
                        continue
                    out.append({
                        "name": str(item["name"]),
                        "value": str(item.get("value", "")),
                        "domain": str(item.get("domain", ".zhihu.com")),
                        "path": str(item.get("path", "/")),
                        **({"expires": float(item["expires"])} if item.get("expires") else {}),
                    })
                return out
            raise ValueError("scheme=json 需为数组或对象")
        # header scheme：name=value; name2=value2
        out = []
        for seg in raw.split(";"):
            seg = seg.strip()
            if not seg or "=" not in seg:
                continue
            name, value = seg.split("=", 1)
            out.append({"name": name.strip(), "value": value.strip(),
                        "domain": ".zhihu.com", "path": "/"})
        if not out:
            raise ValueError("cookie 解析为空（期望 name=value; ...）")
        return out

    def set_cookie(self, channel: str, cookie: str, scheme: str = "header") -> dict:
        """注入并加密存储 cookie（POST /zhihu/channels/{id}/login）。

        仅 CH-A/CH-B 需要登录态；返回会话摘要（不含明文）。
        """
        if channel not in ("ch-a", "ch-b"):
            raise ValueError("仅 ch-a / ch-b 支持 cookie 注入")
        parsed = self.parse_cookie(cookie, scheme)
        enc = self.vault.encrypt(json.dumps(parsed, ensure_ascii=False))
        self.upsert_session(channel, cookie_enc=enc, status="active", health="ok")
        self.backoff.record_success(channel)  # 注入成功视为通道恢复
        return {
            "channel": channel,
            "scheme": scheme,
            "cookie_count": len(parsed),
            "stored": True,
            "encrypted": True,
            "note": "cookie 已 Fernet 加密存储（zhihu_sessions.cookie_enc），明文不落库",
        }

    def get_cookie(self, channel: str) -> str | None:
        """取出并解密 cookie（返回浏览器 cookie 列表的 JSON 字符串），无则 None。"""
        sess = self.get_session(channel)
        if not sess or not sess.get("cookie_enc"):
            return None
        try:
            enc = sess["cookie_enc"]
            return self.vault.decrypt(enc)
        except ValueError:
            return None  # 解密失败视为未注入（通道失效，不抛给上层）

    def verify_cookie(self, channel: str) -> dict:
        """校验已存 cookie：解密 + 解析 + 是否含关键字段（不发起网络请求）。

        返回 {channel, valid, note, cookie_count, has_dc0, expires_at}——
        真正的登录态有效性由实际抓取（fetch_*）时探明，此处的结构校验用于 404 兜底。
        """
        if channel not in ("ch-a", "ch-b"):
            return {"channel": channel, "valid": False,
                    "note": f"{channel} 无需 cookie（只读公开/手动导入）"}
        sess = self.get_session(channel)
        enc = sess.get("cookie_enc") if sess else None
        if not enc:
            return {"channel": channel, "valid": False,
                    "note": "未注入 cookie，请先调用 login 注入"}
        try:
            raw = self.vault.decrypt(enc)
            parsed = json.loads(raw)
        except (ValueError, json.JSONDecodeError) as exc:
            return {"channel": channel, "valid": False, "note": f"cookie 解密/解析失败: {exc}"}
        names = {c.get("name") for c in parsed}
        has_dc0 = "d_c0" in names
        return {
            "channel": channel,
            "valid": True,
            "cookie_count": len(parsed),
            "has_dc0": has_dc0,
            "expires_at": next(
                (c.get("expires") for c in parsed if c.get("expires")), None
            ),
            "note": "结构校验通过；登录态有效性在实际抓取时探明",
        }

    # ------------------------------------------------------------------
    # 通道探活（health）
    # ------------------------------------------------------------------
    @staticmethod
    def _playwright_importable() -> bool:
        try:
            return importlib.util.find_spec("playwright") is not None
        except Exception:
            return False

    def ch_a_probe(self) -> ChannelHealth:
        """CH-A 探活：playwright 模块 → chromium 可执行文件 → cookie 注入状态。

        无 Playwright / 无浏览器时返回 ok=false + degraded 标注，绝不抛异常（系统不崩）。
        """
        if not self._playwright_importable():
            return ChannelHealth(
                channel="ch-a", ok=False, degraded=True,
                note="Playwright 未安装（pip install playwright；缺省自动降级 CH-B/CH-D）",
            )
        try:
            from playwright.sync_api import sync_playwright

            with sync_playwright() as p:
                exe = p.chromium.executable_path
            if not exe or not Path(exe).exists():
                return ChannelHealth(
                    channel="ch-a", ok=False, degraded=True,
                    note="chromium 浏览器缺失（playwright install chromium）",
                )
        except Exception as exc:
            return ChannelHealth(
                channel="ch-a", ok=False, degraded=True,
                note=f"Playwright 初始化失败: {type(exc).__name__}: {exc}",
            )
        cookie = self.get_cookie("ch-a")
        sess = self.get_session("ch-a")
        note = "浏览器就绪；未注入 cookie（登录后可用）" if not cookie else "浏览器就绪；cookie 已注入"
        if sess and sess.get("health") == "degraded":
            note += "；上次会话标记 degraded"
        return ChannelHealth(channel="ch-a", ok=True, note=note)

    def ch_b_probe(self) -> ChannelHealth:
        """CH-B 探活：httpx 可用性 + cookie 注入 + degraded 标记（x-zse-96 后置标记）。"""
        if importlib.util.find_spec("httpx") is None:
            return ChannelHealth(
                channel="ch-b", ok=False, degraded=True,
                note="httpx 未安装（pip install httpx；CH-B 备用通道不可用）",
            )
        sess = self.get_session("ch-b")
        if sess and sess.get("status") == "degraded":
            return ChannelHealth(
                channel="ch-b", ok=False, degraded=True,
                note="遇 x-zse-96 签名校验已标记 degraded，自动降级 CH-A（§5.2）",
            )
        cookie = self.get_cookie("ch-b")
        note = "httpx 就绪；未注入 cookie" if not cookie else "httpx 就绪；cookie 已注入"
        if not cookie and self.get_session("ch-a"):
            note += "（可回退使用 ch-a 登录态）"
        return ChannelHealth(channel="ch-b", ok=True, note=note)

    def ch_c_probe(self) -> ChannelHealth:
        """CH-C 探活：仅配置态探测（不发起网络，连通性在使用时校验并降级）。"""
        base = (self.settings.zhihu_rsshub_base or "").strip()
        if not base:
            return ChannelHealth(
                channel="ch-c", ok=False, degraded=True,
                note="RSSHub 未配置（AF_ZHIHU_RSSHUB_BASE），仅公开信号补充不可用",
            )
        return ChannelHealth(
            channel="ch-c", ok=True,
            note=f"RSSHub 已配置（{base}）；连通性在使用时校验",
        )

    @staticmethod
    def ch_d_probe() -> ChannelHealth:
        """CH-D：手动导入永远可用（粘贴/文件，离线保底，D5 兜底）。"""
        return ChannelHealth(
            channel="ch-d", ok=True,
            note="手动导入（粘贴文本/JSON 文件）离线可用，演示与保底必用",
        )

    async def health(self) -> list[ChannelHealth]:
        """返回四通道健康列表（GET /zhihu/channels）。"""
        return [
            self.ch_a_probe(),
            self.ch_b_probe(),
            self.ch_c_probe(),
            self.ch_d_probe(),
        ]

    # ------------------------------------------------------------------
    # 统一接口：私信 / 评论 / 公开画像 / 搜索
    # ------------------------------------------------------------------
    async def fetch_inbox(self) -> list[InboxItem]:
        """拉取新私信列表：CH-A 主通道 → 失败自动降级 CH-B（§5.2）。

        两通道均不可用时抛 ChannelUnavailableError（CH-D 手动导入是 ingest 而非 fetch，
        由上层提示用户改用 CH-D）。
        """
        reasons: list[str] = []
        if self._playwright_importable() and self.get_cookie("ch-a"):
            try:
                return await self._ch_a_fetch_inbox()
            except ChannelUnavailableError as exc:
                reasons.append(f"ch-a: {exc.note}")
        else:
            reasons.append("ch-a: Playwright 不可用或未注入 cookie")

        if self.get_cookie("ch-b") or self.get_cookie("ch-a"):
            try:
                self._gate("ch-b")
                return await self._ch_b_fetch_inbox()
            except ChannelUnavailableError as exc:
                reasons.append(f"ch-b: {exc.note}")
            except RateLimitExceeded as exc:
                reasons.append(f"ch-b: {exc.note}")
        else:
            reasons.append("ch-b: 未注入 cookie")
        raise ChannelUnavailableError(
            "ch-a",
            "；".join(reasons) + "；可用 CH-D 手动导入（POST /zhihu/import）",
        )

    async def fetch_comments(self, answer_url: str) -> list[InboxItem]:
        """拉取指定回答（自有）下新评论：CH-A → CH-B 降级。"""
        reasons: list[str] = []
        if self._playwright_importable() and self.get_cookie("ch-a"):
            try:
                return await self._ch_a_fetch_comments(answer_url)
            except ChannelUnavailableError as exc:
                reasons.append(f"ch-a: {exc.note}")
        else:
            reasons.append("ch-a: Playwright 不可用或未注入 cookie")
        if self.get_cookie("ch-b") or self.get_cookie("ch-a"):
            try:
                self._gate("ch-b")
                return await self._ch_b_fetch_comments(answer_url)
            except ChannelUnavailableError as exc:
                reasons.append(f"ch-b: {exc.note}")
            except RateLimitExceeded as exc:
                reasons.append(f"ch-b: {exc.note}")
        raise ChannelUnavailableError(
            "ch-a", "；".join(reasons) + "；可用 CH-D 手动导入（POST /zhihu/import）"
        )

    async def fetch_profile(self, url_name: str) -> ProfileSignals:
        """拉取公开元数据：CH-C RSSHub 公开聚合（主）→ CH-A 公开页（备）。

        只读公开信息（D7）；两通道均不可用时返回空信号 + note（不抛错，供分级降级）。
        """
        base = (self.settings.zhihu_rsshub_base or "").strip()
        if base:
            try:
                self._gate("ch-c")
                return await self._ch_c_fetch_profile(url_name)
            except (ChannelUnavailableError, RateLimitExceeded) as exc:
                self._fail("ch-c")
                last_err = f"ch-c: {exc.note}"
        else:
            last_err = "ch-c: RSSHub 未配置"
        if self._playwright_importable():
            try:
                self._gate("ch-a")
                return await self._ch_a_fetch_profile(url_name)
            except (ChannelUnavailableError, RateLimitExceeded) as exc:
                last_err += f"；ch-a: {exc.note}"
        return ProfileSignals(url_name=url_name, note=last_err)

    async def search(self, keyword: str) -> list[dict]:
        """话题/回答搜索：CH-C RSSHub 聚合（主）→ CH-A 网页搜索（备）。"""
        base = (self.settings.zhihu_rsshub_base or "").strip()
        if base:
            try:
                self._gate("ch-c")
                return await self._ch_c_search(keyword)
            except (ChannelUnavailableError, RateLimitExceeded) as exc:
                self._fail("ch-c")
                last_err = f"ch-c: {exc.note}"
        else:
            last_err = "ch-c: RSSHub 未配置"
        if self._playwright_importable():
            try:
                self._gate("ch-a")
                return await self._ch_a_search(keyword)
            except (ChannelUnavailableError, RateLimitExceeded) as exc:
                last_err += f"；ch-a: {exc.note}"
        raise ChannelUnavailableError("ch-c", last_err)

    # ------------------------------------------------------------------
    # CH-D 手动导入（先落地，保底/演示必用）
    # ------------------------------------------------------------------
    @staticmethod
    def _extract_messages(text: str) -> tuple[list[str], str]:
        """从粘贴文本中抽取消息列表。

        支持：纯文本（单条）；JSON 对象 {"text"/"content"/"messages"/"items"}；
        JSON 数组（元素为字符串或 {text|content|message}）。解析失败按纯文本处理。
        """
        stripped = (text or "").strip()
        if not stripped:
            return [], "空文本"
        if stripped[0] in "{[":  # 尝试 JSON
            try:
                data = json.loads(stripped)
            except json.JSONDecodeError:
                return [stripped], "纯文本（JSON 解析失败按原文处理）"
            msgs: list[str] = []

            def _push(v) -> None:
                if isinstance(v, str) and v.strip():
                    msgs.append(v.strip())
                elif isinstance(v, dict):
                    for k in ("text", "content", "message"):
                        if isinstance(v.get(k), str) and v[k].strip():
                            msgs.append(v[k].strip())
                            break

            if isinstance(data, dict):
                if "messages" in data and isinstance(data["messages"], list):
                    for m in data["messages"]:
                        _push(m)
                elif "items" in data and isinstance(data["items"], list):
                    for m in data["items"]:
                        _push(m)
                else:
                    _push(data)
            elif isinstance(data, list):
                for m in data:
                    _push(m)
            return msgs, f"JSON 抽取 {len(msgs)} 条"
        return [stripped], "纯文本"

    def ingest_manual(
        self,
        text: str,
        source: str = "manual",
        from_url_name: str = "",
    ) -> dict:
        """CH-D：粘贴文本/JSON → 写入 detections（status='pending'，P3 检测流水线消费）。

        合规（D7）：只读自有/公开内容；原文入库（对外输出前的强制脱敏属 P6 案例层）。
        去重（P3-9）：① text_hash=sha256 全等 → 复用原 detection_id 并标 duplicate；
        ② 改写近重复（SimHash/字符二元组，复用 trap_engine.find_near_duplicate）
        → 不新建行，标 duplicate + near_duplicate。
        """
        items, note = self._extract_messages(text)
        if not items:
            raise ValueError("text 为空或无可抽取消息")
        conn = self._db()
        created: list[dict] = []

        # P3-9/R7（L2-B）：近重复去重需与库内既有文本比对（最近 200 条）。
        # 阈值经 configs 热更新（zhihu.ingest_near_dup_threshold，默认 0.85）；
        # 导入层禁用 hamming（hamming_max=-1），只做 overlap 判定（见模块注释）。
        recent = [
            (r["id"], r["content"])
            for r in conn.execute(
                "SELECT id, content FROM detections WHERE content IS NOT NULL "
                "ORDER BY id DESC LIMIT 200"
            ).fetchall()
            if r["content"]
        ]
        from .trap_engine import find_near_duplicate

        overlap_min = cfg_float(conn, "zhihu.ingest_near_dup_threshold", INGEST_NEAR_DUP_THRESHOLD)
        for it in items:
            h = hashlib.sha256(it.encode("utf-8")).hexdigest()
            row = conn.execute(
                "SELECT id FROM detections WHERE text_hash = ?", (h,)
            ).fetchone()
            if row:
                created.append({"detection_id": row["id"], "duplicate": True,
                                "reason": "exact_hash"})
                continue
            near_id = find_near_duplicate(recent, it, hamming_max=-1, overlap_min=overlap_min)
            if near_id is not None:
                created.append({"detection_id": near_id, "duplicate": True,
                                "reason": "near_duplicate"})
                continue
            cur = conn.execute(
                "INSERT INTO detections (source, platform, text_hash, content, status) "
                "VALUES (?, ?, ?, ?, 'pending')",
                (source, "zhihu" if source == "zhihu" else "other", h, it),
            )
            created.append({"detection_id": cur.lastrowid, "duplicate": False})
        conn.commit()
        self.upsert_session("ch-d", status="active", health="ok", last_sync=time.strftime("%Y-%m-%d %H:%M:%S"))
        return {
            "ok": True,
            "channel": "ch-d",
            "source": source,
            "from_url_name": from_url_name or None,
            "ingested": sum(1 for c in created if not c["duplicate"]),
            "duplicates": sum(1 for c in created if c["duplicate"]),
            "items": created,
            "extract_note": note,
        }

    # ------------------------------------------------------------------
    # HITL：蜜饵草稿（只产文案+指纹，永不自动发布）
    # ------------------------------------------------------------------
    @staticmethod
    def _disguise_score(bait_text: str) -> int:
        """伪装度启发式评分（0-100，确定性、可解释；P4 会接 LLM 校验）。

        基准 55；长度 12-80 得 15；含数字得 10；不含词库明示诈骗关键词得 20。
        """
        score = 55
        n = len(bait_text)
        if 12 <= n <= 80:
            score += 15
        if any(ch.isdigit() for ch in bait_text):
            score += 10
        low = bait_text.lower()
        if not any(k in low for k in _SCAM_KEYWORD_HINTS):
            score += 20
        return max(0, min(100, score))

    async def generate_trap_draft(self, *, template_id: str = "") -> dict:
        """生成蜜饵草稿（§5.3 HITL：只产文案 + 指纹 UUID + 埋设指引，不发布）。

        模板变体池离线可用（D5 降级链）；发布动作由用户手动完成，
        之后调用 mark-deployed 进入监控（P4 状态机落地）。
        """
        variants = TRAP_TEMPLATES.get(template_id) or TRAP_TEMPLATES["other"]
        bait_text = random.choice(variants)
        fingerprint = str(uuid.uuid4())
        return {
            "bait_text": bait_text,
            "fingerprint": fingerprint,
            "disguise_score": self._disguise_score(bait_text),
            "template_id": template_id or "other",
            "guidance": (
                "复制以上文案，手动发布到目标回答评论区/私信（HITL 铁律：系统不自动发布）。"
                "发布后请调用 mark-deployed 标记部署，蜜饵进入监控状态。"
            ),
            "hitl": True,
        }

    # ------------------------------------------------------------------
    # CH-A 内部实现（Playwright，惰性导入）
    # ------------------------------------------------------------------
    async def _ch_a_fetch_inbox(self) -> list[InboxItem]:
        return await self._ch_a_page_items(
            "https://www.zhihu.com/messages",
            item_selector=".List-item, [class*=MessageList] [role=listitem], .message-item",
            title_selector=".AuthorInfo-name, .UserLink-link, [class*=name]",
            text_selector=".RichText, [class*=content], [class*=text]",
        )

    async def _ch_a_fetch_comments(self, answer_url: str) -> list[InboxItem]:
        return await self._ch_a_page_items(
            answer_url,
            item_selector=".CommentItem, .List-item, [class*=CommentList] [role=listitem]",
            title_selector=".AuthorInfo-name, .UserLink-link",
            text_selector=".RichContent-inner, .RichText, [class*=comment]",
        )

    async def _ch_a_page_items(self, url: str, *, item_selector: str,
                               title_selector: str, text_selector: str) -> list[InboxItem]:
        """CH-A 通用抓取：独立浏览器 profile 会话，cookie 注入后读取页面列表。

        cookie 缺失/抓取失败 → ChannelUnavailableError（上层降级 CH-B）。
        """
        cookie_json = self.get_cookie("ch-a")
        if not cookie_json:
            raise ChannelUnavailableError("ch-a", "未注入 cookie（先调用 login 注入登录态）")
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise ChannelUnavailableError("ch-a", f"playwright 未安装: {exc}") from exc

        cookies = json.loads(cookie_json)
        headless = bool(self.settings.zhihu_ch_a_headless)
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=headless)
                try:
                    context = await browser.new_context()
                    await context.add_cookies(cookies)
                    page = await context.new_page()
                    await page.goto(url, timeout=30_000, wait_until="domcontentloaded")
                    try:
                        await page.wait_for_selector(item_selector, timeout=8_000)
                    except Exception:
                        pass  # 列表选择器变化不致命：继续尝试读取
                    items: list[InboxItem] = []
                    for i, el in enumerate(await page.query_selector_all(item_selector)):
                        title = ""
                        t_el = await el.query_selector(title_selector)
                        if t_el:
                            title = (await t_el.inner_text()).strip()
                        text = ""
                        x_el = await el.query_selector(text_selector)
                        if x_el:
                            text = (await x_el.inner_text()).strip()
                        if not text:
                            continue
                        items.append(InboxItem(
                            msg_id=f"ch-a-{hashlib.sha256(text.encode('utf-8')).hexdigest()[:12]}",
                            from_url_name=title,
                            text=text[:4000],
                            created_at=time.strftime("%Y-%m-%d %H:%M:%S"),
                            raw={"url": url, "index": i},
                        ))
                    if not items:
                        raise ChannelUnavailableError(
                            "ch-a", f"页面未解析到列表项（{url}）；可能登录态失效或页面结构变化"
                        )
                    self.backoff.record_success("ch-a")
                    self.upsert_session("ch-a", status="active", health="ok",
                                        last_sync=time.strftime("%Y-%m-%d %H:%M:%S"))
                    return items
                finally:
                    await browser.close()
        except ChannelUnavailableError:
            raise
        except Exception as exc:
            self._fail("ch-a")
            raise ChannelUnavailableError(
                "ch-a", f"CH-A 会话读取失败: {type(exc).__name__}: {exc}"
            ) from exc

    async def _ch_a_fetch_profile(self, url_name: str) -> ProfileSignals:
        """CH-A 公开页画像（备用，只读公开信息）。"""
        cookie_json = self.get_cookie("ch-a")
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise ChannelUnavailableError("ch-a", f"playwright 未安装: {exc}") from exc
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=bool(self.settings.zhihu_ch_a_headless))
                try:
                    context = await browser.new_context()
                    if cookie_json:
                        await context.add_cookies(json.loads(cookie_json))
                    page = await context.new_page()
                    await page.goto(f"https://www.zhihu.com/people/{url_name}",
                                    timeout=30_000, wait_until="domcontentloaded")
                    text = ""
                    try:
                        await page.wait_for_selector("[class*=ProfileHeader]", timeout=8_000)
                        text = (await page.inner_text("body"))[:2000]
                    except Exception:
                        text = (await page.inner_text("body"))[:1000]
                    registered = None
                    activity = 0.5 if ("关注" in text or "回答" in text) else 0.0
                    self.backoff.record_success("ch-a")
                    return ProfileSignals(
                        url_name=url_name,
                        activity_level=round(activity, 2),
                        content_consistency=0.5 if len(text) > 300 else None,
                        registered_at=registered,
                        note="ch-a 公开页抓取",
                    )
                finally:
                    await browser.close()
        except Exception as exc:
            self._fail("ch-a")
            raise ChannelUnavailableError("ch-a", f"公开页抓取失败: {type(exc).__name__}") from exc

    async def _ch_a_search(self, keyword: str) -> list[dict]:
        cookie_json = self.get_cookie("ch-a")
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise ChannelUnavailableError("ch-a", f"playwright 未安装: {exc}") from exc
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=bool(self.settings.zhihu_ch_a_headless))
                try:
                    context = await browser.new_context()
                    if cookie_json:
                        await context.add_cookies(json.loads(cookie_json))
                    page = await context.new_page()
                    await page.goto(
                        f"https://www.zhihu.com/search?type=content&q={keyword}",
                        timeout=30_000, wait_until="domcontentloaded",
                    )
                    results: list[dict] = []
                    for el in await page.query_selector_all(
                        "[class*=SearchResult], .List-item, [class*=search]"
                    ):
                        t = (await el.inner_text()).strip()[:200]
                        if t:
                            results.append({"title": t, "keyword": keyword})
                    self.backoff.record_success("ch-a")
                    return results
                finally:
                    await browser.close()
        except Exception as exc:
            self._fail("ch-a")
            raise ChannelUnavailableError("ch-a", f"搜索抓取失败: {type(exc).__name__}") from exc

    # ------------------------------------------------------------------
    # CH-B 内部实现（httpx 私有 API 模拟，备用）
    # ------------------------------------------------------------------
    def _ch_b_headers(self) -> dict:
        cookie = self.get_cookie("ch-b") or self.get_cookie("ch-a") or ""
        try:
            raw = json.loads(cookie) if cookie else []
            cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in raw)
        except (json.JSONDecodeError, KeyError):
            cookie_str = cookie
        return {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            ),
            "Cookie": cookie_str,
            "Referer": "https://www.zhihu.com/",
            "x-requested-with": "fetch",
        }

    async def _ch_b_fetch_inbox(self) -> list[InboxItem]:
        """CH-B：模拟知乎私有 API 读私信（低敏读操作）。

        403/401 或响应特征含 x-zse-96 签名校验 → 标记 degraded → 抛错降级 CH-A。
        """
        try:
            import httpx
        except ImportError as exc:
            raise ChannelUnavailableError("ch-b", f"httpx 未安装: {exc}") from exc
        url = "https://www.zhihu.com/api/v4/messages"
        try:
            async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
                resp = await client.get(url, headers=self._ch_b_headers())
        except Exception as exc:
            self._fail("ch-b")
            raise ChannelUnavailableError("ch-b", f"请求失败: {type(exc).__name__}: {exc}") from exc
        if resp.status_code in (401, 403, 429) or self._looks_signed(resp.text):
            self._mark_degraded("ch-b", f"签名校验/拒绝访问（HTTP {resp.status_code}），降级 CH-A")
            raise ChannelUnavailableError(
                "ch-b", f"x-zse-96 签名校验或拒绝访问（HTTP {resp.status_code}），已降级 CH-A"
            )
        try:
            data = resp.json()
        except Exception:
            self._fail("ch-b")
            raise ChannelUnavailableError("ch-b", "响应非 JSON，无法解析（视为通道失效）")
        items: list[InboxItem] = []
        for row in (data.get("data") or []):
            at = row.get("updated_time") or row.get("created_time") or ""
            text = (row.get("content") or row.get("message") or "").strip()
            if not text:
                continue
            items.append(InboxItem(
                msg_id=str(row.get("id") or f"ch-b-{len(items)}"),
                from_url_name=row.get("member", {}).get("name", ""),
                text=text[:4000],
                created_at=str(at),
            ))
        self.backoff.record_success("ch-b")
        self.upsert_session("ch-b", status="active", health="ok",
                            last_sync=time.strftime("%Y-%m-%d %H:%M:%S"))
        return items

    async def _ch_b_fetch_comments(self, answer_url: str) -> list[InboxItem]:
        try:
            import httpx
        except ImportError as exc:
            raise ChannelUnavailableError("ch-b", f"httpx 未安装: {exc}") from exc
        url = (answer_url or "").replace("https://www.zhihu.com", "https://www.zhihu.com/api/v4")
        if "/api/v4" not in url:
            # 兜底：评论 API 示意 endpooint（真实端点以登录态为准）
            url = f"https://www.zhihu.com/api/v4/answers/{answer_url.split('/')[-1]}/comments"
        try:
            async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
                resp = await client.get(url, headers=self._ch_b_headers())
        except Exception as exc:
            self._fail("ch-b")
            raise ChannelUnavailableError("ch-b", f"请求失败: {type(exc).__name__}") from exc
        if resp.status_code in (401, 403, 429) or self._looks_signed(resp.text):
            self._mark_degraded("ch-b", f"签名校验/拒绝访问（HTTP {resp.status_code}），降级 CH-A")
            raise ChannelUnavailableError("ch-b", "x-zse-96 签名校验，已降级 CH-A")
        items: list[InboxItem] = []
        for row in (resp.json().get("data") or []):
            text = (row.get("content") or "").strip()
            if not text:
                continue
            items.append(InboxItem(
                msg_id=str(row.get("id") or f"ch-b-{len(items)}"),
                from_url_name=(row.get("author") or {}).get("name", ""),
                text=text[:4000],
                created_at=str(row.get("created_time") or ""),
            ))
        self.backoff.record_success("ch-b")
        return items

    @staticmethod
    def _looks_signed(body: str) -> bool:
        """判断响应是否触发签名校验特征（x-zse-96 等）。"""
        head = (body or "")[:2000].lower()
        return "x-zse-96" in head or (
            ("签名" in head or "验证" in head) and ("403" in head or "拒绝" in head)
        )

    # ------------------------------------------------------------------
    # CH-C 内部实现（RSSHub 只读公开聚合）
    # ------------------------------------------------------------------
    def _rsshub_get(self, path: str) -> str:
        """同步拉取 RSSHub 公开路由（轻量调用，供公开信号补充）。"""
        import httpx

        base = (self.settings.zhihu_rsshub_base or "").strip()
        url = f"{base.rstrip('/')}/{path.lstrip('/')}"
        with httpx.Client(timeout=10.0, follow_redirects=True) as client:
            resp = client.get(url, headers={"User-Agent": "CanaryGuard/0.1 (+anti-fraud)"})
            if resp.status_code >= 400:
                raise ChannelUnavailableError("ch-c", f"RSSHub HTTP {resp.status_code}")
            return resp.text

    @staticmethod
    def _parse_rss_items(xml_text: str) -> list[dict]:
        """极简 RSS 2.0 解析（stdlib ElementTree，不引第三方 feedparser）。"""
        import xml.etree.ElementTree as ET

        items: list[dict] = []
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError:
            return items
        for item in root.iter("item"):
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            pub = (item.findtext("pubDate") or "").strip()
            if title:
                items.append({"title": title, "link": link, "pubDate": pub})
        return items

    async def _ch_c_fetch_profile(self, url_name: str) -> ProfileSignals:
        """CH-C：RSSHub zhihu people 公开动态 → 活跃度信号（只读公开，D7）。"""
        try:
            xml_text = await self._run_in_thread(self._rsshub_get, f"/zhihu/people/{url_name}")
        except Exception as exc:
            raise ChannelUnavailableError("ch-c", f"RSSHub 不可达: {type(exc).__name__}") from exc
        items = self._parse_rss_items(xml_text)
        n = len(items)
        activity = min(1.0, n / 20.0) if n else 0.0
        self.backoff.record_success("ch-c")
        return ProfileSignals(
            url_name=url_name,
            activity_level=round(activity, 2),
            content_consistency=0.8 if n >= 5 else (0.3 if n else None),
            note=f"ch-c RSSHub 公开动态 {n} 条",
        )

    async def _ch_c_search(self, keyword: str) -> list[dict]:
        """CH-C：RSSHub 热榜聚合内关键词过滤（公开内容搜索降级方案）。"""
        try:
            xml_text = await self._run_in_thread(self._rsshub_get, "/zhihu/hotlist")
        except Exception as exc:
            raise ChannelUnavailableError("ch-c", f"RSSHub 不可达: {type(exc).__name__}") from exc
        items = self._parse_rss_items(xml_text)
        matched = [it for it in items if keyword in it.get("title", "")]
        self.backoff.record_success("ch-c")
        if not matched:
            raise ChannelUnavailableError(
                "ch-c", f"热榜聚合中未命中关键词「{keyword}」"
            )
        return matched[:20]

    @staticmethod
    async def _run_in_thread(fn, *args):
        """把阻塞型（同步）IO 调用放到线程池，避免阻塞事件循环。"""
        import asyncio

        return await asyncio.to_thread(fn, *args)

    # ------------------------------------------------------------------
    # 运维快照
    # ------------------------------------------------------------------
    def status_snapshot(self) -> dict:
        """含限速器/退避/会话的综合状态（GET /zhihu/status 数据源）。"""
        sessions = {}
        for ch in CHANNELS:
            sess = self.get_session(ch)
            sessions[ch] = (
                {"status": sess["status"], "health": sess["health"],
                 "has_cookie": bool(sess.get("cookie_enc")), "last_sync": sess.get("last_sync")}
                if sess else {"status": "inactive", "health": "unknown",
                              "has_cookie": False, "last_sync": None}
            )
        return {
            "rate_limit": {
                "capacity": self.limiter.capacity,
                "available": self.limiter.available,
                "config": f"AF_ZHIHU_RATE_LIMIT={self.settings.zhihu_rate_limit}",
            },
            "backoff": self.backoff.snapshot(),
            "sessions": sessions,
        }