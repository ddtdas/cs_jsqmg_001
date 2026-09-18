"""configs 表运行时覆盖值读取（P2-1：修复"配置 PUT 只写不读"）。

轻量实现：每次决策点直查 configs 表（数据量极小，单行 KV），
缺失 / JSON 解析失败 / 类型异常一律回退调用方提供的 default。

命名空间约定（与 app/api/config.py PUT 的 key 一致）：
  grading.l3_min / speech.rule_min_llm / trap.simhash_threshold /
  trap.overlap_threshold / llm.max_fail_streak / llm.daily_budget ...
"""

from __future__ import annotations

import json
from typing import Any, Callable


def cfg_get(conn, key: str, default: Any = None) -> Any:
    """读取 configs 表覆盖值：缺失/解析失败返回 default。"""
    try:
        row = conn.execute("SELECT value FROM configs WHERE cfg_key=?", (key,)).fetchone()
    except Exception:
        return default
    if row is None:
        return default
    try:
        return json.loads(row["value"])
    except (json.JSONDecodeError, TypeError, KeyError):
        return default


def cfg_float(conn, key: str, default: float) -> float:
    v = cfg_get(conn, key, default)
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def make_reader(conn) -> Callable[[str, Any], Any]:
    """绑定某条连接/事务的 reader：cfg_reader(key, default) -> value。

    测试专用（R5 收尾标注）：生产无调用方（事实消费者为 cfg_float/cfg_get/
    default_cfg_reader），仅供 tests/test_r3_backend_fixes.py 构造注入 reader。
    """
    return lambda key, default=None: cfg_get(conn, key, default)


def default_cfg_reader(key: str, default: Any = None) -> Any:
    """进程内默认 reader：从当前线程的 DB 连接读取（供无 conn 上下文的模块使用）。

    LLM Provider 等无请求级连接的模块通过它读取覆盖值；DB 不可用/未建库时静默回退默认。
    """
    try:
        from .. import db as dbmod

        if not dbmod.db_path().exists():
            return default  # 库文件不存在：不创建空库，直接回退默认
        return cfg_get(dbmod.get_conn(), key, default)
    except Exception:
        return default
