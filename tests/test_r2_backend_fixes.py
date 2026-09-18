"""第 2 轮后端修复回归测试（backend-core，R2-BE，安全对抗）。

覆盖（docs/第2轮_实战测试报告.md 问题清单）：
  1) P0-E3   422 验证错误不再回显 input/ctx（防 admin key 泄露）
  2) P1-B7   超 int64 路径参数 → 422 invalid_integer（不 500、不回显类名）
  3) P1-C1/C2/C4  脱敏字形盲区：emoji keycap/圈号/上标/Arabic-Indic/天城/孟加拉/
     罗马/希腊-西里尔 + 身份证 Arabic-Indic + 银行卡（空格/连字符/分段/Luhn）
  4) P1-A2   reset-key 并发串行化：结束后 api_keys enabled=1 仅 1 行
  5) P1-A5   admin key 熵 >=128bit（token_hex(16)），旧 8-hex key 兼容
  6) P1-A4   local-login 等 Origin/Host/Sec-Fetch-Site 校验（伪造 403）
  7) P1-D4   30 并发 publish：无 500 / 无误 404
  8) P1-C6   gate_logs 链式承诺：全链校验 + 篡改检出 + audit-verify 端点
  9) P1-E1   config 键名白名单：密钥/未定义键拒绝，overrides 不回显敏感键

运行：pytest tests/test_r2_backend_fixes.py -q（项目根）。
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app import db as dbmod
from app.config import get_settings
from app.services import llm_provider

ROOT = Path(__file__).resolve().parents[1]
REASON_20 = "这是一条超过二十个字的敏感操作理由说明文本"


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AF_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AF_LLM_API_KEY", "")
    get_settings.cache_clear()
    dbmod.close_all()
    llm_provider.reset_provider()
    dbmod.ensure_schema()
    yield dbmod.get_conn()
    dbmod.close_all()
    llm_provider.reset_provider()
    get_settings.cache_clear()


@pytest.fixture()
def api(tmp_path, monkeypatch):
    """回环 TestClient（Host=127.0.0.1，满足 P1-A4 Host 白名单）。"""
    monkeypatch.setenv("AF_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AF_LLM_API_KEY", "")
    monkeypatch.setenv("AF_API_KEY", "")
    get_settings.cache_clear()
    dbmod.close_all()
    llm_provider.reset_provider()
    from app.middleware.rate_limit import reset_rate_limiters

    reset_rate_limiters()

    from app.main import app

    with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000)) as c:
        yield c
    dbmod.close_all()
    llm_provider.reset_provider()
    get_settings.cache_clear()
    from app.middleware.rate_limit import reset_rate_limiters

    reset_rate_limiters()


def _admin_key(tmp_path) -> str:
    return (tmp_path / "bootstrap_admin_key.txt").read_text(encoding="utf-8").strip()


# ===========================================================================
# 1) P0-E3：422 验证错误不再回显 input/ctx（防 admin key 泄露）
# ===========================================================================


def test_validation_error_does_not_echo_input(api):
    """公开端点 POST /scan/text 类型错误 body → 422，不回显 input 字段与 key 明文。"""
    fake_key = "af_admin_" + "a" * 32
    r = api.post("/api/v1/scan/text", json={"text": {fake_key: "secret-value"}})
    assert r.status_code == 422
    body = r.text
    assert r.json()["ok"] is False
    assert '"input"' not in body, "validation_error 不得包含 input 字段"
    assert '"ctx"' not in body, "validation_error 不得包含 ctx 字段"
    assert fake_key not in body, "错误响应不得回显 admin key 形态明文"
    assert "secret-value" not in body

    r2 = api.post("/api/v1/scan/text", json={"text": 12345})
    assert r2.status_code == 422
    assert '"input"' not in r2.text


# ===========================================================================
# 2) P1-B7：超 int64 路径参数 → 422（不 500、不回显类名）
# ===========================================================================

HUGE = "99999999999999999999"  # > 2^63-1


@pytest.mark.parametrize("method,path,needs_auth", [
    ("post", f"/api/v1/traps/{HUGE}/check-hit", False),
    ("get", f"/api/v1/traps/{HUGE}", False),
    # /events、/alerts 为受保护端点（P1-1）：携带 admin key 后才能走到路径参数校验
    ("get", f"/api/v1/events/{HUGE}", True),
    ("post", f"/api/v1/alerts/{HUGE}/read", True),
])
def test_huge_int_path_param_422_not_500(api, tmp_path, method, path, needs_auth):
    body = {"text": "测试"} if "check-hit" in path else None
    headers = {"X-API-Key": _admin_key(tmp_path)} if needs_auth else None
    r = api.request(method, path, json=body, headers=headers)
    assert r.status_code in (400, 422), f"{method.upper()} {path} 应 4xx，实际 {r.status_code}"
    assert "OverflowError" not in r.text, "错误响应不得回显异常类名"
    assert "Traceback" not in r.text


# ===========================================================================
# 3) P1-C1/C2/C4：脱敏字形盲区 + 身份证变体 + 银行卡
# ===========================================================================

PHONE_GLYPH_VARIANTS = [
    ("keycap", "1️⃣3️⃣8️⃣1️⃣2️⃣3️⃣4️⃣5️⃣6️⃣7️⃣8️⃣"),
    ("circled", "①③⑧①②③④⑤⑥⑦⑧"),
    ("superscript", "¹³⁸¹²³⁴⁵⁶⁷⁸"),
    ("arabic_indic", "١٣٨١٢٣٤٥٦٧٨"),
    ("devanagari", "१३८१२३४५६७८"),
    ("bengali", "১৩৮১২৩৪৫৬৭৮"),
    ("roman", "ⅠⅢⅧⅠⅡⅢⅣⅤⅥⅦⅧ"),
    ("greek_omicron", "1381234567Ο"),
    ("cyrillic_o", "1381234567О"),
    # W3 补修：西里尔混淆（З→3 / О→0 / Е→3 等）
    ("cyrillic_ze_o", "1З8ОО1З8ООО"),
    ("cyrillic_lower", "1з8оо1з8ооо"),
    ("cyrillic_ye", "1Е8ОО1Е8ООО"),
    ("cyrillic_prefix86", "861З8ОО1З8ООО"),
    ("cyrillic_prefix_plus", "+86 1З8ОО1З8ООО"),
    ("double_escaped", r"\u0031\u0033\u0038\u0031\u0032\u0033\u0034\u0035\u0036\u0037\u0038"),
]

ID_GLYPH_VARIANTS = [
    ("id_plain", "110101199001011234"),
    ("id_arabic_indic", "١١٠١٠١١٩٩٠٠١٠١١٢٣٤"),
    ("id_fullwidth", "１１０１０１１９９００１０１１２３４"),
]


def _luhn_complete(prefix: str) -> str:
    """给定前缀，附加 Luhn 校验位，返回完整银行卡号（16-19 位）。"""
    total = 0
    for i, ch in enumerate(reversed(prefix)):
        d = int(ch)
        if i % 2 == 0:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    check = (10 - (total % 10)) % 10
    return prefix + str(check)


BANK_CARD_VARIANTS = [
    ("plain16", "4111111111111111"),
    ("spaced", "4111 1111 1111 1111"),
    ("hyphenated", "4111-1111-1111-1111"),
    ("plain17", _luhn_complete("411111111111111")),
    ("plain19", _luhn_complete("41111111111111111")),
]


def _assert_variant_rejected(variant: str, residue: str) -> None:
    from app.services.desensitize import DesensitizeService

    hits = DesensitizeService.scan_sensitive(variant)
    assert hits, f"变体 {variant!r} 必须被 scan_sensitive 检出"

    with pytest.raises(Exception) as e:
        DesensitizeService.assert_clean(variant)
    assert getattr(e.value, "code", None) == "sensitive_data", f"变体 {variant!r} 必须被拒绝"

    red = DesensitizeService().regex_redact(variant)
    assert red["redacted"] != variant, f"变体 {variant!r} 必须被抹除"
    assert red["replaced"], f"变体 {variant!r} 必须产生 replaced 记录"
    assert residue not in red["redacted"], f"抹除后不得残留 {residue!r}"
    # 掩码后重扫必须干净（强断言）
    assert DesensitizeService.scan_sensitive(red["redacted"]) == [], \
        f"抹除结果 {red['redacted']!r} 仍含敏感信息"


@pytest.mark.parametrize("name,variant", PHONE_GLYPH_VARIANTS, ids=[n for n, _ in PHONE_GLYPH_VARIANTS])
def test_phone_glyph_variants_rejected(name, variant):
    _assert_variant_rejected(variant, "13812345678")


@pytest.mark.parametrize("name,variant", ID_GLYPH_VARIANTS, ids=[n for n, _ in ID_GLYPH_VARIANTS])
def test_id_card_variants_rejected(name, variant):
    _assert_variant_rejected(variant, "110101199001011234")


@pytest.mark.parametrize("name,variant", BANK_CARD_VARIANTS, ids=[n for n, _ in BANK_CARD_VARIANTS])
def test_bank_card_variants_rejected(name, variant):
    _assert_variant_rejected(variant, "4111111111111111")


def test_bank_card_luhn_negative_no_false_positive():
    """非 Luhn 的 16 位长数字串不得被当作银行卡误伤。"""
    from app.services.desensitize import DesensitizeService

    text = "参考号 1234567890123456 已登记"
    assert DesensitizeService.scan_sensitive(text) == []
    red = DesensitizeService().regex_redact(text)
    assert red["redacted"] == text and red["replaced"] == []


def test_qq_and_bare_wxid_regex(env):
    """P2 观察项顺手补 regex：裸 wxid / QQ 上下文。"""
    from app.services.desensitize import DesensitizeService

    assert DesensitizeService.scan_sensitive("加我 wxid_abc12345 详聊")
    assert DesensitizeService.scan_sensitive("QQ 12345678 联系")
    red = DesensitizeService().regex_redact("加我 wxid_abc12345 或 QQ 12345678")
    assert "wxid_abc12345" not in red["redacted"]
    assert "12345678" not in red["redacted"]


# ===========================================================================
# 4) P1-A2：并发 reset-key 表一致（enabled=1 仅 1 行）  [真实 uvicorn]
# ===========================================================================


def _find_free_port() -> int:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture(scope="module")
def live():
    """模块级真实 uvicorn（python -m app.run，proxy_headers=False）供并发用例。"""
    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="af_r2_live_"))
    port = _find_free_port()
    env = dict(os.environ)
    env.update({
        "AF_DATA_DIR": str(tmp),
        "AF_LLM_API_KEY": "",
        "AF_API_KEY": "",
        "AF_RATE_LIMIT_PER_MIN": "0",
        "PYTHONUNBUFFERED": "1",
    })
    log_file = tmp / "server.log"
    with open(log_file, "w", encoding="utf-8") as lf:
        proc = subprocess.Popen(
            [sys.executable, "-m", "app.run", "--host", "127.0.0.1", "--port", str(port)],
            cwd=str(ROOT), env=env,
            stdout=lf, stderr=subprocess.STDOUT,
        )
        base = f"http://127.0.0.1:{port}"
        ok = False
        for _ in range(100):
            if proc.poll() is not None:
                break
            try:
                if httpx.get(f"{base}/api/v1/system/health", timeout=2).status_code == 200:
                    ok = True
                    break
            except Exception:
                time.sleep(0.2)
    if not ok:
        proc.kill()
        pytest.fail(f"live uvicorn 未就绪（日志：{log_file}）")
    yield base, tmp, log_file
    proc.kill()
    proc.wait()


def _sqlite_enabled_admin_count(db_file: Path) -> int:
    with sqlite3.connect(str(db_file)) as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM api_keys WHERE role='admin' AND name='bootstrap' AND enabled=1"
        ).fetchone()
        return int(row[0])


def test_reset_key_concurrent_keeps_single_enabled(live):
    base, tmp, _ = live
    db_file = tmp / "af.db"

    def _reset(_):
        with httpx.Client(base_url=base, timeout=10) as c:
            r = c.post("/api/v1/auth/reset-key",
                       json={"confirm": True, "reason": REASON_20})
            return r.status_code

    with ThreadPoolExecutor(max_workers=5) as pool:
        codes = list(pool.map(_reset, range(10)))  # 10 次并发（2 轮 × 5 线程）

    assert all(c == 200 for c in codes), f"并发 reset 应全部 200，实际 {codes}"
    enabled = _sqlite_enabled_admin_count(db_file)
    assert enabled == 1, f"并发 reset 后 enabled=1 行数应为 1，实际 {enabled}"

    # 文件 key 与表中唯一 enabled 行哈希一致（锚点一致，无漂移）
    file_key = (tmp / "bootstrap_admin_key.txt").read_text(encoding="utf-8").strip()
    with sqlite3.connect(str(db_file)) as conn:
        row = conn.execute(
            "SELECT key_hash FROM api_keys WHERE role='admin' AND name='bootstrap' AND enabled=1"
        ).fetchone()
    assert row[0] == hashlib.sha256(file_key.encode("utf-8")).hexdigest()


# ===========================================================================
# 5) P1-A5：key 熵 >=128bit + 旧 8-hex 兼容
# ===========================================================================


def _hex_part(key: str) -> str:
    return key.split("_", 2)[2]


def test_new_key_entropy_128bit(tmp_path, monkeypatch):
    monkeypatch.setenv("AF_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    from app.deps import load_or_create_bootstrap_key

    key = load_or_create_bootstrap_key(get_settings())
    assert key.startswith("af_admin_")
    assert len(_hex_part(key)) == 32, f"新 key 应为 32-hex（128bit），实际 {_hex_part(key)!r}"


def test_reset_key_entropy_128bit(live):
    base, _, _ = live
    with httpx.Client(base_url=base, timeout=10) as c:
        r = c.post("/api/v1/auth/reset-key",
                   json={"confirm": True, "reason": REASON_20})
        assert r.status_code == 200
        new_key = r.json()["data"]["key"]
    assert len(_hex_part(new_key)) == 32, "reset-key 新 key 必须 >=128bit（32-hex）"


def test_old_format_8hex_key_still_loads(tmp_path, monkeypatch):
    """旧版 8-hex（32bit）key 文件兼容：读取复用、可登录。"""
    monkeypatch.setenv("AF_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AF_API_KEY", "")
    get_settings.cache_clear()
    old = "af_admin_deadbeef"
    (tmp_path / "bootstrap_admin_key.txt").write_text(old + "\n", encoding="utf-8")

    from app.deps import get_admin_key, load_or_create_bootstrap_key

    assert load_or_create_bootstrap_key(get_settings()) == old, "旧 8-hex key 必须被复用"
    assert get_admin_key(get_settings()) == old

    dbmod.ensure_schema()
    from app.deps import require_admin  # noqa: F401  # 仅确认模块导入不因格式校验崩溃


# ===========================================================================
# 6) P1-A4：local-login/current-key/reset-key Origin/Host/Sec-Fetch-Site
# ===========================================================================

def test_local_login_origin_host_secfetch_checks(api, tmp_path):
    # 伪造 Origin → 403
    r1 = api.post("/api/v1/auth/local-login", headers={"Origin": "http://evil.com"})
    assert r1.status_code == 403 and r1.json()["error"]["code"] == "origin_forbidden"

    # 伪造 Host（DNS-rebinding 场景）→ 403
    r2 = api.post("/api/v1/auth/local-login", headers={"Host": "evil.com"})
    assert r2.status_code == 403 and r2.json()["error"]["code"] == "host_forbidden"

    # Sec-Fetch-Site: cross-site → 403
    r3 = api.post("/api/v1/auth/local-login", headers={"Sec-Fetch-Site": "cross-site"})
    assert r3.status_code == 403

    # 合法本机 Origin → 200
    r4 = api.post("/api/v1/auth/local-login", headers={"Origin": "http://127.0.0.1:9200"})
    assert r4.status_code == 200

    # 无 Origin/Host（curl / MCP）→ 200
    r5 = api.post("/api/v1/auth/local-login")
    assert r5.status_code == 200

    # current-key / reset-key 同样受 Origin 约束
    r6 = api.get("/api/v1/auth/current-key", headers={"Origin": "http://evil.com"})
    assert r6.status_code == 403
    r7 = api.post("/api/v1/auth/reset-key",
                  json={"confirm": True, "reason": REASON_20},
                  headers={"Origin": "http://evil.com"})
    assert r7.status_code == 403


# ===========================================================================
# 7) P1-D4：30 并发 publish（真实 uvicorn）无 500 / 无误 404；
#    P1-4：同一检测并发发布只成功 1 次（去重）
# ===========================================================================

def test_concurrent_publish_no_500_no_false_404(live):
    base, tmp, log_file = live
    key = (tmp / "bootstrap_admin_key.txt").read_text(encoding="utf-8").strip()
    auth = {"X-API-Key": key}

    # 先造一条检测（供 publish 关联）
    with httpx.Client(base_url=base, timeout=10) as c:
        r = c.post("/api/v1/scan/text", json={"text": "稳赚不赔的理财项目，导师带你内幕消息"})
        assert r.status_code == 200
        det_id = r.json()["data"]["detection_id"]

    def _publish(i: int):
        with httpx.Client(base_url=base, timeout=30) as c:
            rr = c.post("/api/v1/cases/publish", json={
                "det_id": det_id,
                "redacted_payload": f"并发发布案例载荷编号 {i}，注意防骗",
                "confirm": True,
                "reason": REASON_20,
            }, headers=auth)
            return rr.status_code, rr.text

    with ThreadPoolExecutor(max_workers=30) as pool:
        results = list(pool.map(_publish, range(30)))

    codes = [c for c, _ in results]
    log_tail = ""
    if any(c in (500, 404) for c in codes):
        log_tail = "\nserver.log tail:\n" + log_file.read_text(encoding="utf-8", errors="replace")[-2000:]
    assert 500 not in codes, f"并发 publish 不得出现 500：{results}{log_tail}"
    assert 404 not in codes, f"并发 publish 不得误报 404：{results}{log_tail}"
    # P1-4（R1 严格验收）：同一检测仅一个案例——串行锁内先查重，恰一个 200 新建，
    # 其余 409 case_exists（异载荷）；不得出现 503（无锁竞争退路保留）
    assert all(c in (200, 409) for c in codes), f"仅允许 200（新建）/409（重复发布）：{results}"
    assert codes.count(200) == 1, f"同一检测并发发布只能成功 1 次（P1-4 去重）：{results}"

    # 去重闭环：同一检测最终只落一条案例
    with httpx.Client(base_url=base, timeout=10) as c:
        lst = c.get("/api/v1/cases", params={"page": 1, "page_size": 100}, headers=auth)
        items = lst.json()["data"]["items"]
    assert sum(1 for it in items if it["det_id"] == det_id) == 1, "同一检测只能有一条案例"


# ===========================================================================
# 8) P1-C6：gate_logs 链式承诺（全链校验 + 篡改检出 + audit-verify 端点）
# ===========================================================================

def test_gate_log_chain_verify_and_tamper_detect(api, tmp_path):
    auth = {"X-API-Key": _admin_key(tmp_path)}

    # 写 3 条 config.put（链式写）
    for val in (9.5, 9.0, 8.5):
        r = api.put("/api/v1/config", json={
            "key": "grading.l3_min", "value": val,
            "confirm": True, "reason": REASON_20,
        }, headers=auth)
        assert r.status_code == 200

    conn = dbmod.get_conn()
    from app.services.audit_log import verify_gate_chain

    v = verify_gate_chain(conn)
    assert v["valid"] is True and v["checked"] >= 3

    # audit-verify 端点（受保护）
    assert api.get("/api/v1/system/audit-verify").status_code == 401
    rv = api.get("/api/v1/system/audit-verify", headers=auth)
    assert rv.status_code == 200 and rv.json()["data"]["valid"] is True

    # 篡改某行 before → 链断裂
    conn.execute("UPDATE gate_logs SET before='{\"x\": 1}' "
                 "WHERE id=(SELECT MAX(id) FROM gate_logs)")
    conn.commit()
    v2 = verify_gate_chain(conn)
    assert v2["valid"] is False and v2["broken_at"] is not None
    rv2 = api.get("/api/v1/system/audit-verify", headers=auth)
    assert rv2.status_code == 409
    assert rv2.json()["error"]["code"] == "audit_chain_broken"


def test_gate_log_chain_written_by_sensitive_actions(api, tmp_path):
    """config.put / trap.retire / case.publish 均走链式写入（payload_hash=sha256(prev+...)）。"""
    from app.services.audit_log import GENESIS_HASH, verify_gate_chain

    conn = dbmod.get_conn()
    auth = {"X-API-Key": _admin_key(tmp_path)}

    api.put("/api/v1/config", json={
        "key": "speech.rule_min_llm", "value": 8.0,
        "confirm": True, "reason": REASON_20,
    }, headers=auth)

    rows = conn.execute("SELECT * FROM gate_logs ORDER BY id").fetchall()
    assert len(rows) >= 1
    assert rows[0]["prev_hash"] == GENESIS_HASH, "首行 prev_hash 必须为 GENESIS"
    for i in range(1, len(rows)):
        assert rows[i]["prev_hash"] == rows[i - 1]["payload_hash"], "相邻行 prev_hash 必须衔接"
    v = verify_gate_chain(conn)
    assert v["valid"] is True


def test_gate_log_chain_migration_from_old_schema(tmp_path, monkeypatch):
    """旧格式 gate_logs 库（无 prev_hash/payload_json 列）迁移后链式可验（P1-C6 迁移兼容）。"""
    import sqlite3

    old_db = tmp_path / "af.db"
    conn = sqlite3.connect(str(old_db))
    conn.execute(
        "CREATE TABLE gate_logs (id INTEGER PRIMARY KEY AUTOINCREMENT, action TEXT NOT NULL, "
        "payload_hash TEXT NOT NULL DEFAULT '', before TEXT NOT NULL DEFAULT '{}', "
        "after TEXT NOT NULL DEFAULT '{}', reason TEXT NOT NULL DEFAULT '', "
        "ts TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    for i in range(2):
        pj = '{"k": %d}' % i
        conn.execute(
            "INSERT INTO gate_logs (action, payload_hash, before, after, reason) VALUES (?,?,?,?,?)",
            ("config.put", hashlib.sha256(pj.encode()).hexdigest(),
             '{"before":%d}' % i, '{"after":%d}' % i, "r%d" % i),
        )
    conn.commit()
    conn.close()

    monkeypatch.setenv("AF_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    dbmod.close_all()
    dbmod.ensure_schema()

    from app.services.audit_log import GENESIS_HASH, verify_gate_chain

    conn = dbmod.get_conn()
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(gate_logs)")}
    assert {"prev_hash", "payload_json"} <= cols
    rows = conn.execute("SELECT * FROM gate_logs ORDER BY id").fetchall()
    assert rows[0]["prev_hash"] == GENESIS_HASH
    for i in range(1, len(rows)):
        assert rows[i]["prev_hash"] == rows[i - 1]["payload_hash"]
    assert verify_gate_chain(conn)["valid"] is True


# ===========================================================================
# 9) P1-E1：config 键名白名单（拒绝密钥类直写 + overrides 不回显）
# ===========================================================================

def test_config_whitelist_rejects_sensitive_and_unknown(api, tmp_path):
    auth = {"X-API-Key": _admin_key(tmp_path)}
    for bad_key in ("llm.api_key", "bootstrap.admin_key", "foo.bar", "llm.secret"):
        r = api.put("/api/v1/config", json={
            "key": bad_key, "value": "SK-SECRET" if "key" in bad_key else 1,
            "confirm": True, "reason": REASON_20,
        }, headers=auth)
        assert r.status_code == 422, f"键 {bad_key} 必须被白名单拒绝"
        assert r.json()["error"]["code"] == "config_key_not_allowed"

    # 白名单键允许
    r = api.put("/api/v1/config", json={
        "key": "llm.daily_budget", "value": 100,
        "confirm": True, "reason": REASON_20,
    }, headers=auth)
    assert r.status_code == 200

    # configs 直插敏感键 → GET overrides 不得回显（含历史残留场景）
    conn = dbmod.get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO configs (cfg_key, value, updated_at) "
        "VALUES ('llm.api_key', '\"SK-SECRET\"', datetime('now'))"
    )
    conn.execute(
        "INSERT OR REPLACE INTO configs (cfg_key, value, updated_at) "
        "VALUES ('bootstrap.admin_key', '\"hacked\"', datetime('now'))"
    )
    conn.commit()
    g = api.get("/api/v1/config").json()["data"]
    assert "llm.api_key" not in g["overrides"]
    assert "bootstrap.admin_key" not in g["overrides"]
    assert g["overrides"].get("llm.daily_budget") == 100