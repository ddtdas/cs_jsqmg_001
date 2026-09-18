"""P7 启动脚本 + 本机一键登录/key 管理回归测试。

覆盖（对应任务 R1 DoD）：
  1) start.bat / stop.bat / open-console.bat 三文件存在、纯 ASCII（字节扫描）、CRLF
  2) POST /auth/local-login：回环来源放行并返回 bootstrap key；非回环 403
  3) GET  /auth/current-key：回环返回 key 明文；非回环 403
  4) POST /auth/reset-key：无 confirm 403 / reason 不足 422 / 非回环 403；
     成功路径：key 变化 + 文件更新 + api_keys 哈希登记（旧停用新启用）
     + gate_logs 审计（action='auth.reset_key'）+ events 事件
  5) bootstrap-info 公开端点不泄露 key

运行：pytest tests/test_p7_launch.py -q（项目根）。
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db as dbmod
from app.config import get_settings

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ["start.bat", "stop.bat", "open-console.bat"]
# P1-A5：新 key 为 32-hex（128bit）；兼容旧版 8-hex（32bit）文件
KEY_RE = re.compile(r"^af_admin_[0-9a-f]{8,64}$")

REASON_20 = "本机用户主动重置本地登录密钥，用于定期安全轮换更新凭据"


@pytest.fixture()
def api(tmp_path, monkeypatch):
    """隔离 API 客户端：临时 data 目录 + 文件 key 路径（回环来源）。"""
    monkeypatch.setenv("AF_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AF_API_KEY", "")  # 显式清空：reset-key 走文件路径可测
    get_settings.cache_clear()
    dbmod.close_all()

    from app.main import app

    # base_url=127.0.0.1：Host 头为 127.0.0.1（P1-A4 Host 白名单）
    with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000)) as c:
        yield c
    dbmod.close_all()
    get_settings.cache_clear()


def _bootstrap_key(tmp_path) -> str:
    return (tmp_path / "bootstrap_admin_key.txt").read_text(encoding="utf-8").strip()


def _non_loopback_client():
    """默认 TestClient：client host 为 'testclient'（非回环）。"""
    from app.main import app

    return TestClient(app)  # client=("testclient", 50000)


# ===========================================================================
# 1) 三脚本存在 / 纯 ASCII / CRLF
# ===========================================================================

@pytest.mark.parametrize("name", SCRIPTS)
def test_launch_script_exists_pure_ascii_crlf(name):
    p = ROOT / name
    assert p.is_file(), f"缺少启动脚本: {name}"
    raw = p.read_bytes()
    non_ascii = [b for b in raw if b > 127]
    assert not non_ascii, f"{name} 含非 ASCII 字节: {non_ascii[:16]}"
    text = raw.decode("ascii")
    assert "\r\n" in text, f"{name} 不是 CRLF 行尾"
    assert "\n" not in text.replace("\r\n", ""), f"{name} 存在裸 LF（非 CRLF）"


def test_launch_script_logic_markers():
    """脚本关键逻辑标记存在（可读性 + 逻辑完备性）。"""
    start = (ROOT / "start.bat").read_text(encoding="ascii")
    assert "netstat" in start and "LISTENING" in start          # 三态：端口探测
    assert ":ALREADY_RUNNING" in start and ":START_MASTER" in start
    assert ":PROBE_LOOP" in start and ":PROBE_FAILED" in start  # 健康探测 + 失败不静默
    assert "bootstrap_admin_key.txt" in start                   # admin key 提示
    assert "start \"\"" in start and "/ui/" in start            # 打开浏览器
    assert "%~dp0" in start                                     # 项目根自推导

    stop = (ROOT / "stop.bat").read_text(encoding="ascii")
    assert "netstat" in stop and "taskkill" in stop             # 按 PID 杀进程树
    assert "/T /F" in stop
    assert "not defined FOUND_PID" in stop                      # 未运行提示分支

    console = (ROOT / "open-console.bat").read_text(encoding="ascii")
    assert "activate.bat" in console and "%~dp0" in console     # venv 激活 + 项目根
    assert "cmd /k" in console                                  # 窗口保持


# ===========================================================================
# 2) bootstrap-info 公开端点不泄露 key
# ===========================================================================

def test_bootstrap_info_no_key_leak(api):
    r = api.post("/api/v1/auth/bootstrap-info")
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["initialized"] is True
    assert "key" not in data, "bootstrap-info 公开端点不得泄露 key"


# ===========================================================================
# 3) local-login：回环放行 / 非回环 403
# ===========================================================================

def test_local_login_loopback_returns_key(api, tmp_path):
    r = api.post("/api/v1/auth/local-login")
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["key"] == _bootstrap_key(tmp_path)
    assert KEY_RE.match(data["key"])
    assert "hint" in data


def test_local_login_non_loopback_forbidden(api):
    with _non_loopback_client() as c2:
        r = c2.post("/api/v1/auth/local-login")
    assert r.status_code == 403
    body = r.json()
    assert body["ok"] is False and body["error"]["code"] == "loopback_only"


# ===========================================================================
# 4) current-key：回环返回明文 / 非回环 403
# ===========================================================================

def test_current_key_loopback_ok(api, tmp_path):
    r = api.get("/api/v1/auth/current-key")
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["key"] == _bootstrap_key(tmp_path)
    assert data["source"] == "file"
    # P2-A 修复：current-key 不再返回 key_file 绝对路径（曾泄露盘符/目录/文件名）
    assert "key_file" not in data, "current-key 不得泄露 key_file 绝对路径"


def test_current_key_non_loopback_forbidden(api):
    with _non_loopback_client() as c2:
        r = c2.get("/api/v1/auth/current-key")
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "loopback_only"


# ===========================================================================
# 5) reset-key：双确认 + 审计 + key 轮换
# ===========================================================================

def test_reset_key_requires_confirm_and_reason(api, tmp_path):
    before = _bootstrap_key(tmp_path)
    # 无 confirm / confirm=false -> 403
    r0 = api.post("/api/v1/auth/reset-key", json={})
    assert r0.status_code == 403 and r0.json()["error"]["code"] == "confirm_required"
    r1 = api.post("/api/v1/auth/reset-key", json={"confirm": False, "reason": REASON_20})
    assert r1.status_code == 403 and r1.json()["error"]["code"] == "confirm_required"
    # reason 不足 -> 422
    r2 = api.post("/api/v1/auth/reset-key", json={"confirm": True, "reason": "短理由"})
    assert r2.status_code == 422 and r2.json()["error"]["code"] == "reason_too_short"
    # 失败请求不得改 key / 不留审计
    assert _bootstrap_key(tmp_path) == before
    conn = dbmod.get_conn()
    n = conn.execute("SELECT COUNT(*) AS n FROM gate_logs WHERE action='auth.reset_key'").fetchone()["n"]
    assert n == 0


def test_reset_key_non_loopback_forbidden(api):
    with _non_loopback_client() as c2:
        r = c2.post("/api/v1/auth/reset-key",
                    json={"confirm": True, "reason": REASON_20})
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "loopback_only"


def test_reset_key_success_rotates_key_and_audits(api, tmp_path):
    conn = dbmod.get_conn()
    old = _bootstrap_key(tmp_path)

    r = api.post("/api/v1/auth/reset-key", json={"confirm": True, "reason": REASON_20})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    data = body["data"]
    new_key = data["key"]
    assert new_key != old
    assert KEY_RE.match(new_key)
    assert data["audit"]["action"] == "auth.reset_key"
    assert data["audit"]["gate_log_id"] > 0

    # 文件已更新 + 后续 local-login / current-key 返回新 key
    assert _bootstrap_key(tmp_path) == new_key
    assert api.post("/api/v1/auth/local-login").json()["data"]["key"] == new_key
    assert api.get("/api/v1/auth/current-key").json()["data"]["key"] == new_key

    # gate_logs 审计（D6）
    log = conn.execute(
        "SELECT * FROM gate_logs WHERE action='auth.reset_key' ORDER BY id DESC"
    ).fetchone()
    assert log is not None
    assert log["reason"] == REASON_20
    assert len(log["payload_hash"]) == 64
    before = json.loads(log["before"])
    after = json.loads(log["after"])
    assert before["key_sha256"] == hashlib.sha256(old.encode()).hexdigest()
    assert after["key_sha256"] == hashlib.sha256(new_key.encode()).hexdigest()

    # api_keys 哈希登记：旧停用、新启用
    old_row = conn.execute("SELECT enabled FROM api_keys WHERE key_hash=?",
                           (hashlib.sha256(old.encode()).hexdigest(),)).fetchone()
    new_row = conn.execute("SELECT enabled FROM api_keys WHERE key_hash=?",
                           (hashlib.sha256(new_key.encode()).hexdigest(),)).fetchone()
    assert old_row is not None and old_row["enabled"] == 0
    assert new_row is not None and new_row["enabled"] == 1

    # 事件流
    ev = conn.execute("SELECT COUNT(*) AS n FROM events WHERE kind='admin_key_reset'").fetchone()["n"]
    assert ev >= 1
