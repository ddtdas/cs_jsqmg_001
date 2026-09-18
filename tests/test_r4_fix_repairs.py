"""R4 修复回归测试（金丝雀蜜罐修复工程师第 4 轮）。

覆盖 F1/F3/F5/F6 修复项：
  F1 规则词库缺口：5 条典型诈骗话术 ≥4/5 命中（verdict≠normal 且 rule_score>0）；
     3 条正常语料 0 误报；se_analysis 关键词补全 + urgency 细化（负例不再命中）。
  F3 scan/text 匿名不送 LLM（allow_llm=False 纯规则，防刷预算）；
     accounts/check 匿名只读不落库（persisted=False）。
  F5 scan/text 纯空白 → 422 empty_text；钓鱼 URL+银行卡密话术命中。
  F6 generate-draft platform 枚举校验（非法 422 invalid_platform）；
     check-hit 退役后 409 封包 {ok:false, error:{code:'trap_retired'}}。

运行：pytest tests/test_r4_fix_repairs.py -q（项目根）。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import db as dbmod
from app.config import get_settings
from app.services import llm_provider
from app.services.llm_provider import LLMProvider
from app.services.se_analysis import SEAnalyzer
from app.services.speech_engine import SpeechEngine

# R4-F1：5 条典型诈骗话术（参考队3/队4 案例）—— 必须命中
SCAM_TEXTS_R4 = [
    "恭喜您中奖了！请先缴纳手续费和公证费，才能领取奖品",          # 中奖/手续费/公证费
    "导师带单收益30%以上，扫码下载APP注册领红包，跟单就赚",        # 投资理财类
    "您好，我是平台客服，您购买的商品可以理赔退款，请先开通备用金并开启屏幕共享",  # 冒充客服退款
    "您的账户被银联冻结，请点击链接填写验证码解冻",                # 银联冻结/链接/验证码
    "请登录钓鱼链接 www.fake-bank-verify.com 输入银行卡号和密码完成验证",  # 钓鱼 URL（队1 P2-6）
]

# R4-F1：3 条正常语料 —— 0 误报
NORMAL_TEXTS_R4 = [
    "今天天气不错，我们去公园散步吧",
    "这份季度报告周三之前需要交到领导办公室",
    "周末一起吃饭吗？我订了那家新开的川菜馆",
]


class FakeProvider(LLMProvider):
    """可编程假 LLM：按 schema 返回对应结果，记录调用次数（验证 LLM 是否被触发）。"""

    def __init__(self) -> None:
        super().__init__(base_url="http://fake.invalid", api_key="k", model="m")
        self.calls: list[dict] = []

    async def complete(self, *, system: str, user: str,
                       schema: dict | None = None, temperature: float = 0) -> dict | str:
        self.calls.append({"system": system, "user": user})
        props = (schema or {}).get("properties", {})
        if "verdict" in props:
            return {"verdict": "fraud", "severity": 8, "reason": "fake-review"}
        if "agree" in props:
            return {"agree": True, "confidence": 0.9}
        if "bait_text" in props:
            return {"bait_text": "最近有个不错的理财项目，你有兴趣的话可以聊聊", "note": "评论区"}
        if "disguise_score" in props:
            return {"disguise_score": 90, "reason": "自然"}
        return {"ok": True}


@pytest.fixture()
def clean_env(tmp_path, monkeypatch):
    """隔离数据库 + 无 LLM（降级路径）。"""
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


async def _scan(conn, text: str) -> dict:
    return await SpeechEngine().scan_text(text, source="manual", conn=conn)


# ================= F1：词库缺口 → 话术命中 =================

@pytest.mark.asyncio
async def test_f1_scam_texts_hit(clean_env):
    """5 条典型诈骗话术 ≥4/5 命中（本实现目标 5/5）。"""
    hit = 0
    for text in SCAM_TEXTS_R4:
        r = await _scan(clean_env, text)
        assert r["verdict"] != "normal", f"未命中: {text[:24]}..."
        assert r["rule_score"] > 0, f"rule_score 应为正: {text[:24]}..."
        assert r["hits"], f"无规则命中: {text[:24]}..."
        hit += 1
    assert hit >= 4, "诈骗话术命中率应 ≥4/5"


@pytest.mark.asyncio
async def test_f1_normal_texts_zero_false_positive(clean_env):
    """3 条正常语料 0 误报（verdict=normal、无命中）。"""
    for text in NORMAL_TEXTS_R4:
        r = await _scan(clean_env, text)
        assert r["verdict"] == "normal", f"误报: {text[:20]}..."
        assert r["hits"] == [], f"正常语料不应命中: {text[:20]}..."
        assert r["rule_score"] == 0


# ================= F1：se_analysis 关键词补全 / urgency 细化 =================

def test_f1_urgency_refined_no_false_positive():
    """负例『今天天气不错』不再命中 urgency；精确表达『今天就/现在马上/立刻』仍命中。"""
    out = SEAnalyzer().analyze("今天天气不错，我们去公园散步吧")
    assert "urgency" not in out["psych_techniques"], "『今天』不再触发 urgency（负例误报来源）"

    out2 = SEAnalyzer().analyze("今天就到账，现在马上转账，立刻办理")
    assert "urgency" in out2["psych_techniques"]


def test_f1_social_proof_keywords():
    out = SEAnalyzer().analyze("很多客户都赚了，别人都跟着入金，都在赚")
    assert "social_proof" in out["psych_techniques"]


def test_f1_phishing_and_exploitation_keywords():
    out = SEAnalyzer().analyze("扫码下载APP，点击链接输入网址")
    assert out["attack_vector"] == "phishing"

    out2 = SEAnalyzer().analyze("平台要求先入金10万才能继续投进去")  # 无 completion 阶段词，避免阶段后移
    assert out2["seadm_stage"] == "exploitation"


# ================= F3：匿名调用降级 =================

def _fresh_app(tmp_path, monkeypatch):
    monkeypatch.setenv("AF_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AF_LLM_BASE_URL", "https://api.deepseek.com")
    monkeypatch.setenv("AF_LLM_MODEL", "deepseek-chat")
    monkeypatch.setenv("AF_LLM_API_KEY", "k")   # LLM 已配置（验证 allow_llm 开关而非 no_key）
    get_settings.cache_clear()
    dbmod.close_all()
    llm_provider.reset_provider()
    dbmod.ensure_schema()
    from app.main import app
    return app


def test_f3_anonymous_scan_no_llm(tmp_path, monkeypatch):
    """匿名 scan/text：即使 LLM 可用也不触发（allow_llm=False 纯规则，防刷预算）。"""
    app = _fresh_app(tmp_path, monkeypatch)
    fake = FakeProvider()
    llm_provider.set_provider(fake)
    try:
        with TestClient(app) as client:
            r_anon = client.post("/api/v1/scan/text", json={
                "text": "稳赚不赔，导师带你内幕消息", "source": "manual"})
            assert r_anon.status_code == 200
            d = r_anon.json()["data"]
            assert d["llm_used"] is False, "匿名调用不得触发 LLM"
            assert d["verdict"] in ("suspicious", "fraud"), "纯规则也应命中"
            assert fake.calls == [], f"匿名调用不应产生 LLM 调用: {len(fake.calls)}"

            # 已认证调用 → 维持 LLM 复核（llm_used=True）
            key = (tmp_path / "bootstrap_admin_key.txt").read_text(encoding="utf-8").strip()
            r_auth = client.post("/api/v1/scan/text", json={
                "text": "稳赚不赔，导师带你内幕消息", "source": "manual"},
                headers={"X-API-Key": key})
            assert r_auth.status_code == 200
            da = r_auth.json()["data"]
            assert da["llm_used"] is True, "已认证调用应走 LLM 复核"
            assert len(fake.calls) == 2, "复核 + judge 共 2 次 LLM 调用"
    finally:
        llm_provider.reset_provider()
        dbmod.close_all()
        get_settings.cache_clear()


def test_f3_anonymous_account_check_no_upsert(tmp_path, monkeypatch):
    """匿名 accounts/check 只读评估不落库；已认证才 upsert 画像。"""
    app = _fresh_app(tmp_path, monkeypatch)
    try:
        with TestClient(app) as client:
            # 匿名：不落库
            r = client.post("/api/v1/accounts/check", json={
                "url_name": "anon_probe_acct",
                "signals": {"registered_at": "2026-09-01", "reported_count": 2},
            })
            assert r.status_code == 200
            b = r.json()["data"]
            assert b["risk_level"] == "red" and b["evidence"]
            assert b["persisted"] is False
            assert b["account_id"] is None, "匿名新账号不应生成 account_id（未落库）"
            r404 = client.get("/api/v1/accounts/anon_probe_acct")
            assert r404.status_code == 404, "匿名评估不应产生账号档案"

            # 已认证：upsert 画像
            key = (tmp_path / "bootstrap_admin_key.txt").read_text(encoding="utf-8").strip()
            r2 = client.post("/api/v1/accounts/check", json={
                "url_name": "anon_probe_acct",
                "signals": {"registered_at": "2026-09-01", "reported_count": 2},
            }, headers={"X-API-Key": key})
            assert r2.status_code == 200
            b2 = r2.json()["data"]
            assert b2["persisted"] is True and isinstance(b2["account_id"], int)
            r3 = client.get("/api/v1/accounts/anon_probe_acct")
            assert r3.status_code == 200
            assert r3.json()["data"]["url_name"] == "anon_probe_acct"
    finally:
        dbmod.close_all()
        get_settings.cache_clear()


# ================= F5：纯空白 422 + 钓鱼话术命中 =================

def test_f5_scan_text_whitespace_422(tmp_path, monkeypatch):
    """scan/text 纯空白 → 422 empty_text（不再 200 落库）。"""
    app = _fresh_app(tmp_path, monkeypatch)
    try:
        with TestClient(app) as client:
            r = client.post("/api/v1/scan/text", json={"text": "   ", "source": "manual"})
            assert r.status_code == 422
            assert r.json()["error"]["code"] == "empty_text"
            # 空文本（长度 0）仍走 pydantic string_too_short 422
            r2 = client.post("/api/v1/scan/text", json={"text": "", "source": "manual"})
            assert r2.status_code == 422
    finally:
        dbmod.close_all()
        get_settings.cache_clear()


def test_f5_phishing_url_text_hits(tmp_path, monkeypatch):
    """队1 P2-6 漏检话术（钓鱼 URL+银行卡密码）经 F1 词库补充后命中。"""
    app = _fresh_app(tmp_path, monkeypatch)
    try:
        with TestClient(app) as client:
            r = client.post("/api/v1/scan/text", json={
                "text": "请登录钓鱼链接 www.fake-bank-verify.com 输入银行卡号和密码完成验证",
                "source": "manual"})
            assert r.status_code == 200
            d = r.json()["data"]
            assert d["verdict"] != "normal", f"钓鱼话术必须命中，实际 {d['verdict']}"
            assert d["rule_score"] > 0
    finally:
        dbmod.close_all()
        get_settings.cache_clear()


# ================= F6：generate-draft platform 校验 + check-hit 409 封包 =================

def test_f6_generate_draft_platform_validation(tmp_path, monkeypatch):
    """generate-draft platform 限枚举：非法值 422 invalid_platform，合法值 200。"""
    app = _fresh_app(tmp_path, monkeypatch)
    try:
        with TestClient(app) as client:
            key = (tmp_path / "bootstrap_admin_key.txt").read_text(encoding="utf-8").strip()
            auth = {"X-API-Key": key}
            r_bad = client.post("/api/v1/traps/generate-draft", json={"platform": "贴吧"},
                                headers=auth)
            assert r_bad.status_code == 422
            assert r_bad.json()["error"]["code"] == "invalid_platform"

            r_ok = client.post("/api/v1/traps/generate-draft",
                               json={"platform": "评论区", "template_id": "t_invest_flow"},
                               headers=auth)
            assert r_ok.status_code == 200
            d = r_ok.json()["data"]
            assert d["status"] == "draft" and "评论区" in d["fingerprint_note"]
    finally:
        dbmod.close_all()
        get_settings.cache_clear()


def test_f6_check_hit_retired_409_envelope(tmp_path, monkeypatch):
    """check-hit 命中退役后再次检测 → 409 且封包 {ok:false, error:{code:'trap_retired'}}。"""
    app = _fresh_app(tmp_path, monkeypatch)
    try:
        with TestClient(app) as client:
            key = (tmp_path / "bootstrap_admin_key.txt").read_text(encoding="utf-8").strip()
            auth = {"X-API-Key": key}
            bait = "你好，我有个不错的兼职项目，加微信聊聊怎么样"
            r1 = client.post("/api/v1/traps", json={"bait_text": bait}, headers=auth)
            tid = r1.json()["data"]["id"]
            client.post(f"/api/v1/traps/{tid}/deploy", json={}, headers=auth)

            # 命中 → 200 hit:true（active→monitored→hit→retired）
            r2 = client.post(f"/api/v1/traps/{tid}/check-hit", json={"text": bait})
            assert r2.status_code == 200 and r2.json()["data"]["hit"] is True

            # 已退役再次检测 → 409，封包 ok:false + error.code=trap_retired
            r3 = client.post(f"/api/v1/traps/{tid}/check-hit", json={"text": bait})
            assert r3.status_code == 409
            body = r3.json()
            assert body["ok"] is False, "409 必须 ok:false（统一封包约定）"
            assert body["error"]["code"] == "trap_retired"
    finally:
        dbmod.close_all()
        get_settings.cache_clear()
