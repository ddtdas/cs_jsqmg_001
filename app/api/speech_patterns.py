"""话术词库管理 API（/api/v1/speech-patterns，全部端点 require_admin）。

对应设计《话术库适应更新》§1：
  - GET    /speech-patterns              分页列表（category/keyword 过滤，含 hit_count）
  - POST   /speech-patterns              新建词条（pattern 唯一 UNIQUE(pattern,category)、
                                          weight 1-10、category 6 类枚举）
  - PUT    /speech-patterns/{id}         编辑（敏感操作：confirm=true + reason≥20 + GateLog 审计）
  - POST   /speech-patterns/{id}/toggle  启停（停用后 rule_match 不再命中，热更新即时生效）
  - DELETE /speech-patterns/{id}         删除（敏感操作：confirm=true + reason≥20 + GateLog 审计）
  - POST   /speech-patterns/import       批量导入（INSERT OR IGNORE 幂等 UPSERT，{imported, skipped}）
  - GET    /speech-patterns/export       全量导出 JSON（备份/迁移）

统一 {ok,data}/{ok:false,error:{code,message}} 封包（§7.1，MCP 透传协议）。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from ..db import get_db
from ..deps import require_admin
from ..services.audit_log import write_gate_log
from ..services.speech_seed import ALLOWED_CATEGORIES
from ..utils import ApiError, ok

router = APIRouter(prefix="/speech-patterns", tags=["speech-patterns"])

REASON_MIN_LEN = 20


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _require_confirm(confirm: bool, reason: str) -> str:
    """敏感操作闸控：confirm=true 且 reason.strip()≥20 字，否则 403/422。返回去空格后的 reason。"""
    if not confirm:
        raise ApiError("confirm_required", "敏感操作必须 confirm=true（双确认）", 403)
    reason = reason.strip()
    if len(reason) < REASON_MIN_LEN:
        raise ApiError(
            "reason_too_short",
            f"reason 至少 {REASON_MIN_LEN} 字，当前 {len(reason)} 字",
            422,
        )
    return reason


def _check_category(category: str) -> None:
    if category not in ALLOWED_CATEGORIES:
        raise ApiError(
            "invalid_category",
            f"category 必须为 {'/'.join(sorted(ALLOWED_CATEGORIES))} 之一",
            422,
        )


def _get_or_404(db, pattern_id: int) -> dict:
    row = db.execute(
        "SELECT * FROM speech_patterns WHERE id=?", (pattern_id,)
    ).fetchone()
    if row is None:
        raise ApiError("pattern_not_found", f"词条不存在: id={pattern_id}", 404)
    return dict(row)


class PatternCreate(BaseModel):
    pattern: str = Field(min_length=1, max_length=200)
    regex: str | None = Field(default=None, max_length=1000)
    weight: float = Field(ge=1, le=10, description="权重 1-10")
    category: str
    enabled: bool | None = Field(default=None, description="缺省 1（启用）")


class PatternUpdate(BaseModel):
    pattern: str = Field(min_length=1, max_length=200)
    regex: str | None = Field(default=None, max_length=1000)
    weight: float = Field(ge=1, le=10, description="权重 1-10")
    category: str
    confirm: bool = Field(default=False)
    reason: str = Field(default="")


class ConfirmRequest(BaseModel):
    confirm: bool = Field(default=False)
    reason: str = Field(default="")


class ImportItem(BaseModel):
    pattern: str = Field(min_length=1, max_length=200)
    regex: str | None = Field(default=None, max_length=1000)
    weight: float = Field(ge=1, le=10)
    category: str


class ImportRequest(BaseModel):
    items: list[ImportItem] = Field(min_length=1, max_length=5000)


@router.get("", dependencies=[Depends(require_admin)])
def list_patterns(
    db=Depends(get_db),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
    category: str | None = Query(default=None),
    keyword: str | None = Query(default=None),
) -> dict:
    """分页列表：category 精确过滤 + keyword 模糊搜索（pattern/regex 包含匹配），含 hit_count。"""
    conds: list[str] = []
    args: list = []
    if category:
        conds.append("category = ?")
        args.append(category)
    kw = (keyword or "").strip()
    if kw:
        conds.append("(pattern LIKE ? OR regex LIKE ?)")
        args.extend([f"%{kw}%", f"%{kw}%"])
    where = (" WHERE " + " AND ".join(conds)) if conds else ""
    total = db.execute(
        f"SELECT COUNT(*) AS n FROM speech_patterns{where}", tuple(args)
    ).fetchone()["n"]
    rows = db.execute(
        f"SELECT * FROM speech_patterns{where} ORDER BY id LIMIT ? OFFSET ?",
        tuple(args + [page_size, (page - 1) * page_size]),
    ).fetchall()
    return ok({
        "page": page,
        "page_size": page_size,
        "total": total,
        "items": [dict(r) for r in rows],
    })


@router.post("", dependencies=[Depends(require_admin)])
def create_pattern(payload: PatternCreate, db=Depends(get_db)) -> dict:
    """新建词条：pattern 唯一（UNIQUE(pattern,category)）、weight 1-10、category 枚举。"""
    _check_category(payload.category)
    pattern = payload.pattern.strip()
    if not pattern:
        raise ApiError("invalid_pattern", "pattern 不能为空", 422)
    regex = (payload.regex or "").strip() or None
    dup = db.execute(
        "SELECT id FROM speech_patterns WHERE pattern=? AND category=?",
        (pattern, payload.category),
    ).fetchone()
    if dup is not None:
        raise ApiError("duplicate_pattern", "该 pattern+category 组合已存在", 409)
    enabled = 1 if (payload.enabled is None or payload.enabled) else 0
    try:
        cur = db.execute(
            "INSERT INTO speech_patterns (pattern, regex, weight, category, source, enabled) "
            "VALUES (?, ?, ?, ?, 'manual', ?)",
            (pattern, regex, payload.weight, payload.category, enabled),
        )
    except sqlite3.IntegrityError:
        raise ApiError("duplicate_pattern", "该 pattern+category 组合已存在", 409)
    db.commit()
    return ok(_get_or_404(db, cur.lastrowid))


@router.put("/{pattern_id}", dependencies=[Depends(require_admin)])
def update_pattern(pattern_id: int, payload: PatternUpdate, db=Depends(get_db)) -> dict:
    """编辑词条（敏感操作）：confirm=true + reason≥20，写 gate_logs(pattern.update, before/after)。"""
    reason = _require_confirm(payload.confirm, payload.reason)
    before = _get_or_404(db, pattern_id)
    _check_category(payload.category)
    pattern = payload.pattern.strip()
    if not pattern:
        raise ApiError("invalid_pattern", "pattern 不能为空", 422)
    regex = (payload.regex or "").strip() or None
    dup = db.execute(
        "SELECT id FROM speech_patterns WHERE pattern=? AND category=? AND id<>?",
        (pattern, payload.category, pattern_id),
    ).fetchone()
    if dup is not None:
        raise ApiError("duplicate_pattern", "该 pattern+category 组合已存在", 409)
    db.execute(
        "UPDATE speech_patterns SET pattern=?, regex=?, weight=?, category=? WHERE id=?",
        (pattern, regex, payload.weight, payload.category, pattern_id),
    )
    after = _get_or_404(db, pattern_id)
    write_gate_log(
        db,
        action="pattern.update",
        payload_json=json.dumps(
            {"action": "pattern.update", "pattern_id": pattern_id, "reason": reason},
            ensure_ascii=False, sort_keys=True,
        ),
        before=json.dumps(before, ensure_ascii=False, sort_keys=True),
        after=json.dumps(after, ensure_ascii=False, sort_keys=True),
        reason=reason,
        ts=_now(),
    )
    db.commit()
    return ok(after)


@router.post("/{pattern_id}/toggle", dependencies=[Depends(require_admin)])
def toggle_pattern(pattern_id: int, db=Depends(get_db)) -> dict:
    """启停切换：停用（enabled=0）后 rule_match 实时查询不再命中；启用即时恢复（热更新）。"""
    row = _get_or_404(db, pattern_id)
    new_enabled = 0 if row["enabled"] else 1
    db.execute(
        "UPDATE speech_patterns SET enabled=? WHERE id=?",
        (new_enabled, pattern_id),
    )
    db.commit()
    return ok({"id": pattern_id, "pattern": row["pattern"], "enabled": new_enabled})


@router.delete("/{pattern_id}", dependencies=[Depends(require_admin)])
def delete_pattern(pattern_id: int, payload: ConfirmRequest, db=Depends(get_db)) -> dict:
    """删除词条（敏感操作）：confirm=true + reason≥20，写 gate_logs(pattern.delete)。"""
    reason = _require_confirm(payload.confirm, payload.reason)
    before = _get_or_404(db, pattern_id)
    db.execute("DELETE FROM speech_patterns WHERE id=?", (pattern_id,))
    write_gate_log(
        db,
        action="pattern.delete",
        payload_json=json.dumps(
            {"action": "pattern.delete", "pattern_id": pattern_id,
             "pattern": before["pattern"], "reason": reason},
            ensure_ascii=False, sort_keys=True,
        ),
        before=json.dumps(before, ensure_ascii=False, sort_keys=True),
        after=json.dumps({"deleted": True}, ensure_ascii=False),
        reason=reason,
        ts=_now(),
    )
    db.commit()
    return ok({"deleted": True, "id": pattern_id})


@router.post("/import", dependencies=[Depends(require_admin)])
def import_patterns(payload: ImportRequest, db=Depends(get_db)) -> dict:
    """批量导入（幂等 UPSERT）：INSERT OR IGNORE 按 UNIQUE(pattern,category) 去重，可重复执行。

    返回 {imported, skipped}；写 gate_logs(pattern.import) 审计（import 幂等，非敏感闸控）。
    """
    for idx, item in enumerate(payload.items):
        _check_category(item.category)
        if not item.pattern.strip():
            raise ApiError("invalid_import_item", f"第 {idx + 1} 项 pattern 不能为空", 422)
    before_total = db.total_changes
    for item in payload.items:
        db.execute(
            "INSERT OR IGNORE INTO speech_patterns "
            "(pattern, regex, weight, category, source, enabled) VALUES (?, ?, ?, ?, 'import', 1)",
            (item.pattern.strip(), (item.regex or "").strip() or None,
             item.weight, item.category),
        )
    db.commit()
    imported = db.total_changes - before_total
    skipped = len(payload.items) - imported
    write_gate_log(
        db,
        action="pattern.import",
        payload_json=json.dumps(
            {"action": "pattern.import", "requested": len(payload.items),
             "imported": imported, "skipped": skipped},
            ensure_ascii=False, sort_keys=True,
        ),
        before=json.dumps({"requested": len(payload.items)}, ensure_ascii=False),
        after=json.dumps({"imported": imported, "skipped": skipped}, ensure_ascii=False),
        reason="批量导入话术词条（import 幂等 UPSERT）",
        ts=_now(),
    )
    db.commit()
    return ok({"imported": imported, "skipped": skipped})


@router.get("/export", dependencies=[Depends(require_admin)])
def export_patterns(db=Depends(get_db)) -> dict:
    """全量导出（备份/迁移）：返回全部词条（含 id/pattern/regex/weight/category/source/enabled/hit_count）。"""
    rows = db.execute("SELECT * FROM speech_patterns ORDER BY id").fetchall()
    return ok({"total": len(rows), "items": [dict(r) for r in rows]})
