"""P6 证据包 + 脱敏 + 案例库测试。

覆盖 DoD：
  1) 证据包哈希链：冻结后篡改内容 → verify 失败（哈希不可变）
  2) 脱敏正则强制抹除：手机/身份证/姓名/邮箱/链接/微信号 + replaced 记录
  3) 含敏感信息入案例库被拒（代码层强制）
  4) 图谱数据可渲染（by_tag/by_grade 结构化聚合）
  5) 举报模板不含冒充官方措辞（无"已冻结/已拦截"，个人署名）
  6) /evidence/build→freeze→export 与 /cases/publish→graph API 跑通
"""

from __future__ import annotations

import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from app import db as dbmod
from app.config import get_settings
from app.services import llm_provider
from app.services.cases import CaseService
from app.services.desensitize import DesensitizeService
from app.services.evidence import EvidenceService
from app.utils import ApiError


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AF_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AF_LLM_API_KEY", "")
    get_settings.cache_clear()
    dbmod.close_all()
    llm_provider.set_provider(None)
    dbmod.ensure_schema()
    yield dbmod.get_conn()
    dbmod.close_all()
    llm_provider.set_provider(None)
    get_settings.cache_clear()


def _mk_det(conn, *, content: str, rule_score: float = 9.0,
            llm_verdict: str | None = "fraud", hits: list[tuple[str, float]] | None = None) -> int:
    text = content or "test"
    cur = conn.execute(
        "INSERT INTO detections (source, text_hash, content, rule_score, llm_verdict, judge_confidence, grade, status) "
        "VALUES ('manual', ?, ?, ?, ?, 0.9, 'L4', 'processed')",
        (hashlib.sha256(text.encode()).hexdigest(), text, rule_score, llm_verdict),
    )
    det_id = int(cur.lastrowid)
    for pattern, score in (hits or [("稳赚不赔", 9.0)]):
        row = conn.execute("SELECT id FROM speech_patterns WHERE pattern=? LIMIT 1", (pattern,)).fetchone()
        conn.execute(
            "INSERT INTO speech_hits (det_id, pattern_id, matched_text, score) VALUES (?,?,?,?)",
            (det_id, row["id"], pattern, score),
        )
    conn.commit()
    return det_id


# ================= 证据包：哈希链 / 冻结 / 篡改 =================

def test_evidence_build_chain_structure(env):
    det_id = _mk_det(env, content="稳赚不赔的理财项目请联系我")
    r = EvidenceService().build(env, [det_id], screenshots=["var/shot1.png"])
    assert r["frozen"] == 0
    chain = r["hash_chain"]
    assert len(chain) == 2  # detection + screenshot
    prev = ""
    for node in chain:
        assert node["prev_hash"] == prev
        assert len(node["hash"]) == 64 and len(node["content_sha256"]) == 64
        prev = node["hash"]
    assert prev == chain[-1]["hash"]


def test_evidence_verify_ok_before_freeze(env):
    det_id = _mk_det(env, content="投资理财高回报")
    svc = EvidenceService()
    pkg = svc.build(env, [det_id])
    ver = svc.verify(env, pkg["pkg_id"])
    assert ver["intact"] is True and ver["frozen"] is False


def test_evidence_freeze_then_tamper_fails(env):
    det_id = _mk_det(env, content="稳赚不赔的理财项目")
    svc = EvidenceService()
    pkg = svc.build(env, [det_id])
    # 冻结前篡改 → 校验失败（内容与链不一致）
    env.execute("UPDATE detections SET content='被篡改的内容' WHERE id=?", (det_id,))
    env.commit()
    assert svc.verify(env, pkg["pkg_id"])["intact"] is False
    # 恢复原内容
    env.execute("UPDATE detections SET content='稳赚不赔的理财项目' WHERE id=?", (det_id,))
    env.commit()
    assert svc.verify(env, pkg["pkg_id"])["intact"] is True

    # 冻结
    frozen = svc.freeze(env, pkg["pkg_id"])
    assert frozen["frozen"] is True and frozen["intact"] is True
    # 冻结后篡改 → 校验失败（哈希不可变，存证核心）
    env.execute("UPDATE detections SET content='冻结后我改了内容' WHERE id=?", (det_id,))
    env.commit()
    ver = svc.verify(env, pkg["pkg_id"])
    assert ver["intact"] is False and ver["frozen"] is True
    # 重复冻结被拒
    with pytest.raises(ApiError) as e:
        svc.freeze(env, pkg["pkg_id"])
    assert e.value.code == "already_frozen"


def test_evidence_export_json_and_text(env):
    det_id = _mk_det(env, content="刷单返利垫付")
    svc = EvidenceService()
    pkg = svc.build(env, [det_id])
    js = svc.export(env, pkg["pkg_id"], "json")
    assert js["fmt"] == "json" and js["content"]["pkg_id"] == pkg["pkg_id"]
    assert js["content"]["hash_chain"] and js["verified"]["intact"]
    tx = svc.export(env, pkg["pkg_id"], "text")
    assert tx["fmt"] == "text"
    assert "证据包" in tx["content"] and "链节点" in tx["content"]


# ================= 举报模板合规（D7） =================

@pytest.mark.parametrize("kind", ["96110", "platform", "pibyao"])
def test_report_template_compliance(env, kind):
    det_id = _mk_det(env, content="我叫张三丰，电话13812345678，稳赚不赔")
    svc = EvidenceService()
    pkg = svc.build(env, [det_id])
    tpl = svc.report_template(env, pkg["pkg_id"], kind)
    text = tpl["text"]
    # 个人署名 + 引导
    assert "普通用户（个人）" in text
    # 不得出现冒充官方/处置措辞
    for bad in ("已冻结", "已拦截", "我们已", "本平台已", "官方已"):
        assert bad not in text, f"模板出现冒充措辞: {bad}"
    # 模板内不得泄漏脱敏前的敏感信息
    assert "13812345678" not in text and "张三丰" not in text
    # 非法 kind
    with pytest.raises(ApiError):
        svc.report_template(env, pkg["pkg_id"], "evil")


# ================= 脱敏 =================

def test_desensitize_regex_masks_all_types(env):
    raw = (
        "我叫张三丰，电话13812345678，身份证110101199001011234，"
        "邮箱zhangsan@example.com，网址https://t.cn/Abc123，微信号:wxid_abc12345"
    )
    svc = DesensitizeService()
    r = svc.regex_redact(raw)
    out = r["redacted"]
    assert "13812345678" not in out
    assert "110101199001011234" not in out
    assert "张三丰" not in out
    assert "zhangsan@example.com" not in out
    assert "https://t.cn/Abc123" not in out
    assert "wxid_abc12345" not in out
    # replaced 记录 {from,to}
    kinds = {x["from"] for x in r["replaced"]}
    assert {"13812345678", "张三丰", "110101199001011234"} <= kinds
    # 脱敏后应无残留
    assert DesensitizeService.scan_sensitive(out) == []


def test_desensitize_llm_degrade_no_key(env):
    """无 LLM key → 降级：正则结果直接可用（layer=regex）。"""
    svc = DesensitizeService()
    r = svc.regex_redact("请联系我 13812345678")
    assert r["redacted"] == "请联系我 138****5678"
    assert r["layer"] == "regex"


def test_desensitize_scan_sensitive_detects(env):
    hits = DesensitizeService.scan_sensitive("电话 13812345678")
    assert hits and hits[0]["type"] == "phone"
    assert DesensitizeService.scan_sensitive("今天天气很好") == []


# ================= 案例库：强制脱敏 / 图谱 =================

def test_cases_publish_rejects_sensitive(env):
    det_id = _mk_det(env, content="稳赚不赔")
    svc = CaseService()
    # 含手机号 → 拒绝入库（代码层强制）
    with pytest.raises(ApiError) as e:
        svc.publish(env, det_id=det_id, redacted_payload="加我微信 13812345678")
    assert e.value.code == "sensitive_data"
    assert env.execute("SELECT COUNT(*) AS n FROM cases").fetchone()["n"] == 0


def test_cases_publish_after_desensitize_ok(env):
    det_id = _mk_det(env, content="稳赚不赔的理财项目，联系人 13812345678")
    svc = CaseService()
    d = DesensitizeService().regex_redact("稳赚不赔的理财项目，联系人 13812345678")
    case = svc.publish(env, det_id=det_id, redacted_payload=d["redacted"],
                       desensitize_log=d["replaced"])
    assert case["redacted_payload"] == "稳赚不赔的理财项目，联系人 138****5678"
    assert case["graph_tags"]  # 自动推导标签


def test_cases_graph_aggregation(env):
    svc = CaseService()
    d1 = _mk_det(env, content="稳赚不赔")
    d2 = _mk_det(env, content="刷单返利")
    d3 = _mk_det(env, content="投资理财高回报")
    svc.publish(env, det_id=d1, redacted_payload="稳赚不赔案例", graph_tags=["fake_investment"])
    svc.publish(env, det_id=d2, redacted_payload="刷单返利案例", graph_tags=["job_scam"])
    svc.publish(env, det_id=d3, redacted_payload="高回报案例", graph_tags=["fake_investment"])
    g = svc.graph(env)
    assert g["total"] == 3
    by_tag = {x["name"]: x["value"] for x in g["by_tag"]}
    assert by_tag["fake_investment"] == 2 and by_tag["job_scam"] == 1
    assert isinstance(g["by_grade"], list) and all({"name", "value"} <= set(x) for x in g["by_grade"])
    # 分页查询
    lst = svc.list_cases(env, page=1, page_size=2, tag="fake_investment")
    assert lst["total"] == 2 and len(lst["items"]) == 2


# ================= API 集成 =================

def test_p6_api_integration(tmp_path, monkeypatch):
    monkeypatch.setenv("AF_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AF_LLM_API_KEY", "")
    get_settings.cache_clear()
    dbmod.close_all()
    llm_provider.set_provider(None)

    from app.main import app

    with TestClient(app) as client:
        # 写操作（build/freeze/export/publish）需 admin（P1-1）；key 于 lifespan 启动时生成
        key = (tmp_path / "bootstrap_admin_key.txt").read_text(encoding="utf-8").strip()
        auth = {"X-API-Key": key}
        reason = "这是一条超过二十个字的证据案例发布理由说明文本"

        # 建一条检测（扫描真实诈骗话术）
        r = client.post("/api/v1/scan/text", json={"text": "稳赚不赔的理财项目，导师带你内幕消息"})
        det_id = r.json()["data"]["detection_id"]

        # evidence build → get → freeze → export → report-template
        r1 = client.post("/api/v1/evidence/build", json={"det_ids": [det_id], "screenshots": ["var/shot.png"]}, headers=auth)
        assert r1.status_code == 200
        pkg_id = r1.json()["data"]["pkg_id"]
        assert len(r1.json()["data"]["hash_chain"]) == 2

        r2 = client.get(f"/api/v1/evidence/{pkg_id}", headers=auth)
        assert r2.json()["data"]["verified"]["intact"] is True

        r3 = client.post(f"/api/v1/evidence/{pkg_id}/freeze",
                         json={"confirm": True, "reason": reason}, headers=auth)
        assert r3.json()["data"]["frozen"] is True

        r4 = client.get(f"/api/v1/evidence/{pkg_id}/export", params={"fmt": "json"}, headers=auth)
        assert r4.json()["data"]["content"]["frozen"] is True

        r4b = client.get(f"/api/v1/evidence/{pkg_id}/export", params={"fmt": "text"}, headers=auth)
        assert r4b.json()["data"]["fmt"] == "text"

        r5 = client.get(f"/api/v1/evidence/{pkg_id}/report-template", params={"kind": "96110"})
        text = r5.json()["data"]["text"]
        assert "普通用户（个人）" in text and "已冻结" not in text and "138" not in text

        # cases publish（先脱敏）→ list → graph
        # 直接发布含敏感 → 拒绝（先过 confirm+reason 闸控，再被脱敏校验拦下）
        r6 = client.post("/api/v1/cases/publish", json={
            "det_id": det_id, "redacted_payload": "加我微信 13812345678",
            "confirm": True, "reason": reason,
        }, headers=auth)
        assert r6.status_code == 422
        assert r6.json()["error"]["code"] == "sensitive_data"

        # 脱敏后发布
        d = DesensitizeService().regex_redact("加我微信 13812345678，稳赚不赔")
        r7 = client.post("/api/v1/cases/publish", json={
            "det_id": det_id,
            "redacted_payload": d["redacted"],
            "desensitize_log": d["replaced"],
            "graph_tags": ["fake_investment"],
            "confirm": True,
            "reason": reason,
        }, headers=auth)
        assert r7.status_code == 200
        case_id = r7.json()["data"]["id"]

        # gate_logs 审计（P1-2）
        log = dbmod.get_conn().execute("SELECT * FROM gate_logs WHERE action='case.publish'").fetchone()
        assert log is not None and log["reason"] == reason and len(log["payload_hash"]) == 64

        r8 = client.get("/api/v1/cases", params={"tag": "fake_investment"})
        assert r8.json()["data"]["total"] >= 1

        r9 = client.get("/api/v1/cases/graph")
        g = r9.json()["data"]
        assert g["total"] >= 1 and any(x["name"] == "fake_investment" for x in g["by_tag"])

    dbmod.close_all()
    get_settings.cache_clear()


# ================= C14：se_factors（推理链）进入证据哈希链 =================

def test_evidence_detection_items_include_se_factors(env):
    """C14：_detection_items 每条检测项携带 se_factors（解析为 dict；无/坏 JSON → {}）。"""
    det_id = _mk_det(env, content="稳赚不赔")
    env.execute(
        "UPDATE detections SET se_factors=? WHERE id=?",
        (json.dumps({"attack_vector": "pretexting", "steps": ["打招呼", "上钩"]}, ensure_ascii=False), det_id),
    )
    env.commit()
    items = EvidenceService()._detection_items(env, [det_id])
    assert items[0]["se_factors"] == {"attack_vector": "pretexting", "steps": ["打招呼", "上钩"]}
    # 原返回结构保持不变
    assert items[0]["content"] == "稳赚不赔" and items[0]["grade"] == "L4"
    # 空字符串 → {}（se_factors 列 NOT NULL DEFAULT '{}'，DB 侧不接受 NULL）
    env.execute("UPDATE detections SET se_factors='' WHERE id=?", (det_id,))
    env.commit()
    assert EvidenceService()._detection_items(env, [det_id])[0]["se_factors"] == {}
    # 坏 JSON → {}
    env.execute("UPDATE detections SET se_factors='not-json{{' WHERE id=?", (det_id,))
    env.commit()
    assert EvidenceService()._detection_items(env, [det_id])[0]["se_factors"] == {}


def test_evidence_chain_covers_se_factors(env):
    """C14：se_factors 进入哈希链——改推理链内容 → verify 失败（篡改即失证）。"""
    det_id = _mk_det(env, content="稳赚不赔")
    env.execute(
        "UPDATE detections SET se_factors=? WHERE id=?",
        (json.dumps({"attack_vector": "pretexting"}, ensure_ascii=False), det_id),
    )
    env.commit()
    svc = EvidenceService()
    pkg = svc.build(env, [det_id])
    assert svc.verify(env, pkg["pkg_id"])["intact"] is True
    # 仅篡改推理链（content 不变）→ 链校验失败
    env.execute(
        "UPDATE detections SET se_factors=? WHERE id=?",
        (json.dumps({"attack_vector": "baiting"}, ensure_ascii=False), det_id),
    )
    env.commit()
    ver = svc.verify(env, pkg["pkg_id"])
    assert ver["intact"] is False