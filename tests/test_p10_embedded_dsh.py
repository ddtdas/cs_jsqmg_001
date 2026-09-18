"""内嵌 DSH（P10）端口解析测试：/system/embedded-dsh 端口与 CANARY_DSH_PORT / dsh 配置对齐。

覆盖 P2-3/P1-C1 修复：端口不再硬编码 3092 —— 优先 CANARY_DSH_PORT 环境变量（不读全局 DSH_PORT，避免根脚本 3080 污染），其次
dsh/cordis.patch.yml 的 webserver port，默认 3092。直接调用端点函数（不经真实
9200 主控），monkeypatch 控制环境变量；另含一条完整 HTTP 链路（require_admin）。
"""

from __future__ import annotations

import pytest

from app.api.system import embedded_dsh


@pytest.mark.parametrize(
    ("env_port", "expected"),
    [
        (None, 3092),   # 未设置 CANARY_DSH_PORT → 读 dsh/cordis.patch.yml（默认 3092）
        ("", 3092),     # 空串等同未设置
        ("3093", 3093),  # CANARY_DSH_PORT 显式覆盖
        ("3095", 3095),
    ],
)
def test_embedded_dsh_port_resolution(monkeypatch, env_port, expected):
    """端口解析：env CANARY_DSH_PORT 优先，其次 dsh 配置，默认 3092。"""
    if env_port is None:
        monkeypatch.delenv("CANARY_DSH_PORT", raising=False)
    else:
        monkeypatch.setenv("CANARY_DSH_PORT", env_port)

    result = embedded_dsh()
    assert result["ok"] is True
    data = result["data"]
    assert data["port"] == expected
    assert data["url"] == f"http://127.0.0.1:{expected}/"
    assert "running" in data and isinstance(data["running"], bool)
    assert "token_url" in data
    assert "log_path" in data and "dsh-web.out.log" in data["log_path"]


def test_embedded_dsh_http_endpoint(monkeypatch):
    """HTTP 链路：/api/v1/system/embedded-dsh 需 admin 鉴权，封包含 data.port。"""
    from fastapi.testclient import TestClient

    from app.config import get_settings
    from app.deps import load_or_create_bootstrap_key
    from app.main import app

    monkeypatch.delenv("CANARY_DSH_PORT", raising=False)
    settings = get_settings()
    key = load_or_create_bootstrap_key(settings)
    client = TestClient(app)

    r = client.get("/api/v1/system/embedded-dsh", headers={"X-API-Key": key})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["data"]["port"] == 3092

    # 未带 admin key → 401 封包
    r2 = client.get("/api/v1/system/embedded-dsh")
    assert r2.status_code == 401
    assert r2.json()["ok"] is False