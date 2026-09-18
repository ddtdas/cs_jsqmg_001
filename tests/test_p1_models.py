"""P1 数据模型测试：表建表 / 种子词库 ≥20 / ensure_schema 幂等。

运行：pytest tests/test_p1_models.py -q（项目根）。
"""

from __future__ import annotations

import pytest

from app import db
from app.config import get_settings
from app.models import TABLE_NAMES
from app.services.speech_seed import ALLOWED_CATEGORIES, SEED_PATTERNS


@pytest.fixture()
def clean_db(tmp_path, monkeypatch):
    """隔离数据库：AF_DATA_DIR 指向临时目录，确保空库 + 首次迁移。"""
    monkeypatch.setenv("AF_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    db.close_all()
    db.ensure_schema()
    conn = db.get_conn()
    yield conn
    db.close_all()
    get_settings.cache_clear()


def _table_set(conn) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {r["name"] for r in rows}


def _snapshot(conn) -> dict:
    return {
        "tables": _table_set(conn),
        "rows": {
            t: conn.execute(f'SELECT COUNT(*) AS n FROM "{t}"').fetchone()["n"]
            for t in TABLE_NAMES
        },
    }


# ---- 建表 ----

def test_all_tables_created(clean_db):
    conn = clean_db
    existing = _table_set(conn)
    missing = set(TABLE_NAMES) - existing
    extra = existing - set(TABLE_NAMES)
    assert not missing, f"缺少表: {sorted(missing)}"
    assert not extra, f"多余表: {sorted(extra)}"
    assert len(TABLE_NAMES) == 24  # 22 基线 + cover_identities + contact_events（反钓鱼伪装，P19）


# ---- 种子话术语料库 ----

def test_seed_speech_patterns_at_least_20(clean_db):
    total = clean_db.execute("SELECT COUNT(*) AS n FROM speech_patterns").fetchone()["n"]
    seeds = clean_db.execute(
        "SELECT COUNT(*) AS n FROM speech_patterns WHERE source='seed'"
    ).fetchone()["n"]
    assert total == seeds, "表中不应混入非 seed 来源数据"
    assert len(SEED_PATTERNS) >= 20
    assert seeds >= 20, f"种子词库应 ≥20 条，实际 {seeds}"


def test_seed_categories_valid(clean_db):
    cats = {
        r["category"]
        for r in clean_db.execute("SELECT DISTINCT category FROM speech_patterns")
    }
    assert cats <= set(ALLOWED_CATEGORIES), f"存在非法分类: {cats - set(ALLOWED_CATEGORIES)}"


def test_seed_weights_in_range(clean_db):
    bad = clean_db.execute(
        "SELECT COUNT(*) AS n FROM speech_patterns WHERE weight < 0 OR weight > 10"
    ).fetchone()["n"]
    assert bad == 0


# ---- 幂等迁移 ----

def test_ensure_schema_idempotent(clean_db):
    conn = clean_db
    before = _snapshot(conn)
    db.ensure_schema()   # 第二次执行
    db.ensure_schema()   # 第三次执行
    after = _snapshot(conn)
    assert before == after, "重复 ensure_schema 不应改变表结构或行数"


def test_ensure_schema_keeps_existing_rows(clean_db):
    """已有业务数据在重复迁移后必须保留。"""
    conn = clean_db
    conn.execute(
        "INSERT INTO detections (source, text_hash, content, rule_score) VALUES (?,?,?,?)",
        ("import", "abc123", "测试文本", 3.5),
    )
    conn.commit()
    db.ensure_schema()
    row = conn.execute(
        "SELECT source, rule_score FROM detections WHERE text_hash='abc123'"
    ).fetchone()
    assert row is not None
    assert row["source"] == "import"
    assert row["rule_score"] == 3.5


def test_register_bootstrap_key_reusable(clean_db, monkeypatch, tmp_path):
    """register_bootstrap_key 幂等：重复调用不重复插入（P1/D6）。"""
    from app.models import register_bootstrap_key

    conn = clean_db
    # 先写 bootstrap key 文件（register 依赖 get_admin_key 读取）
    from app.deps import load_or_create_bootstrap_key

    key = load_or_create_bootstrap_key(get_settings())
    assert key
    n1 = register_bootstrap_key(conn)
    n2 = register_bootstrap_key(conn)
    assert n1 == n2 == 1
    roles = {r["role"] for r in conn.execute("SELECT DISTINCT role FROM api_keys")}
    assert "admin" in roles
