"""LLM Provider（OpenAI 兼容 chat completions）。

对应主方案 §6.1/§6.3 与示例 llm_provider_接口示意.py：
  1) fence()：用 <untrusted_data> 围栏包装一切外部（敌对）输入（D4），防提示词注入；
  2) 调用 OpenAI 兼容端点（DeepSeek 默认，本地 Ollama/vLLM 填同一配置即可切换）；
  3) schema 给定：解析 JSON → jsonschema 校验 → clamp_int / safe_str / 枚举白名单钳制；
  4) 失败/超时/预算超限/未配置 → 抛 DegradedError，上层走降级链（D5）；
  5) 预算护栏：daily_budget 每日上限 + 连续失败熔断（MAX_FAIL_STREAK 次 → degraded）；
  6) X1（第 3 轮）：熔断自动恢复——冷却期（llm.cooldown_seconds，默认 60s）后进入
     半开（half-open）状态，允许 1 次单飞探活：成功 → 解除熔断（state=close），
     失败 → 重新熔断并重置冷却计时（state=open）；熔断/探活/恢复写日志 + events 表
     （kind=llm_circuit_breaker，payload.state=open|half_open|close）；
  7) no_key 模式：AF_LLM_API_KEY 为空时任何调用立即降级（不动网络）；
  8) 测试注入：set_provider(FakeProvider) 替换实例。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Callable

import httpx
import jsonschema

from ..config import Settings, get_settings

FENCE_OPEN = "<untrusted_data>"
FENCE_CLOSE = "</untrusted_data>"
MAX_FAIL_STREAK = 3          # 连续失败熔断阈值
MAX_STR_LEN = 2000           # safe_str 截断上限
COOLDOWN_SECONDS = 60.0      # X1：熔断后冷却期（到期后进入半开探活）


class DegradedError(RuntimeError):
    """LLM 不可用信号：调用方应切换降级链并告警（对应接口示意 DegradedError）。"""


# ---------------------------------------------------------------------------
# 告警钩子（"熔断并告警"的落地点：上报层可订阅，如写 events 表/日志）
# ---------------------------------------------------------------------------
_degraded_hooks: list[Callable[[str, str], None]] = []


def on_degraded(callback: Callable[[str, str], None]) -> None:
    """注册 LLM 降级/失败通知回调（kind, note）。

    测试专用（R5 收尾标注）：生产无调用方注册（降级事件经 _fire_hooks 写
    events 表 + 日志），仅供 tests/test_p3_speech.py 注入断言钩子。
    """
    _degraded_hooks.append(callback)


def _fire_hooks(kind: str, note: str) -> None:
    for cb in list(_degraded_hooks):
        try:
            cb(kind, note)
        except Exception:
            pass


# X1：熔断状态机事件 —— 写日志 + events 表（kind=llm_circuit_breaker）。
# 事件写入失败（无库/无表/表不存在）一律静默，绝不影响 LLM 主流程。
def _circuit_event(state: str, note: str, **extra: Any) -> None:
    logger = logging.getLogger("canary.llm")
    if state == "open":
        logger.warning("[circuit_breaker] state=%s: %s", state, note)
    else:
        logger.info("[circuit_breaker] state=%s: %s", state, note)
    try:
        from .. import db as dbmod

        if not dbmod.db_path().exists():
            return  # 库文件不存在：不创建空库
        conn = dbmod.get_conn()
        conn.execute("SELECT 1 FROM events LIMIT 1")  # 表缺失 → 抛异常 → 静默
        payload = json.dumps({"state": state, "reason": note, **extra}, ensure_ascii=False)
        conn.execute(
            "INSERT INTO events (kind, payload) VALUES ('llm_circuit_breaker', ?)",
            (payload,),
        )
        conn.commit()
    except Exception:
        pass


class LLMProvider:
    """统一的 LLM 入口。"""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float = 30.0,
        daily_budget: int = 0,
        max_fail_streak: int | None = None,
        cooldown_seconds: float | None = None,
        config_reader: Callable[[str, Any], Any] | None = None,
    ) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.model = model or ""
        self.timeout = float(timeout or 30.0)
        self.daily_budget = int(daily_budget or 0)   # 0=不限额；>0=每日次数上限
        self.max_fail_streak = int(max_fail_streak or MAX_FAIL_STREAK)
        # X1：熔断冷却期（秒），可经 configs 表热更新覆盖（llm.cooldown_seconds）
        self.cooldown_seconds = float(cooldown_seconds or COOLDOWN_SECONDS)
        # P2-1：configs 表热更新读取钩子（llm.max_fail_streak / llm.daily_budget）
        self._cfg = config_reader if config_reader is not None else (lambda _k, d=None: d)
        self.used_today = 0                          # 当日已用次数
        self.failed_streak = 0                       # 连续失败计数
        self.degraded = False                        # 熔断标记
        self.degraded_reason = ""
        # X1：熔断自动恢复状态（冷却计时 + 半开单飞探活）
        self._tripped_at: float | None = None        # 熔断打开时间戳（冷却起点）
        self._probe_inflight = False                 # 半开探活是否在途（单飞）
        self._budget_date = time.strftime("%Y-%m-%d")  # 预算自然日（跨日自动重置，P1-3）
        self._state_lock = threading.Lock()            # 熔断/预算状态临界区（进程内线程安全）

    def _roll_budget(self) -> None:
        """自然日滚动：日期变更自动重置当日计数（P1-3 §6.3 预算护栏）。调用方需持锁。"""
        today = time.strftime("%Y-%m-%d")
        if self._budget_date != today:
            self._budget_date = today
            self.used_today = 0

    def _effective(self, key: str, default: Any) -> Any:
        """读取 configs 覆盖值（P2-1）；异常静默回退默认。"""
        try:
            return self._cfg(key, default)
        except Exception:
            return default

    # ---- fence（D4：敌对输入隔离）----

    @staticmethod
    def fence(text: str) -> str:
        """用 <untrusted_data> 围栏包装外部文本，防提示词注入。"""
        return f"{FENCE_OPEN}\n{text}\n{FENCE_CLOSE}"

    # ---- 预算/熔断 ----

    def _degrade(self, reason: str) -> DegradedError:
        self.degraded = True
        self.degraded_reason = reason
        self._tripped_at = self._tripped_at or time.time()  # 冷却起点（X1）
        _fire_hooks("llm_degraded", reason)
        return DegradedError(reason)

    @staticmethod
    def _safe_str(value: Any) -> str:
        s = str(value).strip()
        return s[:MAX_STR_LEN]

    def _clamp(self, obj: dict, schema: dict) -> dict:
        """数值钳制 + 字符串清洗 + 枚举白名单（schema 校验后的第二道防线）。"""
        props = schema.get("properties", {})
        for key, spec in props.items():
            if key not in obj:
                continue
            v = obj[key]
            if "enum" in spec and v not in spec["enum"]:
                raise DegradedError(f"LLM 输出枚举越界: {key}={v!r} (允许 {spec['enum']})")
            t = spec.get("type")
            if t == "integer":
                try:
                    obj[key] = int(v)
                except (TypeError, ValueError):
                    raise DegradedError(f"LLM 输出非整数: {key}={v!r}") from None
                if "minimum" in spec:
                    obj[key] = max(obj[key], int(spec["minimum"]))
                if "maximum" in spec:
                    obj[key] = min(obj[key], int(spec["maximum"]))
            elif t == "number":
                try:
                    obj[key] = float(v)
                except (TypeError, ValueError):
                    raise DegradedError(f"LLM 输出非数字: {key}={v!r}") from None
                if "minimum" in spec:
                    obj[key] = max(obj[key], float(spec["minimum"]))
                if "maximum" in spec:
                    obj[key] = min(obj[key], float(spec["maximum"]))
            elif t == "string":
                obj[key] = self._safe_str(v)
        return obj

    @staticmethod
    def _extract_json(content: str) -> str:
        """容忍 LLM 常用 ```json ... ``` 代码块包装。"""
        t = content.strip()
        if t.startswith("```"):
            lines = t.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            t = "\n".join(lines).strip()
        return t

    @staticmethod
    def _schema_without_bounds(schema: dict) -> dict:
        """去掉数值上下限的校验副本：越界数值交给 _clamp 钳制而非硬拒绝。

        类型 / 必填 / 枚举仍由 jsonschema 硬校验（枚举越界 = 输出不可信 → DegradedError）。
        """
        import copy

        s = copy.deepcopy(schema)
        for spec in s.get("properties", {}).values():
            if isinstance(spec, dict):
                spec.pop("minimum", None)
                spec.pop("maximum", None)
        return s

    def _validate(self, content: str, schema: dict) -> dict:
        text = self._extract_json(content)
        try:
            obj = json.loads(text)
        except json.JSONDecodeError as exc:
            raise DegradedError(f"LLM 输出非 JSON: {exc}") from exc
        try:
            jsonschema.validate(obj, self._schema_without_bounds(schema))
        except jsonschema.ValidationError as exc:
            raise DegradedError(f"LLM 输出未通过 schema 校验: {exc.message}") from exc
        return self._clamp(obj, schema)

    # ---- 统一入口 ----

    async def complete(
        self,
        *,
        system: str,
        user: str,
        schema: dict | None = None,
        temperature: float = 0,
    ) -> dict | str:
        """OpenAI 兼容 chat completions；schema 给定时做 JSON 校验与钳制。

        Returns:
            schema 非空 → 校验/钳制后的 dict；否则原始文本 str。
        Raises:
            DegradedError: 未配置/超时/调用失败/预算超限/输出非法。
        """
        # no_key 模式（D5）：配置缺失立即降级，不发起网络
        if not (self.api_key and self.model and self.base_url):
            raise self._degrade(
                "no_key 模式：AF_LLM_BASE_URL/AF_LLM_API_KEY/AF_LLM_MODEL 未配置齐"
            )
        # P2-1：configs 表热更新覆盖（llm.max_fail_streak / llm.daily_budget）
        mfs = self.max_fail_streak
        try:
            mfs = int(self._effective("llm.max_fail_streak", self.max_fail_streak))
        except (TypeError, ValueError):
            pass
        try:
            budget = int(self._effective("llm.daily_budget", self.daily_budget) or 0)
        except (TypeError, ValueError):
            budget = self.daily_budget
        with self._state_lock:
            self._roll_budget()
            if budget > 0 and self.used_today >= budget:
                # 预算超限是"每日滚动闸门"，不是永久熔断（次日自动恢复，P1-3）
                note = f"当日预算超限（上限 {budget} 次）"
                _fire_hooks("llm_budget_exceeded", note)
                raise DegradedError(note)
            if self.degraded:
                # X1：熔断自动恢复 —— 冷却期 + 半开单飞探活
                cooldown = float(self.cooldown_seconds)
                try:
                    cooldown = float(self._effective("llm.cooldown_seconds", self.cooldown_seconds))
                except (TypeError, ValueError):
                    pass
                if self._probe_inflight:
                    raise DegradedError(f"LLM 半开探活进行中，请稍后重试：{self.degraded_reason}")
                elapsed = time.time() - (self._tripped_at or time.time())
                if elapsed < cooldown:
                    remain = max(0, int(cooldown - elapsed))
                    raise DegradedError(
                        f"LLM 熔断冷却中（剩余 {remain}s）：{self.degraded_reason}"
                    )
                # 冷却期已过 → 半开：本次调用作为唯一探活（单飞），成功全开/失败重新熔断
                self._probe_inflight = True
                _circuit_event("half_open", "熔断冷却结束，进入半开探活",
                               cooldown_seconds=cooldown)

        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": self.fence(user)},   # D4：一切外部文本先 fence
        ]
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(
                    f"{self.base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": self.model,
                        "messages": messages,
                        "temperature": temperature,
                    },
                )
                resp.raise_for_status()
                content = resp.json()["choices"][0]["message"]["content"]
        except DegradedError:
            raise
        except Exception as exc:
            with self._state_lock:
                was_probe = self._probe_inflight
                self._probe_inflight = False
                self.failed_streak += 1
                note = f"调用失败(第{self.failed_streak}/{mfs}次): {type(exc).__name__}"
                tripped = self.failed_streak >= mfs
                if was_probe:
                    # 半开探活失败 → 重新熔断并重置冷却计时（X1）
                    self.degraded = True
                    self.degraded_reason = "半开探活失败，重新熔断"
                    self._tripped_at = time.time()
                    self.failed_streak = 1
                    _circuit_event("open", "半开探活失败，重新熔断", failed_streak=1)
                elif tripped:
                    self.degraded = True
                    self.degraded_reason = f"连续 {mfs} 次失败，熔断至降级链"
                    self._tripped_at = time.time()
                    _circuit_event("open", f"连续 {mfs} 次失败，熔断至降级链",
                                   failed_streak=self.failed_streak)
            _fire_hooks("llm_call_failed", note)
            if was_probe or tripped:
                raise DegradedError(self.degraded_reason)
            raise DegradedError(f"LLM 调用失败: {exc}") from exc

        with self._state_lock:
            was_degraded = self.degraded
            was_probe = self._probe_inflight
            self.failed_streak = 0
            self.used_today += 1
            self._probe_inflight = False
            if was_degraded:
                # X1：调用成功 → 解除熔断（半开探活通过 / 服务恢复）
                self.degraded = False
                self.degraded_reason = ""
                self._tripped_at = None
                _circuit_event(
                    "close",
                    "LLM 半开探活通过，熔断解除" if was_probe else "LLM 服务恢复，熔断解除",
                    failed_streak=0,
                )
        if schema is not None:
            return self._validate(content, schema)
        return content


# ---------------------------------------------------------------------------
# 生产单例 + 测试注入（P1-3）：
#   - get_provider() 返回模块级懒加载单例（按配置指纹缓存），保证熔断/预算状态
#     跨请求保留 —— 修复"每请求新建实例导致 degraded/failed_streak/used_today 清零"
#     的真实缺陷（审查报告 P1-3，D5 熔断与 §6.3 预算护栏）。
#   - set_provider(FakeProvider) 优先返回覆盖实例（测试注入，行为不变）；
#     set_provider(None) 恢复；reset_provider() 同时清空覆盖与缓存单例（测试隔离钩子）。
# ---------------------------------------------------------------------------
_provider_override: LLMProvider | None = None
_provider_lock = threading.Lock()
_cached_provider: LLMProvider | None = None
_cached_config: tuple | None = None


def _config_key(settings: Settings) -> tuple:
    """配置指纹：配置变更（env/.env 变化后 cache_clear）→ 重建单例。"""
    return (
        settings.llm_base_url or "",
        settings.llm_api_key or "",
        settings.llm_model or "",
        float(settings.llm_timeout or 30.0),
        int(settings.llm_daily_budget or 0),
    )


def set_provider(provider: LLMProvider | None) -> None:
    """注入 provider 实例（测试用）；传 None 恢复为按配置构建。"""
    global _provider_override
    _provider_override = provider


def reset_provider() -> None:
    """清空测试注入与缓存单例（测试隔离钩子，P1-3）。"""
    global _provider_override, _cached_provider, _cached_config
    with _provider_lock:
        _provider_override = None
        _cached_provider = None
        _cached_config = None


def get_provider(settings: Settings | None = None) -> LLMProvider:
    """返回当前生效的 LLM Provider（外部交互都走这里）。

    生产路径：模块级懒加载单例（按配置指纹缓存），熔断/预算状态跨请求保留。
    """
    global _cached_provider, _cached_config
    if _provider_override is not None:
        return _provider_override
    s = settings or get_settings()
    key = _config_key(s)
    with _provider_lock:
        if _cached_provider is None or _cached_config != key:
            from .config_reader import default_cfg_reader

            _cached_provider = LLMProvider(
                base_url=s.llm_base_url,
                api_key=s.llm_api_key,
                model=s.llm_model,
                timeout=s.llm_timeout,
                daily_budget=s.llm_daily_budget,
                config_reader=default_cfg_reader,   # P2-1：llm.* 覆盖值热更新
            )
            _cached_config = key
        return _cached_provider