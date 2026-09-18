"""P18 知识库蒸馏：KnowledgeDistiller 服务 + /knowledge API + 案例回写测试。

运行：cd /d 项目根 && .venv\\Scripts\\python.exe -m pytest tests/test_p18_knowledge.py -q

覆盖（17 条真实运行）：
  1) 全部端点 require_admin：未带 key → 401
  2) POST /import 单条 → 200 + distilled 结构 + 落库行
  3) import 幂等 upsert：subject+ktype 冲突 → 更新同一条
  4) POST /import-text 整段切分 → 多条 + 自动分类
  5) 分类判定：新骗术/案例复盘/线索/平台漏洞/法律/其他
  6) 脱敏：手机/QQ/银行卡 明文不落库，iocs 存掩码
  7) 危害分：ktype 默认 + 含 IOC +1 封顶 10
  8) _decay 时间衰减档位（<7d/7-30d/30-90d/>90d）
  9) refresh_decay 按 created_at 重算 decay_weight/weighted_score
 10) search 按 weighted_score 降序
 11) GET / 分页 + ktype/keyword/min_harm 过滤
 12) /harm 人工调分（1-10 钳制、无 confirm 403、gate_logs 审计）
 13) /verified 人工复核 + 审计
 14) /to-pattern 蒸馏成语条 + gate_logs + 幂等（二次 skipped）
 15) /disable 下架 + /delete 删除（confirm+reason + gate_logs）
 16) 案例发布回写：publish 后自动蒸馏知识条（ktype=案例复盘, harm 按 grade 映射）
 17) 非法入参 422（空 subject / 非法 ktype / harm 越界）
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app import db as dbmod
from app.config import get_settings
from app.main import app
from app.services import llm_provider
from app.services.cases import CaseService
from app.services.knowledge import KnowledgeDistiller

GOOD_REASON = "这是一条充分的操作理由，长度超过二十个字符用于审计说明"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """隔离数据目录（空 AF_API_KEY → 生成的 bootstrap key），服务层直连连接。"""
    monkeypatch.setenv("AF_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AF_API_KEY", "")
    monkeypatch.setenv("AF_LLM_BASE_URL", "")
    monkeypatch.setenv("AF_LLM_MODEL", "")
    monkeypatch.setenv("AF_LLM_API_KEY", "")
    get_settings.cache_clear()
    dbmod.close_all()
    llm_provider.set_provider(None)
    dbmod.ensure_schema()
    yield dbmod.get_conn()
    dbmod.close_all()
    llm_provider.set_provider(None)
    get_settings.cache_clear()


@pytest.fixture()
def api(tmp_path, monkeypatch):
    """隔离数据目录 + TestClient（lifespan 自动建表/种子/bootstrap key）。"""
    monkeypatch.setenv("AF_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AF_API_KEY", "")
    monkeypatch.setenv("AF_LLM_BASE_URL", "")
    monkeypatch.setenv("AF_LLM_MODEL", "")
    monkeypatch.setenv("AF_LLM_API_KEY", "")
    get_settings.cache_clear()
    dbmod.close_all()
    with TestClient(app) as client:
        key = (tmp_path / "bootstrap_admin_key.txt").read_text(encoding="utf-8").strip()
        yield client, key
    dbmod.close_all()
    get_settings.cache_clear()


def _auth(key: str) -> dict:
    return {"X-API-Key": key}


def _import_one(client, key, subject, content, **extra) -> dict:
    item = {"subject": subject, "content": content}
    item.update(extra)
    r = client.post("/api/v1/knowledge/import", headers=_auth(key),
                    json={"items": [item]})
    assert r.status_code == 200, r.text
    return r.json()["data"]["distilled"][0]


# ===========================================================================
# 1) require_admin
# ===========================================================================

def test_all_endpoints_401_without_key(api):
    client, _ = api
    import_body = {"items": [{"subject": "s", "content": "c"}]}
    for method, url, body in [
        ("get", "/api/v1/knowledge", None),
        ("get", "/api/v1/knowledge/search?q=x", None),
        ("post", "/api/v1/knowledge/import", import_body),
        ("post", "/api/v1/knowledge/import-text", {"text": "情报正文"}),
        ("post", "/api/v1/knowledge/1/harm", {"delta": 1, "confirm": True, "reason": GOOD_REASON}),
        ("post", "/api/v1/knowledge/1/verified", {"confirm": True, "reason": GOOD_REASON}),
        ("post", "/api/v1/knowledge/1/to-pattern",
         {"confirm": True, "reason": GOOD_REASON, "category": "other"}),
        ("post", "/api/v1/knowledge/1/disable", {"confirm": True}),
        ("delete", "/api/v1/knowledge/1", {"confirm": True, "reason": GOOD_REASON}),
    ]:
        r = client.request(method, url, json=body)
        assert r.status_code == 401, f"{method} {url}"
        assert r.json()["ok"] is False


# ===========================================================================
# 导入 / 蒸馏
# ===========================================================================

def test_import_single_item(api, env):
    client, key = api
    d = _import_one(client, key, "新型扫码领红包骗局",
                    "近期出现新型骗局：扫码领红包后被诱导下载APP，导师带单稳赚不赔",
                    ktype="新骗术", harm=9)
    assert d["ktype"] == "新骗术"
    assert d["harm_score"] == 9
    assert d["decay_weight"] == 1.0
    assert d["weighted_score"] == 9
    row = env.execute("SELECT * FROM knowledge_items WHERE id=?", (d["id"],)).fetchone()
    assert row is not None
    assert row["source"] == "manual" and row["enabled"] == 1 and row["verified"] == 0
    ev = env.execute(
        "SELECT COUNT(*) AS n FROM knowledge_events WHERE action='import'"
    ).fetchone()["n"]
    assert ev >= 1


def test_import_upsert_idempotent(api, env):
    client, key = api
    d1 = _import_one(client, key, "同主题词条", "v1 内容：疑似刷单返利新套路", ktype="线索")
    d2 = _import_one(client, key, "同主题词条", "v2 内容：更新后的刷单返利情报", ktype="线索")
    assert d2["id"] == d1["id"], "subject+ktype 冲突应更新同一条"
    rows = env.execute(
        "SELECT COUNT(*) AS n FROM knowledge_items WHERE subject='同主题词条'"
    ).fetchone()["n"]
    assert rows == 1
    row = env.execute("SELECT content FROM knowledge_items WHERE id=?", (d1["id"],)).fetchone()
    assert "v2" in row["content"]


def test_import_text_splits(api):
    client, key = api
    text = ("第一条情报：新型骗局扫码领红包，请警惕。\n"
            "第二条：平台漏洞，风控可绕过，薅羊毛教程曝光。\n\n"
            "第三条：最新法律条款更新，对诈骗处罚加重。")
    r = client.post("/api/v1/knowledge/import-text", headers=_auth(key),
                    json={"text": text, "source": "web"})
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["imported"] >= 3
    ktypes = {i["ktype"] for i in data["distilled"]}
    assert {"新骗术", "平台漏洞", "法律"} <= ktypes, ktypes


def test_classification_by_keyword(env):
    svc = KnowledgeDistiller()
    cases = [
        ("新型骗局出现请注意", "新骗术"),
        ("这是一次完整案件复盘", "案例复盘"),
        ("收到线索：疑似诈骗窝点", "线索"),
        ("APP 存在风控绕过漏洞", "平台漏洞"),
        ("刑法修订新增条款", "法律"),
        ("今天天气不错", "其他"),
    ]
    for text, expected in cases:
        d = svc.distill({"subject": "分类测试", "content": text})
        assert d["ktype"] == expected, f"{text} -> {expected}"


def test_import_masks_iocs(api, env):
    client, key = api
    d = _import_one(client, key, "泄露情报样本",
                    "嫌疑人电话 13800138000，联系 QQ 12345678，银行卡 4111111111111111",
                    ktype="线索")
    row = env.execute("SELECT content, iocs FROM knowledge_items WHERE id=?", (d["id"],)).fetchone()
    assert "13800138000" not in row["content"]
    assert "12345678" not in row["content"]
    assert "4111111111111111" not in row["content"]
    assert "****" in row["content"] or "***" in row["content"]
    iocs = json.loads(row["iocs"])
    assert {"phones", "qq", "bank_cards"} <= set(iocs.keys())
    all_iocs = [v for vals in iocs.values() for v in vals]
    assert all("*" in v for v in all_iocs), "IOC 必须为掩码值"


def test_harm_default_and_ioc_bonus(env):
    svc = KnowledgeDistiller()
    d = svc.distill({"subject": "新骗术样本", "content": "新型骗局刷单返利", "ktype": "新骗术"})
    assert d["harm_score"] == 8.0
    d2 = svc.distill({"subject": "含手机号样本", "content": "新型骗局，电话 13800138000",
                      "ktype": "新骗术", "harm": 9})
    assert d2["harm_score"] == 10.0, "9 + IOC 奖励 1 应封顶 10"
    d3 = svc.distill({"subject": "线索样本", "content": "疑似线索一条", "ktype": "线索"})
    assert d3["harm_score"] == 5.0
    d4 = svc.distill({"subject": "法律样本", "content": "法律条款说明", "ktype": "法律"})
    assert d4["harm_score"] == 3.0


# ===========================================================================
# 时效衰减 / 加权
# ===========================================================================

def test_decay_weight_brackets():
    svc = KnowledgeDistiller()
    assert svc._decay(0) == 1.0
    assert svc._decay(3) == 1.0
    assert svc._decay(6.9) == 1.0
    assert svc._decay(15) == pytest.approx(1.0 - 8 / 23 * 0.2, abs=0.02)
    assert svc._decay(60) == pytest.approx(0.65, abs=0.02)
    assert svc._decay(89) == pytest.approx(0.5 + 1 / 60 * 0.3, abs=0.02)
    assert svc._decay(120) == 0.2


def test_refresh_decay_recomputes(env):
    svc = KnowledgeDistiller()
    r = svc.import_items([{"subject": "陈旧情报", "content": "陈旧的线索一条",
                           "ktype": "线索"}], env)
    kid = r["distilled"][0]["id"]
    env.execute("UPDATE knowledge_items SET created_at='2020-01-01 00:00:00' WHERE id=?", (kid,))
    env.commit()
    n = svc.refresh_decay(env)
    assert n >= 1
    row = env.execute(
        "SELECT decay_weight, weighted_score, harm_score FROM knowledge_items WHERE id=?",
        (kid,),
    ).fetchone()
    assert float(row["decay_weight"]) == 0.2, ">90 天应衰减至 0.2"
    assert float(row["weighted_score"]) == pytest.approx(5.0 * 0.2)


# ===========================================================================
# 检索 / 列表
# ===========================================================================

def test_search_ordering_by_weighted_score(api):
    client, key = api
    _import_one(client, key, "高危新骗术", "新型骗局刷单返利扫码领红包高危预警", ktype="新骗术", harm=9)
    _import_one(client, key, "低危新骗术", "新型骗局刷单返利普通提示", ktype="新骗术", harm=2)
    r = client.get("/api/v1/knowledge/search", headers=_auth(key), params={"q": "新型骗局"})
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["total"] >= 2
    harms = [i["harm_score"] for i in data["items"]]
    assert harms == sorted(harms, reverse=True), "应按加权分降序"


def test_list_filters(api):
    client, key = api
    _import_one(client, key, "法律词条", "刑法新增条款说明", ktype="法律", harm=3)
    _import_one(client, key, "高危漏洞词条", "平台漏洞情报：风控绕过", ktype="平台漏洞", harm=8)
    r = client.get("/api/v1/knowledge", headers=_auth(key), params={"ktype": "法律"})
    assert all(i["ktype"] == "法律" for i in r.json()["data"]["items"])
    r2 = client.get("/api/v1/knowledge", headers=_auth(key), params={"min_harm": 7})
    assert all(i["harm_score"] >= 7 for i in r2.json()["data"]["items"])
    r3 = client.get("/api/v1/knowledge", headers=_auth(key), params={"keyword": "漏洞"})
    assert any("漏洞" in i["subject"] for i in r3.json()["data"]["items"])
    r4 = client.get("/api/v1/knowledge", headers=_auth(key), params={"page": 1, "page_size": 5, "sort": "created"})
    assert r4.json()["data"]["page_size"] == 5
    assert r4.json()["data"]["total"] >= 2


# ===========================================================================
# 操作：调分 / 复核 / to-pattern / 下架 / 删除
# ===========================================================================

def test_adjust_harm_api(api, env):
    client, key = api
    kid = _import_one(client, key, "调分条目", "线索内容：疑似刷单", ktype="线索")["id"]
    r = client.post(f"/api/v1/knowledge/{kid}/harm", headers=_auth(key),
                    json={"delta": 3, "confirm": True, "reason": GOOD_REASON})
    assert r.status_code == 200
    assert r.json()["data"]["harm_score"] == 8.0  # 5 + 3
    # 越界钳制到 10
    r2 = client.post(f"/api/v1/knowledge/{kid}/harm", headers=_auth(key),
                     json={"delta": 10, "confirm": True, "reason": GOOD_REASON})
    assert r2.json()["data"]["harm_score"] == 10.0
    # 无 confirm → 403
    r3 = client.post(f"/api/v1/knowledge/{kid}/harm", headers=_auth(key),
                     json={"delta": 1, "confirm": False, "reason": GOOD_REASON})
    assert r3.status_code == 403
    # 审计
    log = env.execute(
        "SELECT * FROM gate_logs WHERE action='knowledge.harm_adjust' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert log is not None and json.loads(log["after"])["harm_score"] == 10.0
    ev = env.execute(
        "SELECT COUNT(*) AS n FROM knowledge_events WHERE action='harm_adjust'"
    ).fetchone()["n"]
    assert ev >= 2


def test_set_verified(api, env):
    client, key = api
    kid = _import_one(client, key, "复核条目", "复核内容一条", ktype="其他")["id"]
    r = client.post(f"/api/v1/knowledge/{kid}/verified", headers=_auth(key),
                    json={"confirm": True, "reason": GOOD_REASON})
    assert r.status_code == 200
    assert r.json()["data"]["verified"] == 1
    r2 = client.post(f"/api/v1/knowledge/{kid}/verified", headers=_auth(key),
                     json={"confirm": False, "reason": GOOD_REASON})
    assert r2.status_code == 403
    r3 = client.post(f"/api/v1/knowledge/{kid}/verified", headers=_auth(key),
                     json={"confirm": True, "reason": "短"})
    assert r3.status_code == 422
    log = env.execute(
        "SELECT * FROM gate_logs WHERE action='knowledge.verified' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert log is not None


def test_to_pattern_link(api, env):
    client, key = api
    kid = _import_one(client, key, "新型投资骗局",
                      "新型骗局：导师带单扫码下载APP宣称稳赚不赔",
                      ktype="新骗术")["id"]
    r = client.post(f"/api/v1/knowledge/{kid}/to-pattern", headers=_auth(key),
                    json={"confirm": True, "reason": GOOD_REASON, "category": "fake_investment"})
    assert r.status_code == 200
    d = r.json()["data"]
    assert d["inserted"] is True
    assert d["category"] == "fake_investment"
    assert d["weight"] == min(10.0, 8.0)
    row = env.execute("SELECT * FROM speech_patterns WHERE id=?", (d["pattern_id"],)).fetchone()
    assert row is not None and row["source"] == "knowledge" and row["enabled"] == 1
    log = env.execute(
        "SELECT * FROM gate_logs WHERE action='knowledge.to_pattern' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert log is not None and log["action"] == "knowledge.to_pattern"
    assert "知识" in log["payload_json"] or "pattern" in log["payload_json"]
    # 幂等：同 category 再调 → skipped
    r2 = client.post(f"/api/v1/knowledge/{kid}/to-pattern", headers=_auth(key),
                     json={"confirm": True, "reason": GOOD_REASON, "category": "fake_investment"})
    assert r2.json()["data"]["inserted"] is False


def test_disable_and_delete(api, env):
    client, key = api
    kid_d = _import_one(client, key, "下架条目", "下架内容一条", ktype="线索")["id"]
    r = client.post(f"/api/v1/knowledge/{kid_d}/disable", headers=_auth(key),
                    json={"confirm": True})
    assert r.status_code == 200
    assert r.json()["data"]["enabled"] == 0
    # disable 无 confirm → 403
    r0 = client.post(f"/api/v1/knowledge/{kid_d}/disable", headers=_auth(key),
                     json={"confirm": False})
    assert r0.status_code == 403
    kid_x = _import_one(client, key, "删除条目", "删除内容一条", ktype="线索")["id"]
    r2 = client.request("delete", f"/api/v1/knowledge/{kid_x}", headers=_auth(key),
                        json={"confirm": True, "reason": GOOD_REASON})
    assert r2.status_code == 200
    assert r2.json()["data"]["deleted"] is True
    assert env.execute("SELECT COUNT(*) AS n FROM knowledge_items WHERE id=?", (kid_x,)).fetchone()["n"] == 0
    log = env.execute(
        "SELECT * FROM gate_logs WHERE action='knowledge.delete' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert log is not None and "删除条目" in log["before"]
    r3 = client.request("delete", f"/api/v1/knowledge/{kid_x}", headers=_auth(key),
                        json={"confirm": True, "reason": GOOD_REASON})
    assert r3.status_code == 404


# ===========================================================================
# 案例发布回写
# ===========================================================================

def test_case_publish_distills_knowledge(env):
    det_id = _mk_det(env, "刷单返利垫付解锁任务", grade="L5")
    row = env.execute("SELECT id FROM speech_patterns WHERE pattern='刷单返利'").fetchone()
    env.execute(
        "INSERT INTO speech_hits (det_id, pattern_id, matched_text, score) VALUES (?, ?, '刷单返利', 9.0)",
        (det_id, row["id"]),
    )
    env.commit()
    CaseService().publish(env, det_id=det_id,
                          redacted_payload="刷单返利案例脱敏内容P18",
                          graph_tags=["job_scam"])
    krow = env.execute("SELECT * FROM knowledge_items ORDER BY id DESC LIMIT 1").fetchone()
    assert krow is not None, "案例发布后应自动蒸馏出知识条目"
    assert krow["ktype"] == "案例复盘"
    assert float(krow["harm_score"]) == 8.0, "L5 → harm 8"
    assert krow["source"] == "case"
    assert "刷单返利案例脱敏内容P18" in krow["content"]
    ev = env.execute(
        "SELECT COUNT(*) AS n FROM knowledge_events WHERE action='import'"
    ).fetchone()["n"]
    assert ev >= 1


def _mk_det(env, text: str, grade: str = "L3") -> int:
    import hashlib

    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    cur = env.execute(
        "INSERT INTO detections (source, platform, text_hash, content, status, grade) "
        "VALUES ('manual', 'other', ?, ?, 'processed', ?)",
        (digest, text, grade),
    )
    return int(cur.lastrowid)


# ===========================================================================
# 非法入参
# ===========================================================================

def test_import_invalid_422(api):
    client, key = api
    r = client.post("/api/v1/knowledge/import", headers=_auth(key),
                    json={"items": [{"subject": "", "content": "x"}]})
    assert r.status_code == 422
    r2 = client.post("/api/v1/knowledge/import", headers=_auth(key),
                     json={"items": [{"subject": "s", "content": "c", "ktype": "火星分类"}]})
    assert r2.status_code == 422
    r3 = client.post("/api/v1/knowledge/import", headers=_auth(key),
                     json={"items": [{"subject": "s", "content": "c", "harm": 99}]})
    assert r3.status_code == 422