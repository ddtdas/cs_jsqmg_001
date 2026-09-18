"""队2 社会工程学推理链测试（P11）：SEAnalyzer 规则层 / scan_text 接入 / 落库 / API 字段。

运行：pytest tests/test_p11_se_analysis.py -q（项目根，任意顺序执行）。

覆盖 DoD：
  1) Cialdini 六类心理技巧各 1 例命中（参数化 6 例）
  2) 攻击向量分类：冒充/诱饵/交换/未知（4 例）
  3) SEADM 阶段识别：关系/利用/披露/执行/完成（5 例）
  4) 生命周期识别：破冰/人设/甜头/大额/受阻/二次收割（6 例）
  5) 空文本 → unknown 全空不崩
  6) scan_text 返回含 se_analysis，结构完整
  7) detections 落库 se_factors + /detections/{id}/full 返回
  8) GET /scan/hits 条目含 se_factors
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app import db as dbmod
from app.config import get_settings
from app.services import llm_provider
from app.services.se_analysis import ALL_FACTORS, SEAnalyzer
from app.services.speech_engine import SpeechEngine


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


async def _scan(conn, text: str, source: str = "manual") -> dict:
    return await SpeechEngine().scan_text(text, source=source, conn=conn)


# ================= 1) 心理技巧六原则命中 =================

PSYCH_CASES = [
    ("urgency", "马上转账，否则来不及了"),
    ("authority", "我是公安局的，请配合调查"),
    ("scarcity", "仅此一个名额，限量专属"),
    ("social_proof", "很多人都在做，大家都已赚到钱，成功案例很多"),
    ("reciprocity", "先垫付返利，有红包和免费赠送福利"),
    ("commitment", "保证稳赚不赔，包赚包赔保本"),
]


@pytest.mark.parametrize("factor,text", PSYCH_CASES,
                         ids=[f"psych_{f}" for f, _ in PSYCH_CASES])
def test_psych_technique_hit(factor, text):
    out = SEAnalyzer().analyze(text)
    assert factor in out["psych_techniques"], f"{factor} 应命中: {text}"
    assert any(e["factor"] == factor for e in out["evidence"])
    assert 0.0 <= out["confidence"] <= 1.0


# ================= 2) 攻击向量分类 =================

def test_vector_pretexting():
    out = SEAnalyzer().analyze("我是银联客服，您的账户被清查，请转入安全账户")
    assert out["attack_vector"] == "pretexting"


def test_vector_baiting():
    out = SEAnalyzer().analyze("高回报兼职刷单，免费福利，投资理财稳赚不赔")
    assert out["attack_vector"] == "baiting"


def test_vector_quid_pro_quo():
    out = SEAnalyzer().analyze("先垫付佣金，再交手续费和保证金")
    assert out["attack_vector"] == "quid_pro_quo"


def test_vector_unknown():
    out = SEAnalyzer().analyze("我们晚上去打球，然后吃火锅")
    assert out["attack_vector"] == "unknown"
    assert out["evidence"] == []


# ================= 3) SEADM 阶段识别 =================

def test_seadm_relationship():
    out = SEAnalyzer().analyze("在吗？你好，最近辛苦了")
    assert out["seadm_stage"] == "relationship"


def test_seadm_exploitation():
    out = SEAnalyzer().analyze("需要你帮忙垫付，扫码操作下载一下")
    assert out["seadm_stage"] == "exploitation"


def test_seadm_disclosure():
    out = SEAnalyzer().analyze("请把银行卡卡号和身份证验证码发我")
    assert out["seadm_stage"] == "disclosure"


def test_seadm_execution():
    out = SEAnalyzer().analyze("请立即汇款，点击确认提交")
    assert out["seadm_stage"] == "execution"


def test_seadm_completion():
    out = SEAnalyzer().analyze("恭喜，收益已到账，提现成功")
    assert out["seadm_stage"] == "completion"


def test_seadm_takes_latest_stage():
    """多阶段命中时取推进顺序最靠后的阶段（对话已进展到披露）。"""
    out = SEAnalyzer().analyze("在吗？请把银行卡验证码发我")
    assert out["seadm_stage"] == "disclosure"


# ================= 4) 生命周期识别 =================

LIFECYCLE_CASES = [
    ("break_ice", "在吗？你好，打扰一下，看到你了"),
    ("persona", "我是资深分析师和操盘手，有内幕消息"),
    ("groom", "小额试水先转，返利体验一下"),
    ("big_invest", "现在加仓梭哈，全部重仓多投几十万"),
    ("withdraw_block", "提现失败被冻结，需要解冻交保证金"),
    ("second_harvest", "请再交验证金，补充费用续费"),
]


@pytest.mark.parametrize("stage,text", LIFECYCLE_CASES,
                         ids=[f"lifecycle_{s}" for s, _ in LIFECYCLE_CASES])
def test_lifecycle_stage(stage, text):
    out = SEAnalyzer().analyze(text)
    assert out["scam_lifecycle"] == stage, f"应识别生命周期 {stage}: {text}"


# ================= 5) 空文本 → unknown 全空不崩 =================

@pytest.mark.parametrize("blank", ["", "   ", None])
def test_empty_text_unknown(blank):
    out = SEAnalyzer().analyze(blank)
    assert out["attack_vector"] == "unknown"
    assert out["psych_techniques"] == []
    assert out["seadm_stage"] == "unknown"
    assert out["scam_lifecycle"] == "unknown"
    assert out["evidence"] == []
    assert out["confidence"] == 0.0


def test_empty_result_helper():
    out = SEAnalyzer.empty()
    assert out["attack_vector"] == "unknown" and out["evidence"] == []


def test_factor_table_consistency():
    """关键词表非空且 ALL_FACTORS 为四类之和（confidence 分母）。"""
    from app.services.se_analysis import (
        LIFECYCLE_ORDER, PSYCH_ORDER, SEADM_ORDER, VECTOR_ORDER,
    )
    assert ALL_FACTORS == PSYCH_ORDER + VECTOR_ORDER + SEADM_ORDER + LIFECYCLE_ORDER
    assert len(ALL_FACTORS) == 6 + 5 + 5 + 8


# ================= 5.5) divergence 发散推理（话术套路剧本） =================

DIVERGENCE_IOC_KEYS = ("phones", "emails", "qq", "wechat", "bank_cards")

PLAYBOOK_CASES = [
    # (文本, 期望剧本)；规则见 se_analysis._infer_playbook
    ("我是公安的，涉嫌洗钱，转安全账户马上操作", "冒充公检法剧本"),
    ("高回报兼职刷单，先垫付返利，小额试水体验一下", "刷单返利剧本"),
    ("恭喜您中奖了，先交手续费和保证金，再交验证金续费，马上操作", "中奖/退款二次收割剧本"),
    ("投资理财高回报，很多人都已赚，加仓重仓多投几十万", "投资理财剧本"),
    ("我们晚上去打球，然后吃火锅", "综合诈骗剧本"),
]


@pytest.mark.parametrize("text,expected", PLAYBOOK_CASES,
                         ids=[f"playbook_{i}" for i in range(len(PLAYBOOK_CASES))])
def test_divergence_playbook_inference(text, expected):
    """divergence.playbook 由 attack_vector+psych+lifecycle 组合推理（P2 死代码修复）。"""
    out = SEAnalyzer().analyze(text)
    assert "divergence" in out, "analyze() 应输出 divergence（供看板「话术套路」列）"
    div = out["divergence"]
    assert set(DIVERGENCE_IOC_KEYS) <= set(div["iocs"].keys()), "iocs 缺字段"
    assert isinstance(div["iocs"]["phones"], list)
    assert div["playbook"] == expected, f"剧本推理错误: {text}"
    assert 2 <= len(div["suggested_actions"]) <= 3, "suggested_actions 应为 2-3 条"
    assert all(isinstance(a, str) and a for a in div["suggested_actions"])


def test_divergence_empty_structured():
    """空文本/空结果：divergence 仍存在且为空结构（不破坏既有返回）。"""
    for blank in ("", "   ", None):
        out = SEAnalyzer().analyze(blank)
        div = out["divergence"]
        assert div["playbook"] == ""
        assert div["suggested_actions"] == []
        assert all(div["iocs"][k] == [] for k in DIVERGENCE_IOC_KEYS)
    empty = SEAnalyzer.empty()
    assert "divergence" in empty
    assert empty["divergence"]["playbook"] == ""


def test_divergence_iocs_extracted():
    """iocs 用 extract_iocs 提取（防御式导入，字段齐全）。"""
    out = SEAnalyzer().analyze("wechat abc12345 phone 13800138000 mail a@b.com card 6222020202020202")
    iocs = out["divergence"]["iocs"]
    assert "13800138000" in iocs["phones"]
    assert "a@b.com" in iocs["emails"]
    assert "abc12345" in iocs["wechat"]
    assert "6222020202020202" in iocs["bank_cards"]
    assert isinstance(iocs["qq"], list)



# ================= 6) scan_text 返回含 se_analysis =================

@pytest.mark.asyncio
async def test_scan_text_returns_se_analysis(clean_env):
    r = await _scan(clean_env, "我是银联客服，请马上把验证码发我，否则账户会被冻结")
    assert "se_analysis" in r, "scan_text 结果应含 se_analysis"
    se = r["se_analysis"]
    for key in ("attack_vector", "psych_techniques", "seadm_stage",
                "scam_lifecycle", "evidence", "confidence"):
        assert key in se, f"se_analysis 缺字段 {key}"
    assert se["attack_vector"] == "pretexting"
    assert "authority" in se["psych_techniques"]
    assert se["seadm_stage"] in (
        "relationship", "exploitation", "disclosure", "execution", "completion")
    assert isinstance(se["evidence"], list) and se["evidence"]
    for e in se["evidence"]:
        assert {"factor", "matched", "text"} <= set(e.keys())
    # 保持既有返回结构不变
    for key in ("detection_id", "verdict", "severity", "grade_hint", "hits",
                "rule_score", "judge_confidence", "llm_used", "evidence_tokens"):
        assert key in r


@pytest.mark.asyncio
async def test_scan_text_normal_keeps_se_empty(clean_env):
    """正常文本：se_analysis 存在但向量 unknown / 技巧空。"""
    r = await _scan(clean_env, "我们晚上去打球，然后吃火锅")
    assert r["se_analysis"]["attack_vector"] == "unknown"
    assert r["se_analysis"]["evidence"] == []


# ================= 7) 落库 se_factors + /full 返回 =================

@pytest.mark.asyncio
async def test_persist_writes_se_factors(clean_env):
    r = await _scan(clean_env, "我是银联客服，请马上把验证码发我，否则账户会被冻结")
    det_id = r["detection_id"]
    row = clean_env.execute(
        "SELECT se_factors FROM detections WHERE id=?", (det_id,)
    ).fetchone()
    assert row is not None and row["se_factors"]
    se = json.loads(row["se_factors"])
    assert se["attack_vector"] == "pretexting"
    assert se["confidence"] > 0


def test_detection_full_returns_se_factors(tmp_path, monkeypatch):
    monkeypatch.setenv("AF_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AF_LLM_API_KEY", "")
    get_settings.cache_clear()
    dbmod.close_all()
    llm_provider.set_provider(None)

    from app.main import app

    with TestClient(app) as client:
        r = client.post("/api/v1/scan/text", json={
            "text": "我是银联客服，请马上把验证码发我，否则账户会被冻结",
            "source": "manual",
        })
        body = r.json()
        assert body["ok"] is True
        det_id = body["data"]["detection_id"]
        # 落库断言
        conn = dbmod.get_conn()
        row = conn.execute(
            "SELECT se_factors FROM detections WHERE id=?", (det_id,)
        ).fetchone()
        assert row and json.loads(row["se_factors"])["attack_vector"] == "pretexting"

        # /detections/{id}/full 返回 se_factors（admin）
        key = (tmp_path / "bootstrap_admin_key.txt").read_text(encoding="utf-8").strip()
        r2 = client.get(f"/api/v1/detections/{det_id}/full",
                        headers={"X-API-Key": key})
        assert r2.status_code == 200
        det = r2.json()["data"]["detection"]
        assert "se_factors" in det
        assert det["se_factors"]["attack_vector"] == "pretexting"

    dbmod.close_all()
    get_settings.cache_clear()


# ================= 8) GET /scan/hits 条目含 se_factors =================

def test_scan_hits_contains_se_factors(tmp_path, monkeypatch):
    monkeypatch.setenv("AF_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AF_LLM_API_KEY", "")
    get_settings.cache_clear()
    dbmod.close_all()
    llm_provider.set_provider(None)

    from app.main import app

    with TestClient(app) as client:
        r = client.post("/api/v1/scan/text", json={
            "text": "我是银联客服，请马上把验证码发我，否则账户会被冻结",
            "source": "manual",
        })
        det_id = r.json()["data"]["detection_id"]
        r2 = client.get("/api/v1/scan/hits")
        assert r2.status_code == 200
        items = r2.json()["data"]
        assert isinstance(items, list) and items
        target = next((it for it in items if it["id"] == det_id), None)
        assert target is not None, "最近命中应包含刚扫描的记录"
        assert "se_factors" in target
        assert target["se_factors"]["attack_vector"] == "pretexting"

    dbmod.close_all()
    get_settings.cache_clear()


def test_scan_hits_se_factors_robust_when_empty(tmp_path, monkeypatch):
    """无 se_factors 数据的旧记录（坏/空 JSON）→ {} 不崩。"""
    monkeypatch.setenv("AF_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AF_LLM_API_KEY", "")
    get_settings.cache_clear()
    dbmod.close_all()
    llm_provider.set_provider(None)
    dbmod.ensure_schema()
    conn = dbmod.get_conn()
    conn.execute(
        "INSERT INTO detections (source, text_hash, content, rule_score, se_factors) "
        "VALUES ('import', 'legacy_hash_se', '旧记录无推理链', 3.0, 'not-json{')"
    )
    conn.commit()

    from app.main import app

    with TestClient(app) as client:
        r = client.get("/api/v1/scan/hits")
        assert r.status_code == 200
        items = r.json()["data"]
        assert all(isinstance(it["se_factors"], dict) for it in items)

    dbmod.close_all()
    get_settings.cache_clear()