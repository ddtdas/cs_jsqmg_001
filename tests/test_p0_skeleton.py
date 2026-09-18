"""P0 骨架测试：health / bootstrap key / 认证。

运行：pytest tests/ -q（项目根，见 pytest.ini pythonpath）。
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

# P1-A5：新 key 为 32-hex（128bit 熵）；兼容旧版 8-hex（32bit）文件
ADMIN_KEY_RE = re.compile(r"^af_admin_[0-9a-f]{8,64}$")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """隔离的测试客户端：AF_DATA_DIR 指向临时目录，避免污染真实 data/。"""
    monkeypatch.setenv("AF_DATA_DIR", str(tmp_path))
    from app.config import get_settings

    get_settings.cache_clear()
    from app.main import app

    with TestClient(app) as c:
        yield c
    get_settings.cache_clear()


def _admin_key(tmp_path) -> str:
    return (tmp_path / "bootstrap_admin_key.txt").read_text(encoding="utf-8").strip()


# ---- health ----

def test_health_ok(client):
    r = client.get("/api/v1/system/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    data = body["data"]
    assert data["status"] == "ok"
    assert data["db"] is True
    assert data["llm"] in ("configured", "not_configured")
    assert data["zhihu"] == "idle"
    assert isinstance(data["version"], str) and data["version"]


# ---- bootstrap key ----

def test_bootstrap_key_file_generated(client, tmp_path):
    key_file = tmp_path / "bootstrap_admin_key.txt"
    assert key_file.exists(), "首次启动应自动生成 bootstrap_admin_key.txt"
    key = _admin_key(tmp_path)
    assert ADMIN_KEY_RE.match(key), f"key 格式应为 af_admin_xxxxxxxx，实际: {key!r}"


def test_bootstrap_key_reused(client, tmp_path):
    """二次启动（同 data 目录）应复用已生成的 key。"""
    first = _admin_key(tmp_path)
    from app.deps import load_or_create_bootstrap_key
    from app.config import get_settings

    settings = get_settings()
    again = load_or_create_bootstrap_key(settings)
    assert again == first


def test_bootstrap_info(client, tmp_path):
    """P0-D1：bootstrap-info 只返回初始化状态/端口，不得泄露 key 文件路径与文件名。"""
    r = client.post("/api/v1/auth/bootstrap-info")
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["initialized"] is True
    assert data["port"] == 9200
    assert "key_file" not in data, "不得返回 key_file（含绝对路径）"
    import json as _json

    blob = _json.dumps(data, ensure_ascii=False)
    assert "bootstrap_admin_key" not in blob and ".txt" not in blob
    assert "\\" not in blob and "C:" not in blob


# ---- auth (D6) ----

def test_stats_rejects_missing_key(client):
    r = client.get("/api/v1/system/stats")
    assert r.status_code == 401
    body = r.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "missing_api_key"


def test_stats_rejects_wrong_key(client):
    r = client.get("/api/v1/system/stats", headers={"X-API-Key": "af_admin_wrong"})
    assert r.status_code == 401
    body = r.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "invalid_api_key"


def test_stats_accepts_valid_key(client, tmp_path):
    key = _admin_key(tmp_path)
    r = client.get("/api/v1/system/stats", headers={"X-API-Key": key})
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_stats_accepts_bearer_key(client, tmp_path):
    key = _admin_key(tmp_path)
    r = client.get("/api/v1/system/stats", headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 200
    assert r.json()["ok"] is True
