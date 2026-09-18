"""第 3 轮后端修复回归测试（backend-core，R3-X1：LLM 熔断自动恢复）。

覆盖（第 3 轮 X1）：
  1) 熔断：连续 MAX_FAIL_STREAK 次失败 → degraded=True，熔断打开写 events
     （kind=llm_circuit_breaker，state=open）与日志；
  2) 冷却期：熔断后冷却期内调用仍被拒（DegradedError 含"冷却"）；
  3) 半开恢复：冷却到期后下一次调用自动探活（half-open，单飞），成功 → 熔断解除
     （state=close），llm_used 自动回到 true，无需重启进程；
  4) 探活失败 → 重新熔断并重置冷却计时（state=open）；
  5) llm.cooldown_seconds 可热更新（configs 表 / config PUT 白名单）；
  6) 熔断/恢复均有日志（canary.llm logger，含 circuit_breaker 标记）。

运行：pytest tests/test_r3_backend_fixes.py -q（项目根）。
"""

from __future__ import annotations

import asyncio
import json
import logging

import httpx
import pytest
from fastapi.testclient import TestClient

from app import db as dbmod
from app.config import get_settings
from app.services import llm_provider
from app.services.llm_provider import DegradedError, LLMProvider

REASON_20 = "这是一条超过二十个字的敏感操作理由说明文本"
SCHEMA = {"type": "object", "properties": {"v": {"type": "integer"}}, "required": ["v"]}


class _FakeLLMResponse:
    def __init__(self, content: str) -> None:
        self._c = content

    def raise_for_status(self) -> None:
        pass

    def json(self):
        return {"choices": [{"message": {"content": self._c}}]}


class _RecoveringClient:
    """有状态假 LLM 客户端：前 fail_calls 次抛连接错误，之后按 system 提示词返回 JSON。"""

    def __init__(self, fail_calls: int) -> None:
        self.calls = 0
        self.fail_calls = fail_calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url: str, **kw):
        self.calls += 1
        if self.calls <= self.fail_calls:
            raise httpx.ConnectError("mock llm down")
        messages = kw.get("json", {}).get("messages", [])
        sysp = messages[0]["content"] if messages else ""
        if "反诈话术审查" in sysp:
            content = '{"verdict": "fraud", "severity": 8, "reason": "mock review"}'
        elif "仲裁" in sysp:
            content = '{"agree": true, "confidence": 0.9}'
        else:
            content = '{"v": 1}'
        return _FakeLLMResponse(content)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """隔离 DB + 清空 provider 单例。"""
    monkeypatch.setenv("AF_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AF_LLM_API_KEY", "")
    get_settings.cache_clear()
    dbmod.close_all()
    llm_provider.reset_provider()
    dbmod.ensure_schema()
    yield dbmod.get_conn()
    dbmod.close_all()
    llm_provider.reset_provider()
    get_settings.cache_clear()


@pytest.fixture()
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("AF_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AF_LLM_API_KEY", "")
    monkeypatch.setenv("AF_API_KEY", "")
    get_settings.cache_clear()
    dbmod.close_all()
    llm_provider.reset_provider()

    from app.main import app

    with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000)) as c:
        yield c
    dbmod.close_all()
    llm_provider.reset_provider()
    get_settings.cache_clear()


def _admin_key(tmp_path) -> str:
    return (tmp_path / "bootstrap_admin_key.txt").read_text(encoding="utf-8").strip()


def _event_count(conn, state: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM events WHERE kind='llm_circuit_breaker' AND payload LIKE ?",
        (f'%"state": "{state}"%',),
    ).fetchone()
    return int(row["n"])


# ===========================================================================
# 1) 熔断：连续失败 → degraded + 冷却期拒绝 + events(open)
# ===========================================================================


@pytest.mark.asyncio
async def test_breaker_trips_cooling_and_open_event(env, monkeypatch):
    shared = _RecoveringClient(fail_calls=10_000)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **k: shared)
    p = LLMProvider(base_url="http://x", api_key="k", model="m", cooldown_seconds=60)

    for _ in range(3):
        with pytest.raises(DegradedError):
            await p.complete(system="s", user="u", schema=SCHEMA)
    assert p.degraded is True
    assert p.failed_streak >= 3

    # 冷却期内仍被拒
    with pytest.raises(DegradedError) as e:
        await p.complete(system="s", user="u", schema=SCHEMA)
    assert "冷却" in str(e.value)

    # 熔断打开事件落库
    assert _event_count(env, "open") >= 1


# ===========================================================================
# 2) 半开恢复：冷却到期 → 单飞探活成功 → 熔断解除（events close）
# ===========================================================================


@pytest.mark.asyncio
async def test_breaker_half_open_recovers(env, monkeypatch):
    shared = _RecoveringClient(fail_calls=3)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **k: shared)
    p = LLMProvider(base_url="http://x", api_key="k", model="m", cooldown_seconds=0.15)

    for _ in range(3):
        with pytest.raises(DegradedError):
            await p.complete(system="s", user="u", schema=SCHEMA)
    assert p.degraded is True

    # 冷却期内拒绝
    with pytest.raises(DegradedError):
        await p.complete(system="s", user="u", schema=SCHEMA)

    await asyncio.sleep(0.2)  # 冷却到期
    r = await p.complete(system="s", user="u", schema=SCHEMA)  # 半开探活
    assert r == {"v": 1}
    assert p.degraded is False, "探活成功后必须解除熔断"
    assert p.failed_streak == 0
    assert p._tripped_at is None

    assert _event_count(env, "half_open") >= 1
    assert _event_count(env, "close") >= 1


# ===========================================================================
# 3) 探活失败 → 重新熔断并重置冷却计时
# ===========================================================================


@pytest.mark.asyncio
async def test_breaker_probe_failure_reopens(env, monkeypatch):
    shared = _RecoveringClient(fail_calls=10_000)  # 永远失败
    monkeypatch.setattr(httpx, "AsyncClient", lambda **k: shared)
    p = LLMProvider(base_url="http://x", api_key="k", model="m", cooldown_seconds=0.1)

    for _ in range(3):
        with pytest.raises(DegradedError):
            await p.complete(system="s", user="u", schema=SCHEMA)

    await asyncio.sleep(0.15)  # 冷却到期 → 探活失败 → 重新熔断
    with pytest.raises(DegradedError) as e:
        await p.complete(system="s", user="u", schema=SCHEMA)
    assert "探活失败" in str(e.value) or "重新熔断" in str(e.value)
    assert p.degraded is True

    # 重新熔断后进入新冷却期：紧接调用被冷却拒绝
    with pytest.raises(DegradedError) as e2:
        await p.complete(system="s", user="u", schema=SCHEMA)
    assert "冷却" in str(e2.value)

    assert _event_count(env, "open") >= 2  # 首次熔断 + 探活失败重新熔断


# ===========================================================================
# 4) 引擎级：扫描 llm_used 熔断降级 → 自动恢复（无需重启进程）
# ===========================================================================


@pytest.mark.asyncio
async def test_scan_llm_used_recovers_after_breaker(env, monkeypatch):
    shared = _RecoveringClient(fail_calls=3)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **k: shared)
    p = LLMProvider(base_url="http://x", api_key="k", model="m", cooldown_seconds=0.15)
    llm_provider.set_provider(p)

    from app.services.speech_engine import SpeechEngine

    text = "稳赚不赔，导师带你内幕消息，转账到安全账户"

    # 前 3 次扫描：每次 review 调用失败 → 连续 3 次 → 熔断
    for _ in range(3):
        r = await SpeechEngine().scan_text(text, source="manual", conn=env)
        assert r["llm_used"] is False, "失败期必须降级（纯规则）"
    assert p.degraded is True

    # 熔断中扫描仍 200 降级（不瘫）
    r4 = await SpeechEngine().scan_text(text, source="manual", conn=env)
    assert r4["llm_used"] is False

    await asyncio.sleep(0.2)  # 冷却到期
    # 服务恢复后自动回到 llm_used=true（无需重启进程）
    r5 = await SpeechEngine().scan_text(
        "稳赚不赔，导师带你内幕消息，转账到安全账户，另附一条新证据", source="manual", conn=env
    )
    assert r5["llm_used"] is True, "恢复后必须自动回到 LLM 通道"
    assert p.degraded is False

    # 熔断与恢复事件都在
    assert _event_count(env, "open") >= 1
    assert _event_count(env, "close") >= 1


# ===========================================================================
# 5) 日志：熔断/恢复均有 canary.llm 日志（circuit_breaker 标记）
# ===========================================================================


@pytest.mark.asyncio
async def test_breaker_logs_open_and_close(env, monkeypatch, caplog):
    shared = _RecoveringClient(fail_calls=3)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **k: shared)
    p = LLMProvider(base_url="http://x", api_key="k", model="m", cooldown_seconds=0.1)

    with caplog.at_level(logging.INFO, logger="canary.llm"):
        for _ in range(3):
            with pytest.raises(DegradedError):
                await p.complete(system="s", user="u", schema=SCHEMA)
        await asyncio.sleep(0.15)
        await p.complete(system="s", user="u", schema=SCHEMA)  # 探活恢复

    msgs = [r.getMessage() for r in caplog.records if "circuit_breaker" in r.getMessage()]
    assert any("state=open" in m for m in msgs), "熔断必须有 open 日志"
    assert any("state=close" in m for m in msgs), "恢复必须有 close 日志"
    assert any("half_open" in m for m in msgs), "半开探活应有日志"


# ===========================================================================
# 6) llm.cooldown_seconds 热更新（configs 表 + config PUT 白名单）
# ===========================================================================


@pytest.mark.asyncio
async def test_breaker_cooldown_hot_config(env, monkeypatch):
    from app.services.config_reader import make_reader

    env.execute(
        "INSERT OR REPLACE INTO configs (cfg_key, value, updated_at) "
        "VALUES ('llm.cooldown_seconds', '0.05', datetime('now'))"
    )
    env.commit()

    shared = _RecoveringClient(fail_calls=3)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **k: shared)
    # 构造默认冷却 999s，靠 configs 热更新覆盖为 0.05s
    p = LLMProvider(base_url="http://x", api_key="k", model="m",
                    cooldown_seconds=999, config_reader=make_reader(env))
    for _ in range(3):
        with pytest.raises(DegradedError):
            await p.complete(system="s", user="u", schema=SCHEMA)
    with pytest.raises(DegradedError) as e:
        await p.complete(system="s", user="u", schema=SCHEMA)
    assert "冷却" in str(e.value)

    await asyncio.sleep(0.1)  # 热更新冷却 0.05s 已过
    r = await p.complete(system="s", user="u", schema=SCHEMA)
    assert r == {"v": 1} and p.degraded is False


def test_cooldown_key_in_config_whitelist(api, tmp_path):
    """llm.cooldown_seconds 必须可经 config PUT 白名单写入。"""
    auth = {"X-API-Key": _admin_key(tmp_path)}
    r = api.put("/api/v1/config", json={
        "key": "llm.cooldown_seconds", "value": 30,
        "confirm": True, "reason": REASON_20,
    }, headers=auth)
    assert r.status_code == 200
    g = api.get("/api/v1/config").json()["data"]
    assert g["overrides"].get("llm.cooldown_seconds") == 30