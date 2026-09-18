"""知识库蒸馏 API（/api/v1/knowledge，全部 require_admin，统一 ok/err 封包）。

《知识库蒸馏》§七：
  GET   /knowledge                  分页列表（ktype/keyword/min_harm/sort）
  GET   /knowledge/search           检索（q + ktype/min_harm/days，weighted_score 排序）
  POST  /knowledge/import           批量导入 {items:[{subject,content,ktype?,harm?}]} → {imported, distilled}
  POST  /knowledge/import-text      整段情报文本切分蒸馏 {text, source?}
  POST  /knowledge/{id}/harm        人工调分 {delta, confirm, reason}（1-10 钳制）
  POST  /knowledge/{id}/verified    人工复核 {confirm, reason}
  POST  /knowledge/{id}/to-pattern  蒸馏成语条 {confirm, reason, category}
  POST  /knowledge/{id}/disable     下架 {confirm}
  DELETE /knowledge/{id}            删除 {confirm, reason}

合规（§十）：调分/复核/to-pattern/删除 = confirm + reason≥20 + gate_logs 审计；
disable 仅 confirm；导入失败/越界 422；不存在 404。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from ..db import get_db
from ..deps import require_admin
from ..services.audit_log import write_gate_log
from ..services.knowledge import KnowledgeDistiller
from ..utils import ApiError, ok

router = APIRouter(prefix="/knowledge", tags=["knowledge"])

REASON_MIN_LEN = 20
_service = KnowledgeDistiller()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _require_confirm(confirm: bool, reason: str) -> str:
    """敏感操作闸控：confirm=true 且 reason.strip()≥20 字，否则 403/422。"""
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


def _require_confirm_only(confirm: bool) -> None:
    if not confirm:
        raise ApiError("confirm_required", "敏感操作必须 confirm=true（双确认）", 403)


class KnowledgeImportItem(BaseModel):
    subject: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=10000)
    ktype: str | None = Field(default=None)
    harm: float | None = Field(default=None, ge=1, le=10)
    source: str | None = Field(default=None)
    tags: list[str] | None = Field(default=None)


class KnowledgeImportRequest(BaseModel):
    items: list[KnowledgeImportItem] = Field(min_length=1, max_length=200)


class ImportTextRequest(BaseModel):
    text: str = Field(min_length=1, max_length=20000)
    source: str | None = Field(default=None)


class HarmRequest(BaseModel):
    delta: float = Field(ge=-10, le=10)
    confirm: bool = Field(default=False)
    reason: str = Field(default="")


class ConfirmReasonRequest(BaseModel):
    confirm: bool = Field(default=False)
    reason: str = Field(default="")


class ToPatternRequest(BaseModel):
    confirm: bool = Field(default=False)
    reason: str = Field(default="")
    category: str = Field(default="", min_length=1)


class ConfirmOnlyRequest(BaseModel):
    confirm: bool = Field(default=False)


def _item_dict(p: KnowledgeImportItem) -> dict:
    out: dict = {"subject": p.subject, "content": p.content}
    for key in ("ktype", "harm", "source", "tags"):
        val = getattr(p, key)
        if val is not None:
            out[key] = val
    return out


@router.get("", dependencies=[Depends(require_admin)])
def list_knowledge(
    db=Depends(get_db),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
    ktype: str | None = Query(default=None),
    keyword: str | None = Query(default=None),
    min_harm: float | None = Query(default=None),
    sort: str = Query(default="weighted"),
) -> dict:
    """分页列表：ktype 精确 / keyword 模糊（subject/content/keywords）/ min_harm 过滤。"""
    conds: list[str] = []
    args: list = []
    if ktype:
        conds.append("ktype = ?")
        args.append(ktype)
    kw = (keyword or "").strip()
    if kw:
        conds.append("(subject LIKE ? OR content LIKE ? OR keywords LIKE ?)")
        args.extend([f"%{kw}%", f"%{kw}%", f"%{kw}%"])
    if min_harm is not None:
        conds.append("harm_score >= ?")
        args.append(float(min_harm))
    where = (" WHERE " + " AND ".join(conds)) if conds else ""
    total = db.execute(
        f"SELECT COUNT(*) AS n FROM knowledge_items{where}", tuple(args)
    ).fetchone()["n"]
    order = "created_at DESC, id DESC" if sort == "created" else "weighted_score DESC, id DESC"
    rows = db.execute(
        f"SELECT * FROM knowledge_items{where} ORDER BY {order} LIMIT ? OFFSET ?",
        tuple(args + [page_size, (page - 1) * page_size]),
    ).fetchall()
    return ok({
        "page": page,
        "page_size": page_size,
        "total": total,
        "items": [_service._row_to_dict(r) for r in rows],
    })


@router.get("/search", dependencies=[Depends(require_admin)])
def search_knowledge(
    q: str = Query(default="", min_length=1),
    ktype: str | None = Query(default=None),
    min_harm: float | None = Query(default=None),
    days: int | None = Query(default=None, ge=1),
    db=Depends(get_db),
) -> dict:
    """检索（§六.4）：按 weighted_score 降序返回相关条目。"""
    items = _service.search(q, db, ktype=ktype, min_harm=min_harm, days=days)
    return ok({"q": q, "total": len(items), "items": items})


@router.post("/import", dependencies=[Depends(require_admin)])
def import_knowledge(payload: KnowledgeImportRequest, db=Depends(get_db)) -> dict:
    """批量导入 + 蒸馏入库（幂等 upsert：subject+ktype 冲突则更新）。"""
    items = [_item_dict(p) for p in payload.items]
    return ok(_service.import_items(items, db))


@router.post("/import-text", dependencies=[Depends(require_admin)])
def import_knowledge_text(payload: ImportTextRequest, db=Depends(get_db)) -> dict:
    """整段情报文本 → 自动切分 → 逐段蒸馏入库。"""
    source = (payload.source or "").strip() or "web"
    return ok(_service.import_text(payload.text, source=source, conn=db))


@router.post("/{knowledge_id}/harm", dependencies=[Depends(require_admin)])
def adjust_harm(knowledge_id: int, payload: HarmRequest, db=Depends(get_db)) -> dict:
    """人工调分（敏感操作）：confirm+reason≥20；1-10 钳制并重算综合分；gate_logs 审计。"""
    reason = _require_confirm(payload.confirm, payload.reason)
    before = _service._get_or_404(db, knowledge_id)
    updated = _service.adjust_harm(knowledge_id, payload.delta, db)
    write_gate_log(
        db,
        action="knowledge.harm_adjust",
        payload_json=json.dumps(
            {"action": "knowledge.harm_adjust", "knowledge_id": knowledge_id,
             "delta": payload.delta, "reason": reason},
            ensure_ascii=False, sort_keys=True,
        ),
        before=json.dumps(
            {"harm_score": before["harm_score"], "subject": before["subject"]},
            ensure_ascii=False, sort_keys=True,
        ),
        after=json.dumps(
            {"harm_score": updated["harm_score"], "weighted_score": updated["weighted_score"]},
            ensure_ascii=False, sort_keys=True,
        ),
        reason=reason,
        ts=_now(),
    )
    db.commit()
    return ok(updated)


@router.post("/{knowledge_id}/verified", dependencies=[Depends(require_admin)])
def set_verified(knowledge_id: int, payload: ConfirmReasonRequest, db=Depends(get_db)) -> dict:
    """人工复核标记（敏感操作）：confirm+reason≥20 + gate_logs 审计。"""
    reason = _require_confirm(payload.confirm, payload.reason)
    before = _service._get_or_404(db, knowledge_id)
    updated = _service.set_verified(knowledge_id, db)
    write_gate_log(
        db,
        action="knowledge.verified",
        payload_json=json.dumps(
            {"action": "knowledge.verified", "knowledge_id": knowledge_id,
             "subject": updated["subject"], "reason": reason},
            ensure_ascii=False, sort_keys=True,
        ),
        before=json.dumps({"verified": before["verified"]}, ensure_ascii=False),
        after=json.dumps({"verified": updated["verified"]}, ensure_ascii=False),
        reason=reason,
        ts=_now(),
    )
    db.commit()
    return ok(updated)


@router.post("/{knowledge_id}/to-pattern", dependencies=[Depends(require_admin)])
def to_pattern(knowledge_id: int, payload: ToPatternRequest, db=Depends(get_db)) -> dict:
    """蒸馏成语条（敏感操作）：confirm+reason≥20 + category；写 speech_patterns + gate_logs。"""
    reason = _require_confirm(payload.confirm, payload.reason)
    return ok(_service.to_pattern(knowledge_id, payload.category.strip(), db, reason=reason))


@router.post("/{knowledge_id}/disable", dependencies=[Depends(require_admin)])
def disable_knowledge(knowledge_id: int, payload: ConfirmOnlyRequest, db=Depends(get_db)) -> dict:
    """下架（confirm 确认）：enabled=0，不再参与检索/消费。"""
    _require_confirm_only(payload.confirm)
    before = _service._get_or_404(db, knowledge_id)
    updated = _service.disable(knowledge_id, db)
    write_gate_log(
        db,
        action="knowledge.disable",
        payload_json=json.dumps(
            {"action": "knowledge.disable", "knowledge_id": knowledge_id,
             "subject": updated["subject"]},
            ensure_ascii=False, sort_keys=True,
        ),
        before=json.dumps({"enabled": before["enabled"]}, ensure_ascii=False),
        after=json.dumps({"enabled": updated["enabled"]}, ensure_ascii=False),
        reason="下架知识条目",
        ts=_now(),
    )
    db.commit()
    return ok(updated)


@router.delete("/{knowledge_id}", dependencies=[Depends(require_admin)])
def delete_knowledge(knowledge_id: int, payload: ConfirmReasonRequest, db=Depends(get_db)) -> dict:
    """删除知识条目（敏感操作）：confirm+reason≥20 + gate_logs 审计。"""
    reason = _require_confirm(payload.confirm, payload.reason)
    before = _service._get_or_404(db, knowledge_id)
    result = _service.delete(knowledge_id, db)
    write_gate_log(
        db,
        action="knowledge.delete",
        payload_json=json.dumps(
            {"action": "knowledge.delete", "knowledge_id": knowledge_id,
             "subject": before["subject"], "reason": reason},
            ensure_ascii=False, sort_keys=True,
        ),
        before=json.dumps(
            {"subject": before["subject"], "ktype": before["ktype"],
             "harm_score": before["harm_score"]},
            ensure_ascii=False, sort_keys=True,
        ),
        after=json.dumps({"deleted": True}, ensure_ascii=False),
        reason=reason,
        ts=_now(),
    )
    db.commit()
    return ok(result)