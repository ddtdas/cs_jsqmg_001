"""P3 话术引擎 + LLM Provider 测试。

覆盖 DoD：
  1) ≥10 条已知典型诈骗话术 100% 命中（verdict 非 normal）
  2) ≥5 条正常语料 0 误报（verdict=normal、无命中、不触发 LLM）
  3) mock LLM 模式：双通道同向，LLM 复核可用
  4) 无 LLM 降级模式：no_key / mock 抛错 → 纯规则可用且结果合理
  5) D4：进 LLM 的文本必须被 fence 包装
  6) schema 校验 + 数值钳制 / 枚举白名单
  7) /scan/text API 结构 + /scan/hits 列表
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import db as dbmod
from app.config import get_settings
from app.services import llm_provider
from app.services.llm_provider import DegradedError, LLMProvider
from app.services.speech_engine import SpeechEngine

# 典型诈骗话术（杀猪盘/刷单/投资/冒充公检法/退款类）—— 必须 100% 命中
SCAM_TEXTS = [
    "稳赚不赔的理财项目，导师带你内幕消息，今天就能翻倍",
    "刷单返利，先垫付，解锁任务后日结佣金",
    "客服主动退款，点击理赔链接，双倍退款",
    "你涉嫌洗钱，尽快把资金转到安全账户配合调查",
    "请立即注销校园贷，否则影响你的征信记录",
    "网恋对象找我借钱，说家人急需用钱，让我转账",
    "包赔保本高息，投资虚拟货币翻倍收益",
    "冒充公检法的人让我交保证金到安全账户",
    "提现被冻结，交一笔解冻费才能提现，还有加密货币提现手续费",
    "足不出户打字员兼职，先缴纳保证金激活任务",
]

# 正常语料 —— 必须 0 误报
NORMAL_TEXTS = [
    "你好，明天下午三点开会，记得带笔记本",
    "这道数学题我解出来了，答案是42",
    "周末一起去爬山吧，天气不错",
    "项目进度报告已发到群里，请查收",
    "今天食堂的菜不错，我吃了两碗饭",
]


class FakeProvider(LLMProvider):
    """可编程假 LLM：正常返回复核/仲裁结果；fail=True 抛 DegradedError。"""

    def __init__(self, *, fail: bool = False,
                 review: dict | None = None, judge: dict | None = None) -> None:
        super().__init__(base_url="http://fake.invalid", api_key="k", model="m")
        self.fail = fail
        self.review = review or {"verdict": "fraud", "severity": 8, "reason": "fake"}
        self.judge = judge or {"agree": True, "confidence": 0.9}
        self.calls: list[dict] = []

    async def complete(self, *, system: str, user: str,
                       schema: dict | None = None, temperature: float = 0) -> dict | str:
        # 模拟真实 provider 的 D4 行为：进 LLM 前先 fence
        fenced = self.fence(user)
        self.calls.append({"system": system, "user": fenced})
        if self.fail:
            raise DegradedError("fake provider failure")
        if "verdict" in (schema or {}).get("properties", {}):
            return dict(self.review)
        return dict(self.judge)


@pytest.fixture()
def clean_env(tmp_path, monkeypatch):
    """隔离数据库（含 41 条种子词库）+ 重置 provider 注入。"""
    monkeypatch.setenv("AF_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AF_LLM_BASE_URL", "https://api.deepseek.com")
    monkeypatch.setenv("AF_LLM_MODEL", "deepseek-chat")
    monkeypatch.setenv("AF_LLM_API_KEY", "")
    get_settings.cache_clear()
    dbmod.close_all()
    llm_provider.set_provider(None)
    dbmod.ensure_schema()
    yield dbmod.get_conn()
    dbmod.close_all()
    llm_provider.set_provider(None)
    get_settings.cache_clear()


async def _scan(conn, text: str, source: str = "manual") -> dict:
    return await SpeechEngine().scan_text(text, source=source, conn=conn)


# ---- DoD 1：已知话术 100% 命中（纯规则模式，不依赖 LLM）----

@pytest.mark.asyncio
async def test_10_scam_texts_100pct_hit(clean_env):
    llm_provider.set_provider(FakeProvider(fail=True))   # 强制降级链，只验规则
    for text in SCAM_TEXTS:
        r = await _scan(clean_env, text)
        assert r["verdict"] in ("suspicious", "fraud"), f"未命中: {text[:20]}..."
        assert r["hits"], f"无规则命中: {text[:20]}..."
        assert r["severity"] in ("low", "medium", "high")
        assert r["llm_used"] is False


# ---- DoD 2：正常语料 0 误报 ----

@pytest.mark.asyncio
async def test_5_normal_texts_zero_false_positive(clean_env):
    fake = FakeProvider()
    llm_provider.set_provider(fake)
    for text in NORMAL_TEXTS:
        r = await _scan(clean_env, text)
        assert r["verdict"] == "normal", f"误报: {text[:20]}..."
        assert r["hits"] == []
        assert r["severity"] == "none"
        assert r["grade_hint"] == "L1"
        assert r["llm_used"] is False
    assert fake.calls == [], "正常语料不应触发 LLM 调用"


# ---- DoD 3：mock LLM 模式（双通道同向）----

@pytest.mark.asyncio
async def test_mock_llm_review_upgrades_verdict(clean_env):
    fake = FakeProvider(review={"verdict": "fraud", "severity": 9, "reason": "fake"})
    llm_provider.set_provider(fake)
    r = await _scan(clean_env, "稳赚不赔，导师带你内幕消息")
    assert r["llm_used"] is True
    assert r["verdict"] == "fraud"
    assert r["severity"] == "high"
    assert 0.0 <= r["judge_confidence"] <= 1.0
    assert len(fake.calls) == 2          # 复核 + judge
    for call in fake.calls:
        assert "<untrusted_data>" in call["user"], "进 LLM 的文本必须被 fence（D4）"


# ---- DoD 4：降级链 ----

@pytest.mark.asyncio
async def test_no_key_degrades_to_rules(clean_env):
    # AF_LLM_API_KEY 为空（fixture 已设）→ 真实 provider no_key 模式
    llm_provider.set_provider(None)
    r = await _scan(clean_env, "刷单返利，垫付解锁任务")
    assert r["llm_used"] is False
    assert r["verdict"] == "suspicious"
    assert r["judge_confidence"] > 0      # 规则权重归一化置信度
    assert r["hits"]


@pytest.mark.asyncio
async def test_failing_provider_degrades_to_rules(clean_env):
    llm_provider.set_provider(FakeProvider(fail=True))
    r = await _scan(clean_env, "你涉嫌洗钱，转到安全账户")
    assert r["llm_used"] is False
    assert r["verdict"] == "suspicious"
    assert r["hits"] and r["judge_confidence"] > 0


@pytest.mark.asyncio
async def test_no_key_provider_raises_degraded(clean_env):
    llm_provider.set_provider(None)
    provider = llm_provider.get_provider()   # 真实配置：api_key 为空
    assert provider.api_key == ""
    with pytest.raises(DegradedError):
        await provider.complete(system="s", user="u", schema=None)


# ---- DoD：schema 校验 + 钳制 ----

def test_schema_clamp_severity_99_to_10():
    p = LLMProvider(base_url="x", api_key="k", model="m")
    out = p._validate(
        '{"verdict": "suspicious", "severity": 99, "reason": "x"}',
        {
            "type": "object",
            "properties": {
                "verdict": {"type": "string", "enum": ["normal", "suspicious", "fraud"]},
                "severity": {"type": "integer", "minimum": 0, "maximum": 10},
                "reason": {"type": "string"},
            },
            "required": ["verdict", "severity", "reason"],
        },
    )
    assert out["severity"] == 10


def test_schema_enum_whitelist_rejects_bad_verdict():
    p = LLMProvider(base_url="x", api_key="k", model="m")
    with pytest.raises(DegradedError):
        p._validate(
            '{"verdict": "hacked", "severity": 5, "reason": "x"}',
            {
                "type": "object",
                "properties": {"verdict": {"type": "string", "enum": ["normal", "suspicious", "fraud"]}},
                "required": ["verdict"],
            },
        )


def test_schema_rejects_non_json():
    p = LLMProvider(base_url="x", api_key="k", model="m")
    with pytest.raises(DegradedError):
        p._validate("not json at all", {"type": "object", "properties": {}})


# ---- DoD：预算护栏 / 熔断告警 ----

def test_daily_budget_blocks_and_fires_hook():
    p = LLMProvider(base_url="x", api_key="k", model="m", daily_budget=1)
    hooks: list[str] = []
    llm_provider.on_degraded(lambda kind, note: hooks.append(f"{kind}:{note}"))
    # 直接打满预算（绕过网络）验证护栏路径
    p.used_today = 1
    with pytest.raises(DegradedError):
        # 用超短超时确保不真正发网络：budget 检查在请求前，所以不会发请求
        import asyncio
        asyncio.run(p.complete(system="s", user="u", schema=None))
    # P1-3：预算是"每日滚动闸门"而非永久熔断——degraded 必须保持 False（次日自动恢复）
    assert p.degraded is False
    assert any("llm_budget_exceeded" in h for h in hooks)


# ---- DoD 5：/scan API 实测 ----

def test_scan_api_endpoint(tmp_path, monkeypatch):
    monkeypatch.setenv("AF_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AF_LLM_API_KEY", "")
    get_settings.cache_clear()
    dbmod.close_all()
    llm_provider.set_provider(None)

    from app.main import app

    with TestClient(app) as client:
        r = client.post("/api/v1/scan/text", json={
            "text": "稳赚不赔，导师带你内幕消息",
            "source": "manual",
        })
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        data = body["data"]
        for key in ("detection_id", "verdict", "severity", "grade_hint",
                    "hits", "rule_score", "judge_confidence", "llm_used",
                    "evidence_tokens"):
            assert key in data, f"缺字段 {key}"
        assert data["verdict"] in ("suspicious", "fraud")
        assert data["detection_id"] >= 1

        # 最近命中列表
        r2 = client.get("/api/v1/scan/hits")
        assert r2.status_code == 200
        hits = r2.json()["data"]
        assert isinstance(hits, list) and len(hits) >= 1
        assert hits[0]["id"] == data["detection_id"]

        # 落库核对：detections + speech_hits
        conn = dbmod.get_conn()
        det = conn.execute("SELECT rule_score FROM detections WHERE id=?", (data["detection_id"],)).fetchone()
        assert det is not None and det["rule_score"] > 0
        sh = conn.execute("SELECT COUNT(*) AS n FROM speech_hits WHERE det_id=?", (data["detection_id"],)).fetchone()["n"]
        assert sh >= 1

    dbmod.close_all()
    get_settings.cache_clear()
