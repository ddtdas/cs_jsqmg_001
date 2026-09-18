"""聊天记录一键导入 API 专项测试（P15）：ChatImportService + /api/v1/chat-import/*。

运行：cd /d 项目根 && .venv\\Scripts\\python.exe -m pytest tests/test_p15_chat_import.py -q

覆盖（15 条真实运行）：
  1) GET  /chat-import/candidates 无 key → 401
  2) GET  /chat-import/candidates 有 key → 200 {wechat, qq, custom} 结构
  3) POST /chat-import/scan 未知路径 → {platform:'unknown', db_files:[]}
  4) POST /chat-import/scan 微信目录（坏 SQLite 库）→ platform:'wechat' + encrypted:true
  5) POST /chat-import/import TXT 3 行（含手机号 13800138000）→ imported=3 + preview 前 3 条
  6) 落库校验：source='chat_import' 3 行 + platform='other' + content 脱敏（无手机号原文）+ status='scanned'
  7) 重复导入幂等：imported=0 / skipped=3（text_hash 去重）
  8) parse_generic TXT 两行（服务层直连）
  9) parse_generic JSON 数组两条（服务层直连）
 10) DELETE /chat-import/imported 无 confirm → 403 confirm_required
 11) DELETE /chat-import/imported confirm+reason<20 字 → 422 reason_too_short
 12) DELETE /chat-import/imported confirm+reason → 200 {deleted:3} + gate_logs('chat_import.clear') 审计
 13) POST /chat-import/import 加密库（坏 sqlite 文件）→ {error:'encrypted_db'}
 14) POST /chat-import/import-file JSON → 复用导入落库（内容脱敏无手机号原文）
 15) POST /chat-import/import-file 不存在文件 → 422 parse_failed
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app import db as dbmod
from app.config import get_settings
from app.main import app
from app.services.chat_import import ChatImportService

_MOBILE = "13800138000"
_REASON = "P15 自动化验证完成，清理临时聊天导入记录以便后续用例隔离"


@pytest.fixture()
def api(tmp_path, monkeypatch):
    """隔离数据目录 + TestClient 上下文（lifespan 生成 bootstrap key / 建表 / 种子库）。"""
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


def _write_chat_txt(tmp_path, name="chat.txt"):
    """三行聊天文本（末行含手机号，验证脱敏链路）。"""
    p = tmp_path / name
    p.write_text(
        "您好，我收到陌生人发来的退款短信，让我联系客服处理\n"
        "对方让我下载 App 并输入银行卡密码，我怀疑是诈骗\n"
        f"客服电话 {_MOBILE}，请尽快核实资金安全\n",
        encoding="utf-8",
    )
    return p


def _make_fake_wechat_dir(tmp_path):
    """伪造微信目录 + 一个"加密/损坏"的 SQLite 库文件。"""
    root = tmp_path / "WeChat Files" / "wxid_abc"
    (root / "Msg" / "Multi").mkdir(parents=True)
    bad = root / "Msg" / "Multi" / "msg0.db"
    bad.write_bytes(b"not a real sqlite file - encrypted or corrupt\x00" * 16)
    return root


# ===========================================================================
# API：/api/v1/chat-import/candidates
# ===========================================================================

def test_candidates_requires_auth(api):
    client, _ = api
    r = client.get("/api/v1/chat-import/candidates")
    assert r.status_code == 401
    assert r.json()["ok"] is False


def test_candidates_structure(api):
    client, key = api
    r = client.get("/api/v1/chat-import/candidates", headers={"X-API-Key": key})
    assert r.status_code == 200
    cand = r.json()["data"]["candidates"]
    assert isinstance(cand, dict)
    assert set(cand.keys()) == {"wechat", "qq", "custom"}
    assert isinstance(cand["wechat"], list) and isinstance(cand["qq"], list)
    assert isinstance(cand["custom"], str)


# ===========================================================================
# API：/api/v1/chat-import/scan
# ===========================================================================

def test_scan_unknown_path(api):
    client, key = api
    r = client.post(
        "/api/v1/chat-import/scan",
        headers={"X-API-Key": key},
        json={"path": "Z:\\definitely_not_exists\\nope"},
    )
    assert r.status_code == 200
    result = r.json()["data"]["result"]
    assert result["platform"] == "unknown"
    assert result["db_files"] == []
    assert result["encrypted"] is False


def test_scan_wechat_dir_detects_encrypted(api, tmp_path):
    client, key = api
    root = _make_fake_wechat_dir(tmp_path)
    r = client.post(
        "/api/v1/chat-import/scan",
        headers={"X-API-Key": key},
        json={"path": str(root)},
    )
    assert r.status_code == 200
    result = r.json()["data"]["result"]
    assert result["platform"] == "wechat"
    assert len(result["db_files"]) == 1
    assert result["encrypted"] is True


# ===========================================================================
# API：/api/v1/chat-import/import（TXT 全流程）
# ===========================================================================

def test_import_txt_three_messages(api, tmp_path):
    client, key = api
    txt = _write_chat_txt(tmp_path)
    r = client.post(
        "/api/v1/chat-import/import",
        headers={"X-API-Key": key},
        json={"path": str(txt)},
    )
    assert r.status_code == 200, r.text
    result = r.json()["data"]["result"]
    assert result["imported"] == 3
    assert isinstance(result["suspicious"], int)
    preview = result["preview"]
    assert len(preview) == 3, "preview 应包含前 3 条"
    for item in preview:
        assert set(item.keys()) == {"sender", "time", "content_masked", "grade"}
        assert _MOBILE not in item["content_masked"], "预览内容应脱敏"
        assert isinstance(item["grade"], str)


def test_import_persists_masked_rows(api, tmp_path):
    client, key = api
    txt = _write_chat_txt(tmp_path)
    r = client.post(
        "/api/v1/chat-import/import",
        headers={"X-API-Key": key},
        json={"path": str(txt)},
    )
    assert r.status_code == 200, r.text
    conn = dbmod.get_conn()
    rows = conn.execute(
        "SELECT id, source, platform, text_hash, content, status "
        "FROM detections WHERE source='chat_import' ORDER BY id"
    ).fetchall()
    assert len(rows) == 3
    assert all(row["source"] == "chat_import" for row in rows)
    assert all(row["platform"] == "other" for row in rows)
    assert all(row["content"] and len(row["text_hash"]) == 64 for row in rows)
    assert all(_MOBILE not in (row["content"] or "") for row in rows), "落库内容必须脱敏"
    assert any("*" in (row["content"] or "") for row in rows), "手机号应被掩码"
    assert all(row["status"] == "scanned" for row in rows), "导入后应完成推理链分析"


def test_import_idempotent_skips_duplicates(api, tmp_path):
    client, key = api
    txt = _write_chat_txt(tmp_path)
    first = client.post(
        "/api/v1/chat-import/import",
        headers={"X-API-Key": key},
        json={"path": str(txt)},
    ).json()["data"]["result"]
    assert first["imported"] == 3
    second = client.post(
        "/api/v1/chat-import/import",
        headers={"X-API-Key": key},
        json={"path": str(txt)},
    ).json()["data"]["result"]
    assert second["imported"] == 0, "重复导入应去重"
    assert second.get("skipped") == 3


def test_import_encrypted_db_reports_encrypted_db(api, tmp_path):
    client, key = api
    root = _make_fake_wechat_dir(tmp_path)
    r = client.post(
        "/api/v1/chat-import/import",
        headers={"X-API-Key": key},
        json={"path": str(root)},
    )
    assert r.status_code == 200
    result = r.json()["data"]["result"]
    assert result["imported"] == 0
    assert result["error"] == "encrypted_db"


# ===========================================================================
# 服务层：parse_generic
# ===========================================================================

def test_parse_generic_txt(tmp_path):
    p = tmp_path / "chat.txt"
    p.write_text("第一行：你好\n第二行：在吗\n", encoding="utf-8")
    msgs = ChatImportService().parse_generic(str(p))
    assert len(msgs) == 2
    assert msgs[0]["content"] == "第一行：你好"
    assert all(m["sender"] == "" and m["time"] == "" for m in msgs)


def test_parse_generic_json(tmp_path):
    p = tmp_path / "chat.json"
    p.write_text(json.dumps([
        {"sender": "alice", "time": "2024-01-01 10:00:00", "content": "你好"},
        {"sender": "bob", "time": "2024-01-01 10:01:00", "content": "收到"},
    ], ensure_ascii=False), encoding="utf-8")
    msgs = ChatImportService().parse_generic(str(p))
    assert len(msgs) == 2
    assert msgs[0]["sender"] == "alice" and msgs[0]["content"] == "你好"
    assert msgs[1]["sender"] == "bob" and msgs[1]["content"] == "收到"


# ===========================================================================
# API：/api/v1/chat-import/imported（清空 + 审计）
# ===========================================================================

def test_delete_imported_requires_confirm(api):
    client, key = api
    r = client.request(
        "DELETE",
        "/api/v1/chat-import/imported",
        headers={"X-API-Key": key},
        json={"confirm": False, "reason": _REASON},
    )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "confirm_required"


def test_delete_imported_reason_too_short(api):
    client, key = api
    r = client.request(
        "DELETE",
        "/api/v1/chat-import/imported",
        headers={"X-API-Key": key},
        json={"confirm": True, "reason": "太短"},
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "reason_too_short"


def test_delete_imported_success_with_gate_log(api, tmp_path):
    client, key = api
    txt = _write_chat_txt(tmp_path)
    imp = client.post(
        "/api/v1/chat-import/import",
        headers={"X-API-Key": key},
        json={"path": str(txt)},
    ).json()["data"]["result"]
    assert imp["imported"] == 3

    r = client.request(
        "DELETE",
        "/api/v1/chat-import/imported",
        headers={"X-API-Key": key},
        json={"confirm": True, "reason": _REASON},
    )
    assert r.status_code == 200, r.text
    assert r.json()["data"]["deleted"] == 3

    conn = dbmod.get_conn()
    n = conn.execute(
        "SELECT COUNT(*) AS c FROM detections WHERE source='chat_import'"
    ).fetchone()["c"]
    assert n == 0, "清空后不应残留 chat_import 检测"

    row = conn.execute(
        "SELECT action, payload_json FROM gate_logs "
        "WHERE action='chat_import.clear' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row is not None, "清空应写 gate_logs 审计"
    payload = json.loads(row["payload_json"] or "{}")
    assert payload.get("source") == "chat_import"
    assert payload.get("deleted") == 3


# ===========================================================================
# API：/api/v1/chat-import/import-file
# ===========================================================================

def test_import_file_json_reuses_import(api, tmp_path):
    client, key = api
    p = tmp_path / "chat.json"
    p.write_text(json.dumps([
        {"sender": "kefu", "time": "2024-06-01 10:00:00",
         "content": f"请拨打 {_MOBILE} 办理退款，需验证账户"},
        {"sender": "user", "time": "2024-06-01 10:01:00",
         "content": "好的，我马上去银行处理"},
    ], ensure_ascii=False), encoding="utf-8")
    r = client.post(
        "/api/v1/chat-import/import-file",
        headers={"X-API-Key": key},
        json={"file_path": str(p), "platform": "generic"},
    )
    assert r.status_code == 200, r.text
    result = r.json()["data"]["result"]
    assert result["imported"] == 2
    conn = dbmod.get_conn()
    rows = conn.execute(
        "SELECT content FROM detections WHERE source='chat_import'"
    ).fetchall()
    assert len(rows) == 2
    assert all(_MOBILE not in (row["content"] or "") for row in rows)


def test_import_file_missing_path_422(api, tmp_path):
    client, key = api
    r = client.post(
        "/api/v1/chat-import/import-file",
        headers={"X-API-Key": key},
        json={"file_path": str(tmp_path / "nope.txt")},
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "parse_failed"