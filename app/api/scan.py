"""话术检测端点：POST /scan/text、GET /scan/hits、POST/GET /scan/inbox。

对应主方案 §4.2 scan.py 与 MCP af_scan_text / af_scan_inbox 透传目标。
返回统一封包 {ok,data}，data 结构见 SpeechEngine.scan_text；
检测完成后自动进入五级分级（grading.finalize：落库 grade/grade_reason + L3+ 告警闸控）。
POST /scan/inbox：消费 CH-D 导入的 pending 检测（批量规则+LLM，原地更新 status→scanned）。
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from ..db import get_db
from ..deps import is_admin_request, require_admin
from ..services.grading import GradingService
from ..services.speech_engine import SpeechEngine
from ..utils import ApiError, ok

router = APIRouter(prefix="/scan", tags=["scan"])


class ScanTextRequest(BaseModel):
    text: str = Field(min_length=1, max_length=20000, description="待检测文本")
    source: str = Field(
        default="manual",
        pattern="^(zhihu|import|manual)$",
        description="来源：zhihu/import/manual",
    )


@router.post("/text")
async def scan_text(payload: ScanTextRequest, request: Request, db=Depends(get_db)) -> dict:
    """单条文本话术判定：规则 + LLM 双确认（LLM 可用且**已认证**时）+ 五级分级 + L3+ 告警闸控。

    R4-F5：strip 后为空 → 422 empty_text（纯空白不再 200 落库）。
    R4-F3：匿名调用（无有效 admin key）→ allow_llm=False 纯规则路径，
    不触发 LLM 调用（防匿名刷 LLM 预算）；已认证调用维持 LLM 复核。
    """
    text = (payload.text or "").strip()
    if not text:
        raise ApiError("empty_text", "文本不能为空", 422)
    engine = SpeechEngine()
    result = await engine.scan_text(
        payload.text, source=payload.source, conn=db,
        allow_llm=is_admin_request(request),
    )
    grades = GradingService().finalize(db, detection_id=result["detection_id"])
    result["grade"] = grades["grade"]
    result["grade_reason"] = grades["grade_reason"]
    return ok(result)


@router.get("/hits")
def scan_hits(request: Request, db=Depends(get_db), limit: int = 20) -> dict:
    """最近命中列表（detections 倒序，附带命中条数与 se_factors 推理链）。

    D7（最小化）：匿名请求（无有效 admin key）剥离 se_factors.evidence[].text
    原文片段，仅保留 factor/matched；带有效 admin key 返回完整推理链。
    """
    limit = max(1, min(limit, 200))
    rows = db.execute(
        """
        SELECT d.id, d.source, d.platform, d.rule_score, d.llm_verdict, d.judge_confidence, d.created_at,
               d.se_factors,
               (SELECT COUNT(*) FROM speech_hits sh WHERE sh.det_id = d.id) AS hit_count
        FROM detections d
        ORDER BY d.id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    items = [dict(r) for r in rows]
    for it in items:
        # 队2：se_factors 为 JSON 文本 → 解析为结构（无/坏 JSON → {}）
        raw = it.get("se_factors")
        try:
            it["se_factors"] = json.loads(raw) if raw else {}
        except (json.JSONDecodeError, TypeError):
            it["se_factors"] = {}
    # D7（最小化）：匿名请求剥离 se_factors.evidence[].text 原文片段（保留 factor/matched）
    if not is_admin_request(request):
        for it in items:
            sf = it.get("se_factors")
            if isinstance(sf, dict):
                evidence = sf.get("evidence")
                if isinstance(evidence, list):
                    for ev in evidence:
                        if isinstance(ev, dict):
                            ev.pop("text", None)
    return ok(items)


# --------------------------------------------------------------------------- inbox
@router.post("/inbox", dependencies=[Depends(require_admin)])
async def scan_inbox(db=Depends(get_db)) -> dict:
    """触发 inbox 批量检测：消费 CH-D 导入的 pending detections，
    逐条跑规则+LLM（原地更新），status pending→scanned，并进入五级分级。
    写操作需 admin（P1-1：批量消费是资源消耗型写路径）。"""
    engine = SpeechEngine()
    pending = db.execute(
        "SELECT id FROM detections WHERE status='pending' ORDER BY id ASC"
    ).fetchall()
    processed: list[dict] = []
    for row in pending:
        det_id = int(row["id"])
        result = await engine.analyze_detection(det_id, conn=db)
        grades = GradingService().finalize(db, detection_id=det_id)
        result["grade"] = grades["grade"]
        result["grade_reason"] = grades["grade_reason"]
        processed.append(result)

    # 通道状态快照（上下文提示，不阻塞检测）
    channels = [
        dict(r) for r in db.execute("SELECT channel, status, health FROM zhihu_sessions ORDER BY channel")
    ]
    return ok({
        "processed": len(processed),
        "items": processed,
        "zhihu_channels": channels,
    })


@router.get("/inbox", dependencies=[Depends(require_admin)])
def scan_inbox_result(db=Depends(get_db), limit: int = 50) -> dict:
    """查看 inbox 扫描结果（pending/scanned 的检测列表）。

    P2-7：返回含对话原文（d.content），读取需 admin（数据最小化/隐私）。
    """
    limit = max(1, min(limit, 200))
    rows = db.execute(
        """
        SELECT d.id, d.source, d.content, d.rule_score, d.llm_verdict, d.judge_confidence,
               d.grade, d.status, d.created_at,
               (SELECT COUNT(*) FROM speech_hits sh WHERE sh.det_id = d.id) AS hit_count
        FROM detections d
        WHERE d.status IN ('pending', 'scanned')
        ORDER BY d.id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return ok([dict(r) for r in rows])
