"""蜜饵端点（R1）：/api/v1/traps 全套 + check-hit。

对应主方案 §4.2 traps.py 与 MCP af_list_traps / af_generate_trap_draft /
af_mark_trap_deployed / af_disable_trap / af_retire_trap 透传目标。
HITL（D3）：所有端点只生成/标记，不自动发布。
认证与审计（D6 / §11，P1-1/P1-2）：写操作一律 require_admin；
退饵为敏感操作，必须 confirm=true + reason≥20，成功后写 gate_logs 哈希链审计。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from ..db import get_db
from ..deps import require_admin
from ..services.audit_log import write_gate_log
from ..services.trap_engine import TrapEngine
from ..utils import ApiError, get_logger, ok

router = APIRouter(prefix="/traps", tags=["traps"])

log = get_logger("canary.traps")

_engine = TrapEngine()

REASON_MIN_LEN = 20

# R4-F6（队1 P2-10）：generate-draft 的 platform 埋设位置白名单。
# 与 collector.matrix 的 invalid_platform 422 口径对齐：任意字符串不再静默接受；
# 白名单覆盖常见埋设场景（评论区/私信/动态/问答/想法/文章/视频/直播/群聊/邮件/短信/其他）。
_ALLOWED_PLATFORMS: frozenset[str] = frozenset({
    "评论区", "私信", "动态", "问答", "想法", "文章", "视频", "直播",
    "微信群", "QQ群", "群聊", "邮件", "短信", "其他",
})


class CreateTrapRequest(BaseModel):
    bait_text: str = Field(min_length=4, max_length=2000, description="蜜饵文案")
    note: str | None = None


class GenerateDraftRequest(BaseModel):
    template_id: str | None = Field(default=None, description="离线模板 id（t_invest_flow 等）")
    platform: str | None = Field(default="评论区", description="埋设位置提示（限枚举，见 _ALLOWED_PLATFORMS）")
    note: str | None = None


class DeployRequest(BaseModel):
    target_url: str | None = Field(default=None, description="用户手动发布的目标 URL")


class CheckHitRequest(BaseModel):
    text: str = Field(min_length=1, max_length=20000, description="待检测的候选文本")
    detection_id: int | None = Field(
        default=None, ge=1,
        description="可选：指定联动升级的检测 id；缺省按 text 反查最近匹配检测（P1-C）",
    )


class RetireRequest(BaseModel):
    """退饵双确认（§11 敏感操作：confirm=true + reason≥20，GateLog 审计）。"""

    confirm: bool = False
    reason: str = Field(default="", description="操作理由（≥20 字）")


def _require_confirm(confirm: bool, reason: str) -> str:
    """敏感操作闸控：confirm=true 且 reason.strip()≥20 字，否则 403/422。返回去空格后的 reason。"""
    if not confirm:
        raise ApiError("confirm_required", "敏感操作必须 confirm=true（双确认）", 403)
    reason = reason.strip()
    if len(reason) < REASON_MIN_LEN:
        raise ApiError("reason_too_short",
                       f"reason 至少 {REASON_MIN_LEN} 字，当前 {len(reason)} 字", 422)
    return reason


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


@router.get("")
def list_traps(db=Depends(get_db), status: str | None = Query(default=None), limit: int = 100) -> dict:
    """蜜饵列表（可按状态过滤）。"""
    return ok(_engine.list_traps(db, status=status, limit=limit))


@router.post("", dependencies=[Depends(require_admin)])
def create_trap(payload: CreateTrapRequest, db=Depends(get_db)) -> dict:
    """创建蜜饵草稿（用户自拟文案，启发式伪装度）。写操作需 admin（P1-1）。"""
    return ok(_engine.create_draft(db, bait_text=payload.bait_text, note=payload.note))


@router.post("/generate-draft", dependencies=[Depends(require_admin)])
async def generate_draft(payload: GenerateDraftRequest, db=Depends(get_db)) -> dict:
    """LLM/模板生成蜜饵草稿（HITL：只产文案+指纹，不发布）。写操作需 admin（P1-1）。

    R4-F6（队1 P2-10）：platform 限枚举（_ALLOWED_PLATFORMS），非法值 422 invalid_platform。
    """
    platform = (payload.platform or "评论区").strip()
    if platform not in _ALLOWED_PLATFORMS:
        raise ApiError(
            "invalid_platform",
            f"未知埋设位置 {platform!r}（可选 {', '.join(sorted(_ALLOWED_PLATFORMS))}）",
            422,
        )
    return ok(await _engine.generate_draft(
        db,
        template_id=payload.template_id,
        platform=platform,
        note=payload.note,
    ))


@router.get("/{trap_id}")
def get_trap(trap_id: int, db=Depends(get_db)) -> dict:
    return ok(_engine.get(db, trap_id))


@router.post("/{trap_id}/deploy", dependencies=[Depends(require_admin)])
def deploy_trap(trap_id: int, payload: DeployRequest | None = None, db=Depends(get_db)) -> dict:
    """标记已部署（用户手动发布后显式调用，draft→active）。写操作需 admin（P1-1）。"""
    return ok(_engine.deploy(db, trap_id, target_url=payload.target_url if payload else None))


@router.post("/{trap_id}/monitor", dependencies=[Depends(require_admin)])
def monitor_trap(trap_id: int, db=Depends(get_db)) -> dict:
    """进入监控（active→monitored）。写操作需 admin（P1-1）。"""
    return ok(_engine.monitor(db, trap_id))


@router.post("/{trap_id}/disable", dependencies=[Depends(require_admin)])
def disable_trap(trap_id: int, db=Depends(get_db)) -> dict:
    """停用（enabled=0，仅 active/monitored）。写操作需 admin（P1-1）。"""
    return ok(_engine.disable(db, trap_id))


@router.post("/{trap_id}/retire", dependencies=[Depends(require_admin)])
def retire_trap(trap_id: int, payload: RetireRequest | None = None, db=Depends(get_db)) -> dict:
    """手动退役（draft/active/monitored/hit → retired）。

    敏感操作（§11 / P1-2）：confirm=true + reason≥20 双确认闸控，
    成功后写 gate_logs（action='trap.retire'，before/after/payload_hash）+ 事件。
    """
    if payload is None:
        raise ApiError("confirm_required", "敏感操作必须 confirm=true（双确认）", 403)
    reason = _require_confirm(payload.confirm, payload.reason)

    row = db.execute("SELECT * FROM honey_facts WHERE id=?", (trap_id,)).fetchone()
    if row is None:
        raise ApiError("trap_not_found", f"蜜饵不存在: id={trap_id}", 404)
    before = dict(row)

    result = _engine.retire(db, trap_id, reason="user")
    after = {k: result.get(k) for k in ("id", "status", "retired_reason", "enabled")}

    now = _now()
    payload_json = json.dumps(
        {"action": "trap.retire", "trap_id": trap_id, "reason": reason},
        ensure_ascii=False, sort_keys=True,
    )
    # P1-C6：GateLog 链式审计
    write_gate_log(
        db,
        action="trap.retire",
        payload_json=payload_json,
        before=json.dumps(before, ensure_ascii=False),
        after=json.dumps(after, ensure_ascii=False),
        reason=reason,
        ts=now,
    )
    db.commit()
    return ok(result)


@router.post("/{trap_id}/check-hit")
async def check_hit(trap_id: int, payload: CheckHitRequest, db=Depends(get_db)) -> dict:
    """输入候选文本检测踩饵；命中即退役（hit→retired，实锤 D2）。

    P2-4：由 GET 改为 POST（带状态迁移副作用，不符合 GET 语义）；
    请求体 {text: ...}。原 GET 路径返回 405。
    P1-C（R2 严格验收）：命中后与 job_trap_recheck 同口径联动升级——按显式
    detection_id 或 text 反查最近匹配检测，finalize(trap_hit_count=1) 升级 L5 +
    告警 + trap_hit_escalated 事件（source=manual_check_hit），消除"手工实锤
    不进案例管道"的残留面；未匹配到检测则返回 escalation.status=skipped，
    命中结果本身不受影响。
    """
    result = await _engine.check_hit(db, trap_id, payload.text)
    if result["hit"]:
        result["escalation"] = _escalate_manual_hit(
            db, trap_id=trap_id, text=payload.text, detection_id=payload.detection_id)
    return ok(result)


def _escalate_manual_hit(db, *, trap_id: int, text: str, detection_id: int | None) -> dict:
    """P1-C：手工 check-hit 命中后的检测升级（防御式：失败不影响命中主结果）。

    R3（P3-2）边界注明：反查用 content 精确匹配属 best-effort——detections.content
    可能被 text[:4000] 截断（speech_engine._persist）或 chat_import 脱敏后落库，
    与手工提交的原始 text 不一致时会漏检（status=skipped, reason=no_matching_detection）；
    显式 detection_id 路径不受此影响，客户端/MCP 应优先显式传参。
    """
    from ..services.grading import GradingService

    det_id = detection_id
    if det_id is None:
        row = db.execute(
            "SELECT id FROM detections WHERE content = ? ORDER BY id DESC LIMIT 1",
            (text,),
        ).fetchone()
        det_id = row["id"] if row else None
    if det_id is None:
        return {"status": "skipped", "reason": "no_matching_detection"}
    try:
        # 幂等（R3 抽取共享，P3-1）：已因 trap 命中升级 L5 → 跳过
        if GradingService.is_trap_escalated(db, det_id):
            return {"status": "skipped", "reason": "already_l5_trap_hit",
                    "detection_id": det_id}
        GradingService().finalize(db, detection_id=det_id, trap_hit_count=1)
        db.execute(
            "INSERT INTO events (kind, payload) VALUES ('trap_hit_escalated', ?)",
            (json.dumps({"trap_id": trap_id, "detection_id": det_id, "grade": "L5",
                         "source": "manual_check_hit"}, ensure_ascii=False),),
        )
        db.commit()
        return {"status": "escalated", "detection_id": det_id, "grade": "L5"}
    except Exception as exc:  # noqa: BLE001 —— 升级失败不影响 check-hit 主结果
        db.rollback()
        log.warning("manual check-hit escalation failed trap=%s det=%s: %s", trap_id, det_id, exc)
        return {"status": "failed", "detection_id": det_id, "error": str(exc)[:200]}