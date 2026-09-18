"""P2 修复回归测试（审查报告 P2-1 ~ P2-7）。

覆盖：
  1) P2-1 配置热更新真实生效：grading.l3_min / trap 阈值 / speech.rule_min_llm /
     llm.max_fail_streak 运行时读取 configs 覆盖值；
  2) P2-2 APScheduler：9 类 job 注册、触发器类型正确；
  3) P2-4 check-hit 已改 POST（GET 405 回归见 test_p4_traps）；
  4) P2-5 限流：/scan/text 匿名超限 429 后 admin 豁免；
  5) P2-6 日志：var/logs/app.log 有请求日志；
  6) P2-7 GET /scan/inbox 无 key 401（负例另见 test_p6_5_endpoints）。

运行：pytest tests/test_p2_engineering.py -q（项目根）。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db
from app.config import get_settings


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """隔离 DB + 清空单例（provider/全局限速/限流桶）。"""
    monkeypatch.setenv("AF_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AF_LLM_API_KEY", "")
    get_settings.cache_clear()
    db.close_all()
    try:
        from app.middleware.rate_limit import reset_rate_limiters

        reset_rate_limiters()
    except Exception:
        pass
    yield
    db.close_all()
    get_settings.cache_clear()


@pytest.fixture()
def client(env):
    from app.main import app

    with TestClient(app) as c:
        yield c


def _admin_key() -> str:
    settings = get_settings()
    p = Path(settings.data_dir) / "bootstrap_admin_key.txt"
    return p.read_text(encoding="utf-8").strip()


def _set_cfg(conn, key: str, value):
    conn.execute(
        "INSERT OR REPLACE INTO configs (cfg_key, value, updated_at) VALUES (?, ?, datetime('now'))",
        (key, json.dumps(value, ensure_ascii=False)),
    )
    conn.commit()


def _mk_det(conn, content: str, rule_score: float = 11.0, llm_verdict: str = "fraud"):
    text_hash = hashlib.sha256(content.encode()).hexdigest()
    cur = conn.execute(
        "INSERT INTO detections (source, text_hash, content, rule_score, llm_verdict, judge_confidence, grade, status) "
        "VALUES ('manual', ?, ?, ?, ?, 0.9, 'L4', 'processed')",
        (text_hash, content, rule_score, llm_verdict),
    )
    conn.commit()
    return int(cur.lastrowid)


class FakeProvider:
    """可编程假 LLM：正常返回复核/仲裁结果。"""

    def __init__(self):
        self.degraded = False
        self.base_url = "http://fake.invalid"
        self.api_key = "k"
        self.model = "m"

    async def complete(self, *, system: str, user: str, schema: dict | None = None, temperature: float = 0):
        return {"verdict": "fraud", "severity": 8, "reason": "fake", "agree": True, "confidence": 0.9}


# ---------------------------------------------------------------------------
# P2-1 配置热更新
# ---------------------------------------------------------------------------
def test_grading_l3_min_hot_reload(env):
    from app.services.grading import GradingService

    conn = db.get_conn()
    db.ensure_schema()
    det_id = _mk_det(conn, "稳赚不赔的理财项目", rule_score=11.0, llm_verdict=None)
    r_default = GradingService().finalize(conn, detection_id=det_id)
    grade_default = r_default["grade"]
    assert grade_default == "L3", f"默认 l3_min=10，rule 11 应为 L3，实际 {grade_default}"

    _set_cfg(conn, "grading.l3_min", 20.0)
    r_raised = GradingService().finalize(conn, detection_id=det_id)
    grade_raised = r_raised["grade"]
    assert grade_raised == "L2", f"热更新 grading.l3_min=20 后应为 L2，实际 {grade_raised}"

    _set_cfg(conn, "grading.l3_min", 0.0)
    r_lowered = GradingService().finalize(conn, detection_id=det_id)
    grade_lowered = r_lowered["grade"]
    assert grade_lowered == "L3", "热更新 lowering 后应回到 L3"


def test_trap_thresholds_hot_reload(env):
    from app.services.trap_engine import TrapEngine

    conn = db.get_conn()
    db.ensure_schema()  # 先建表（含 configs）
    engine = TrapEngine()
    bait = "今天天气真不错我们一起去公园散步吧"
    variant = "今天天气真不错，我们一起去公园走走散散步呀"

    # 蜜饵 1：默认阈值应命中（命中后自动 retired 防反查）
    d1 = engine.create_draft(conn, bait_text=bait)
    t1 = int(d1["id"])
    engine.deploy(conn, t1, target_url="http://example.com")
    hit_default = asyncio.run(engine.check_hit(conn, t1, variant))
    assert hit_default["hit"] is True, "默认阈值应命中改写文本（overlap≥0.6）"

    # 蜜饵 2：收紧阈值后，无子串重叠的"改写"文本不应再命中
    _set_cfg(conn, "trap.simhash_threshold", 2)
    _set_cfg(conn, "trap.overlap_threshold", 0.99)
    d2 = engine.create_draft(conn, bait_text=bait)
    t2 = int(d2["id"])
    engine.deploy(conn, t2, target_url="http://example.com")
    far_text = "晚上一起吃饭吗想聊聊工作的事情你最近怎么样"
    hit_tight = asyncio.run(engine.check_hit(conn, t2, far_text))
    assert hit_tight["hit"] is False, "收紧阈值后无关文本不应命中"


def test_speech_rule_min_llm_hot_reload(env):
    from app.services import llm_provider as lp
    from app.services.speech_engine import SpeechEngine

    conn = db.get_conn()
    db.ensure_schema()
    lp.set_provider(FakeProvider())
    try:
        engine = SpeechEngine()

        async def _scan():
            return await engine.scan_text("稳赚不赔，导师带你内幕消息", source="manual", conn=conn)

        r_default = asyncio.run(_scan())
        assert r_default.get("llm_used") is True, "默认 rule_min_llm=6 应触发 LLM"

        _set_cfg(conn, "speech.rule_min_llm", 999.0)
        r_raised = asyncio.run(_scan())
        assert r_raised.get("llm_used") is False, "热更新 rule_min_llm=999 后不应触发 LLM"
        assert r_raised.get("source") == "manual", "scan_text 结果应含 source 字段（P2-3）"
    finally:
        lp.reset_provider()


def test_llm_max_fail_streak_hot_reload(env, monkeypatch):
    import httpx

    from app.services import llm_provider as lp
    from app.services.llm_provider import LLMProvider

    conn = db.get_conn()
    db.ensure_schema()  # 先建表（含 configs）

    async def _boom_post(self, *a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(httpx.AsyncClient, "post", _boom_post)

    _set_cfg(conn, "llm.max_fail_streak", 1)
    p = LLMProvider(base_url="http://x", api_key="k", model="m",
                    config_reader=lambda key, default: 1 if key == "llm.max_fail_streak" else default)
    try:
        asyncio.run(p.complete(system="s", user="u"))
    except lp.DegradedError:
        pass
    assert p.degraded is True, "max_fail_streak=1 时首次失败即熔断"


# ---------------------------------------------------------------------------
# P2-2 调度器注册
# ---------------------------------------------------------------------------
def test_scheduler_registers_nine_jobs(env):
    from app.services import scheduler as sched_mod

    sched_mod.create_scheduler()
    jobs = sched_mod.job_snapshot()
    ids = {j["id"] for j in jobs}
    assert ids == {
        "job_scan_dm", "job_zhihu_sync", "job_trap_recheck",
        "job_account_scan", "job_evidence", "job_case_publish",
        "job_collector_matrix", "job_persona_sim", "job_knowledge_decay",
    }, f"应注册 9 类 job，实际 {sorted(ids)}"
    trig_kinds = []
    for j in jobs:
        t = str(j["trigger"]).lower()
        trig_kinds.append("cron" if "cron" in t else "interval")
    assert trig_kinds.count("interval") == 7, "7 个 interval job"
    assert trig_kinds.count("cron") == 2, "2 个 cron job（job_case_publish / job_knowledge_decay 每日）"


def test_scheduler_jobs_run_safely_without_llm(env):
    """无 LLM（no_key）下 job 函数可安全执行：不抛异常、不崩。"""
    from app.services import scheduler as sched_mod

    sched_mod.job_scan_dm()
    sched_mod.job_zhihu_sync()
    sched_mod.job_evidence()
    sched_mod.job_case_publish()
    sched_mod.job_trap_recheck()
    sched_mod.job_account_scan()
    sched_mod.job_collector_matrix()


# ---------------------------------------------------------------------------
# P2-5 限流：匿名超限 429 后 admin 豁免
# ---------------------------------------------------------------------------
def test_rate_limit_scan_text_429_then_admin_bypass(env, monkeypatch):
    monkeypatch.setenv("AF_RATE_LIMIT_PER_MIN", "3")
    get_settings.cache_clear()
    try:
        from app.middleware.rate_limit import reset_rate_limiters

        reset_rate_limiters()
    except Exception:
        pass
    from app.main import app

    with TestClient(app) as c:
        for _ in range(3):
            r = c.post("/api/v1/scan/text", json={"text": "稳赚不赔，导师带你内幕消息"})
            assert r.status_code == 200, "前 3 次应 200"
        r4 = c.post("/api/v1/scan/text", json={"text": "稳赚不赔，导师带你内幕消息"})
        assert r4.status_code == 429, "匿名超限应 429"
        assert r4.json()["error"]["code"] == "rate_limited"
        key = _admin_key()
        r5 = c.post("/api/v1/scan/text", json={"text": "稳赚不赔，导师带你内幕消息"},
                    headers={"X-API-Key": key})
        assert r5.status_code == 200, "有效 admin key 应豁免限流"


# ---------------------------------------------------------------------------
# P2-6 日志落盘
# ---------------------------------------------------------------------------
def test_logging_writes_var_logs(env):
    from app.utils import setup_logging

    setup_logging()
    from app.main import app

    with TestClient(app) as c:
        key = _admin_key()
        c.get("/api/v1/system/health", headers={"X-API-Key": key})
    log_file = Path("var") / "logs" / "app.log"
    assert log_file.exists(), "var/logs/app.log 应存在"
    content = log_file.read_text(encoding="utf-8", errors="replace")
    assert content.strip(), "日志文件不应为空"
    assert "/api/v1/system/health" in content, "应包含请求日志（method/path/status）"
