"""P17 话术库适应更新：speech-patterns 管理 API + 在线学习回写测试。

运行：cd /d 项目根 && .venv\\Scripts\\python.exe -m pytest tests/test_p17_speech_lib.py -q

覆盖（21 条真实运行）：
  1) 全部端点 require_admin：未带 key → 401（ok=False）
  2) GET 列表 200：total=种子条数、items 含 hit_count 等全字段
  3) GET 分页 + category/keyword 过滤生效
  4) POST 新建 → 200 + 落库（source=manual, enabled=1）
  5) POST 重复 pattern+category → 409（唯一约束）
  6) POST weight 越界（0/11/-1）→ 422
  7) POST category 非法 → 422
  8) PUT 无 confirm → 403
  9) PUT reason<20 → 422
 10) PUT confirm+reason → 200 + 更新生效 + gate_logs(pattern.update, before/after)
 11) PUT 不存在 id → 404
 12) toggle 启停：停用后 rule_match 不命中，启用恢复
 13) DELETE 无 confirm → 403；reason 短 → 422
 14) DELETE confirm+reason → 200 {deleted:true} + 行删除 + gate_logs(pattern.delete)
 15) DELETE 不存在 id → 404
 16) import 幂等：首次 imported=N/skipped=0，重复 imported=0/skipped=N；与种子冲突计 skipped
 17) import 非法 category → 422
 18) export 结构：total=items 数，字段齐（含 hit_count）
 19) 在线学习 boost：案例发布成功 → 命中词条 weight+1 + gate_logs(pattern.boost, 案例发布回写)
 20) 在线学习 boost 封顶：weight=10 不再上调
 21) 在线学习 hit_count：scan_text 命中后 hit_count 递增
"""

from __future__ import annotations

import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from app import db as dbmod
from app.config import get_settings
from app.main import app
from app.services import llm_provider
from app.services.cases import CaseService
from app.services.speech_engine import SpeechEngine
from app.services.speech_seed import SEED_PATTERNS

GOOD_REASON = "这是一条充分的操作理由，长度超过二十个字符用于审计说明"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """隔离数据目录（空 AF_API_KEY → 使用生成的 bootstrap key），服务层直连连接。"""
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


def _mk_det(env, text: str = "刷单返利垫付解锁任务") -> int:
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    cur = env.execute(
        "INSERT INTO detections (source, platform, text_hash, content, status, grade) "
        "VALUES ('manual', 'other', ?, ?, 'processed', 'L3')",
        (digest, text),
    )
    return int(cur.lastrowid)


# ===========================================================================
# 认证与列表
# ===========================================================================

def test_all_endpoints_401_without_key(api):
    client, _ = api
    import_body = {"items": [{"pattern": "x", "weight": 5, "category": "other"}]}
    for method, url, body in [
        ("get", "/api/v1/speech-patterns", None),
        ("get", "/api/v1/speech-patterns/export", None),
        ("post", "/api/v1/speech-patterns",
         {"pattern": "a", "weight": 5, "category": "other"}),
        ("put", "/api/v1/speech-patterns/1",
         {"pattern": "a", "weight": 5, "category": "other"}),
        ("post", "/api/v1/speech-patterns/1/toggle", None),
        ("delete", "/api/v1/speech-patterns/1",
         {"confirm": True, "reason": GOOD_REASON}),
        ("post", "/api/v1/speech-patterns/import", import_body),
    ]:
        r = client.request(method, url, json=body)
        assert r.status_code == 401, f"{method} {url}"
        assert r.json()["ok"] is False


def test_list_returns_items_with_hit_count(api):
    client, key = api
    r = client.get("/api/v1/speech-patterns", headers=_auth(key))
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["total"] == len(SEED_PATTERNS)
    assert len(data["items"]) == min(len(SEED_PATTERNS), data["page_size"])
    for f in ("id", "pattern", "regex", "weight", "category",
              "source", "enabled", "hit_count", "created_at"):
        assert f in data["items"][0], f"缺字段 {f}"


def test_list_pagination_and_filter(api):
    client, key = api
    client.post("/api/v1/speech-patterns", headers=_auth(key),
                json={"pattern": "KWCASE001", "weight": 5, "category": "job_scam"})
    r = client.get("/api/v1/speech-patterns", headers=_auth(key),
                   params={"category": "job_scam", "keyword": "KWCASE001"})
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["total"] >= 1
    assert all(i["category"] == "job_scam" for i in data["items"])
    assert any(i["pattern"] == "KWCASE001" for i in data["items"])
    r2 = client.get("/api/v1/speech-patterns", headers=_auth(key),
                    params={"page": 1, "page_size": 5})
    assert r2.json()["data"]["page_size"] == 5
    assert len(r2.json()["data"]["items"]) <= 5


# ===========================================================================
# POST 新建
# ===========================================================================

def test_create_pattern_ok(api, env):
    client, key = api
    r = client.post("/api/v1/speech-patterns", headers=_auth(key), json={
        "pattern": "独家量子疗程", "regex": "量子疗程", "weight": 7,
        "category": "other", "enabled": True,
    })
    assert r.status_code == 200
    d = r.json()["data"]
    assert d["pattern"] == "独家量子疗程"
    assert d["category"] == "other"
    assert d["weight"] == 7
    assert d["enabled"] == 1
    assert d["hit_count"] == 0
    row = env.execute("SELECT * FROM speech_patterns WHERE id=?", (d["id"],)).fetchone()
    assert row is not None and row["source"] == "manual" and row["enabled"] == 1


def test_create_duplicate_409(api):
    client, key = api
    body = {"pattern": "重复词条", "weight": 5, "category": "other"}
    assert client.post("/api/v1/speech-patterns", headers=_auth(key), json=body).status_code == 200
    r = client.post("/api/v1/speech-patterns", headers=_auth(key), json=body)
    assert r.status_code == 409
    assert r.json()["ok"] is False


def test_create_weight_out_of_range_422(api):
    client, key = api
    for w in (0, 11, -1):
        r = client.post("/api/v1/speech-patterns", headers=_auth(key),
                        json={"pattern": f"越界{w}", "weight": w, "category": "other"})
        assert r.status_code == 422, f"weight={w}"


def test_create_invalid_category_422(api):
    client, key = api
    r = client.post("/api/v1/speech-patterns", headers=_auth(key),
                    json={"pattern": "xx", "weight": 5, "category": "not_a_category"})
    assert r.status_code == 422
    assert r.json()["ok"] is False


# ===========================================================================
# PUT 编辑（敏感操作）
# ===========================================================================

def test_update_requires_confirm_403(api):
    client, key = api
    r = client.put("/api/v1/speech-patterns/1", headers=_auth(key), json={
        "pattern": "改", "weight": 6, "category": "other",
        "confirm": False, "reason": ""})
    assert r.status_code == 403


def test_update_short_reason_422(api):
    client, key = api
    r = client.put("/api/v1/speech-patterns/1", headers=_auth(key), json={
        "pattern": "改", "weight": 6, "category": "other",
        "confirm": True, "reason": "太短"})
    assert r.status_code == 422


def test_update_ok_with_audit(api, env):
    client, key = api
    r0 = client.post("/api/v1/speech-patterns", headers=_auth(key),
                     json={"pattern": "原始词条", "weight": 5, "category": "other"})
    pid = r0.json()["data"]["id"]
    r = client.put(f"/api/v1/speech-patterns/{pid}", headers=_auth(key), json={
        "pattern": "编辑后词条", "weight": 8, "category": "job_scam",
        "confirm": True, "reason": GOOD_REASON})
    assert r.status_code == 200
    d = r.json()["data"]
    assert d["pattern"] == "编辑后词条" and d["weight"] == 8 and d["category"] == "job_scam"
    row = env.execute(
        "SELECT * FROM gate_logs WHERE action='pattern.update' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row is not None
    before, after = json.loads(row["before"]), json.loads(row["after"])
    assert before["pattern"] == "原始词条" and after["pattern"] == "编辑后词条"
    assert after["weight"] == 8


def test_update_missing_404(api):
    client, key = api
    r = client.put("/api/v1/speech-patterns/999999", headers=_auth(key), json={
        "pattern": "x", "weight": 5, "category": "other",
        "confirm": True, "reason": GOOD_REASON})
    assert r.status_code == 404


# ===========================================================================
# toggle 启停（热更新）
# ===========================================================================

def test_toggle_disables_pattern_for_rule_match(api, env):
    client, key = api
    r0 = client.post("/api/v1/speech-patterns", headers=_auth(key),
                     json={"pattern": "P17独家内测", "weight": 7, "category": "other"})
    pid = r0.json()["data"]["id"]
    engine = SpeechEngine()
    hits, _ = engine.rule_match("快来领P17独家内测资格", env)
    assert any(h["pattern_id"] == pid for h in hits)
    env.commit()  # 释放 rule_match 的 hit_count 写锁，避免阻塞 API 写路径
    r = client.post(f"/api/v1/speech-patterns/{pid}/toggle", headers=_auth(key))
    assert r.status_code == 200
    assert r.json()["data"]["enabled"] == 0
    hits2, _ = engine.rule_match("快来领P17独家内测资格", env)
    assert not any(h["pattern_id"] == pid for h in hits2), "停用后 rule_match 不应命中"
    env.commit()
    r2 = client.post(f"/api/v1/speech-patterns/{pid}/toggle", headers=_auth(key))
    assert r2.json()["data"]["enabled"] == 1
    hits3, _ = engine.rule_match("快来领P17独家内测资格", env)
    assert any(h["pattern_id"] == pid for h in hits3), "重新启用后应恢复命中"
    env.commit()


# ===========================================================================
# DELETE 删除（敏感操作）
# ===========================================================================

def test_delete_requires_confirm_and_reason(api):
    client, key = api
    r = client.request("delete", "/api/v1/speech-patterns/1", headers=_auth(key),
                       json={"confirm": False, "reason": ""})
    assert r.status_code == 403
    r2 = client.request("delete", "/api/v1/speech-patterns/1", headers=_auth(key),
                        json={"confirm": True, "reason": "短"})
    assert r2.status_code == 422


def test_delete_ok_with_audit(api, env):
    client, key = api
    r0 = client.post("/api/v1/speech-patterns", headers=_auth(key),
                     json={"pattern": "待删除词条", "weight": 5, "category": "other"})
    pid = r0.json()["data"]["id"]
    r = client.request("delete", f"/api/v1/speech-patterns/{pid}", headers=_auth(key),
                       json={"confirm": True, "reason": GOOD_REASON})
    assert r.status_code == 200
    assert r.json()["data"]["deleted"] is True
    assert env.execute(
        "SELECT COUNT(*) AS n FROM speech_patterns WHERE id=?", (pid,)
    ).fetchone()["n"] == 0
    row = env.execute(
        "SELECT * FROM gate_logs WHERE action='pattern.delete' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row is not None and "待删除词条" in row["before"]


def test_delete_missing_404(api):
    client, key = api
    r = client.request("delete", "/api/v1/speech-patterns/999999", headers=_auth(key),
                       json={"confirm": True, "reason": GOOD_REASON})
    assert r.status_code == 404


# ===========================================================================
# import / export
# ===========================================================================

def test_import_idempotent(api):
    client, key = api
    items = [
        {"pattern": "IMP001导入词", "regex": "IMP001", "weight": 6, "category": "other"},
        {"pattern": "IMP002导入词", "weight": 4, "category": "job_scam"},
    ]
    r1 = client.post("/api/v1/speech-patterns/import", headers=_auth(key),
                     json={"items": items})
    assert r1.status_code == 200
    assert r1.json()["data"] == {"imported": 2, "skipped": 0}
    r2 = client.post("/api/v1/speech-patterns/import", headers=_auth(key),
                     json={"items": items})
    assert r2.json()["data"] == {"imported": 0, "skipped": 2}
    # 与种子词条（pattern+category）冲突 → 计 skipped
    r3 = client.post("/api/v1/speech-patterns/import", headers=_auth(key),
                     json={"items": [{"pattern": "稳赚不赔", "weight": 9,
                                      "category": "fake_investment"}]})
    assert r3.json()["data"] == {"imported": 0, "skipped": 1}


def test_import_invalid_category_422(api):
    client, key = api
    r = client.post("/api/v1/speech-patterns/import", headers=_auth(key),
                    json={"items": [{"pattern": "x", "weight": 5, "category": "bad"}]})
    assert r.status_code == 422


def test_export_structure(api):
    client, key = api
    r = client.get("/api/v1/speech-patterns/export", headers=_auth(key))
    assert r.status_code == 200
    d = r.json()["data"]
    assert d["total"] == len(d["items"]) and d["total"] >= 50
    for f in ("id", "pattern", "regex", "weight", "category",
              "source", "enabled", "hit_count"):
        assert f in d["items"][0], f"缺字段 {f}"


# ===========================================================================
# 在线学习回写
# ===========================================================================

def test_publish_boosts_hit_patterns_weight(env):
    det_id = _mk_det(env)
    row = env.execute(
        "SELECT id, weight FROM speech_patterns WHERE pattern='刷单返利'"
    ).fetchone()
    assert row is not None
    env.execute(
        "INSERT INTO speech_hits (det_id, pattern_id, matched_text, score) VALUES (?, ?, '刷单返利', 9.0)",
        (det_id, row["id"]),
    )
    env.commit()
    CaseService().publish(env, det_id=det_id,
                          redacted_payload="刷单返利案例脱敏内容P17",
                          graph_tags=["job_scam"])
    new = env.execute("SELECT weight FROM speech_patterns WHERE id=?", (row["id"],)).fetchone()
    assert float(new["weight"]) == min(10.0, float(row["weight"]) + 1.0)
    logs = env.execute("SELECT * FROM gate_logs WHERE action='pattern.boost'").fetchall()
    assert len(logs) == 1
    assert "案例发布回写" in logs[0]["reason"]
    before, after = json.loads(logs[0]["before"]), json.loads(logs[0]["after"])
    assert after["weight"] == before["weight"] + 1.0


def test_publish_boost_caps_at_10(env):
    det_id = _mk_det(env, "杀猪盘")
    row = env.execute(
        "SELECT id, weight FROM speech_patterns WHERE pattern='杀猪盘'"
    ).fetchone()
    assert float(row["weight"]) == 10.0
    env.execute(
        "INSERT INTO speech_hits (det_id, pattern_id, matched_text, score) VALUES (?, ?, '杀猪盘', 10.0)",
        (det_id, row["id"]),
    )
    env.commit()
    CaseService().publish(env, det_id=det_id,
                          redacted_payload="杀猪盘案例脱敏内容P17",
                          graph_tags=["pig_butchering"])
    new = env.execute("SELECT weight FROM speech_patterns WHERE id=?", (row["id"],)).fetchone()
    assert float(new["weight"]) == 10.0, "权重封顶 10 不再上调"


@pytest.mark.asyncio
async def test_hit_count_increments_on_scan(env):
    llm_provider.set_provider(None)  # no_key 降级链：纯规则
    r = await SpeechEngine().scan_text("刷单返利，垫付解锁任务", source="manual", conn=env)
    assert r["hits"]
    row = env.execute(
        "SELECT hit_count FROM speech_patterns WHERE pattern='刷单返利'"
    ).fetchone()
    assert row is not None and row["hit_count"] >= 1
    SpeechEngine().rule_match("刷单返利，垫付解锁任务", env)
    row2 = env.execute(
        "SELECT hit_count FROM speech_patterns WHERE pattern='刷单返利'"
    ).fetchone()
    assert row2["hit_count"] == row["hit_count"] + 1
