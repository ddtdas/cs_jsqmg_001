"""采集矩阵（P10）测试：矩阵 CRUD / 采集执行（防御式）/ 技术栈 / 鉴权 / 调度注册。"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.scheduler import create_scheduler, get_scheduler

client = TestClient(app)


def _admin_headers() -> dict:
    # TestClient 内直接读 bootstrap key（lifespan 已初始化 data 目录）
    from app.config import get_settings
    from app.deps import load_or_create_bootstrap_key

    settings = get_settings()
    key = load_or_create_bootstrap_key(settings)
    return {"X-API-Key": key}


@pytest.fixture(scope="module", autouse=True)
def _cleanup_cells():
    yield
    # 清理本模块创建的矩阵单元格，避免污染其他用例
    from app import db as dbmod

    conn = dbmod.get_conn()
    conn.execute("DELETE FROM collector_matrix")
    conn.commit()


def test_tech_stack_structure():
    r = client.get("/api/v1/collector/tech-stack", headers=_admin_headers())
    assert r.status_code == 200
    data = r.json()["data"]
    stack = data["stack"] if isinstance(data, dict) and "stack" in data else data
    assert len(stack) == 8
    names = [s["name"] for s in stack]
    assert "Scrapy" in names and "Crawlee" in names and "Wechaty" in names
    for s in stack:
        assert s["repo"] and s["stars"] > 0 and s["use"] and s["integrate"]


def test_tech_stack_requires_auth():
    r = client.get("/api/v1/collector/tech-stack")
    assert r.status_code == 401


def test_add_cell_rsshub_no_account():
    r = client.post(
        "/api/v1/collector/matrix",
        json={"platform": "rsshub", "account": "", "strategy": {"collect": "posts", "frequency": "hourly", "depth": 3, "keyword": "杀猪盘", "near_dup": 0.85}},
        headers=_admin_headers(),
    )
    assert r.status_code == 200
    d = r.json()["data"]
    assert d["platform"] == "rsshub" and d["status"] == "pending"
    assert d["id"] > 0


def test_add_cell_invalid_platform_422():
    r = client.post("/api/v1/collector/matrix", json={"platform": "bogus"}, headers=_admin_headers())
    assert r.status_code == 422


def test_list_matrix_contains_cell():
    r = client.get("/api/v1/collector/matrix", headers=_admin_headers())
    assert r.status_code == 200
    data = r.json()["data"]
    cells = data["cells"] if isinstance(data, dict) and "cells" in data else data
    assert any(c["platform"] == "rsshub" for c in cells)


def test_update_cell_disable():
    r = client.get("/api/v1/collector/matrix", headers=_admin_headers())
    data = r.json()["data"]
    cells = data["cells"] if isinstance(data, dict) and "cells" in data else data
    cid = next(c["id"] for c in cells if c["platform"] == "rsshub")
    r = client.put(f"/api/v1/collector/matrix/{cid}", json={"enabled": False}, headers=_admin_headers())
    assert r.status_code == 200
    assert r.json()["data"]["enabled"] == 0


def test_run_cell_unconfigured_platform_no_crash():
    # weibo 无 cookie → collected=0 + status ok/error 都不崩
    r = client.post(
        "/api/v1/collector/matrix",
        json={"platform": "weibo", "account": "test_acct", "strategy": {"collect": "dm"}},
        headers=_admin_headers(),
    )
    assert r.status_code == 200
    cid = r.json()["data"]["id"]
    r = client.post(f"/api/v1/collector/matrix/{cid}/run", headers=_admin_headers())
    assert r.status_code == 200
    d = r.json()["data"]
    assert d["collected"] == 0
    assert d["error"]  # weibo 占位说明


def test_run_cell_invalid_id_404():
    r = client.post("/api/v1/collector/matrix/999999/run", headers=_admin_headers())
    assert r.status_code == 404


def test_run_all_empty_or_limited():
    r = client.post("/api/v1/collector/matrix/run-all", headers=_admin_headers())
    assert r.status_code == 200
    d = r.json()["data"]
    assert "ran" in d and "collected" in d


def test_delete_cell():
    r = client.get("/api/v1/collector/matrix", headers=_admin_headers())
    data = r.json()["data"]
    cells = data["cells"] if isinstance(data, dict) and "cells" in data else data
    cid = next(c["id"] for c in cells if c["platform"] == "weibo")
    r = client.delete(f"/api/v1/collector/matrix/{cid}", headers=_admin_headers())
    assert r.status_code == 200
    assert r.json()["data"]["deleted"] is True


def test_scheduler_registers_collector_job():
    sched = create_scheduler()
    ids = [j.id for j in sched.get_jobs()]
    assert "job_collector_matrix" in ids
    if get_scheduler() and get_scheduler().running:
        pass  # 测试环境不强制启动
