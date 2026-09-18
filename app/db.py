"""SQLite 连接管理（线程安全）。

- 数据库文件：{data_dir}/af.db（WAL 模式，busy_timeout 防写锁冲突）
- 连接管理：threading.local 每个线程一条独立连接（check_same_thread=False），
  请求级依赖 get_db() 负责事务提交/回滚。
- ensure_schema() 幂等建表/迁移：§8 全部 24 张表 + 索引 + 种子话术词库（可重入）。

对应主方案 §8 数据模型（SQLite 默认，WAL；Postgres 预留切换）。
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from .config import Settings, get_settings

# 每个线程一条连接（供服务层/调度器/测试直连复用），避免跨线程复用 SQLite 连接
_local = threading.local()


def data_dir(settings: Settings | None = None) -> Path:
    """数据目录：相对路径基于项目根（app 包父目录）解析，与启动 cwd 无关。"""
    settings = settings or get_settings()
    p = Path(settings.data_dir)
    if not p.is_absolute():
        p = Path(__file__).resolve().parent.parent / p
    return p


def db_path(settings: Settings | None = None) -> Path:
    return data_dir(settings) / "af.db"


def _connect(settings: Settings) -> sqlite3.Connection:
    path = db_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        str(path),
        check_same_thread=False,
        timeout=5.0,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA busy_timeout=5000;")
    return conn


def get_conn() -> sqlite3.Connection:
    """返回当前线程的 SQLite 连接（首次调用时惰性创建）。"""
    settings = get_settings()
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = _connect(settings)
        _local.conn = conn
    return conn


def get_db():
    """FastAPI 请求级依赖：每个请求一条独立连接，结束自动提交/异常回滚并关闭。

    P1-D4：FastAPI 同步端点/依赖经 anyio 线程池调度，线程局部连接可能被并发请求
    跨线程复用并发生产生 sqlite3 状态错乱（database is locked / InterfaceError
    误 404 / no more rows available）。请求级连接保证同一连接绝不并发使用。
    """
    conn = _connect(get_settings())
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def ensure_schema() -> None:
    """建表/迁移（幂等可重入）：§8 全部 24 张表 + 索引 + 种子话术词库 + bootstrap key 登记。

    任何时刻重复调用都不报错、不重建已有表、不丢数据（CREATE TABLE IF NOT EXISTS
    + INSERT OR IGNORE 双保险）。
    """
    conn = get_conn()
    try:
        from .models import ALL_DDL, ensure_migrations, register_bootstrap_key, seed_leak_records
        from .services.speech_seed import seed_speech_patterns

        for ddl in ALL_DDL:
            conn.execute(ddl)
        ensure_migrations(conn)         # 增量迁移（老库补列，幂等）
        seed_speech_patterns(conn)      # 种子词库幂等入库（≥20 条）
        seed_leak_records(conn)      # 社工库种子幂等入库（leak_records：3 demo + 7 真实事件索引）
        register_bootstrap_key(conn)    # bootstrap admin key 哈希登记 api_keys（D6）
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def close_all() -> None:
    """关闭当前线程持有的连接（供测试/退出时清理）。"""
    conn = getattr(_local, "conn", None)
    if conn is not None:
        conn.close()
        _local.conn = None
