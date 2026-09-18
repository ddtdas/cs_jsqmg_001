"""配置端点：GET /api/v1/config、PUT /api/v1/config（敏感操作，confirm+reason，D6）。

对应主方案 §4.2 config.py 与 MCP af_config / af_config_update 透传目标：
- GET：把词库阈值 / LLM 路由 / 降级链开关 / 分级与告警闸控参数合成为 JSON
  （常驻代码常量 + configs 表运行时覆盖值）；屏蔽密钥明文
- PUT：敏感操作 —— 必须 confirm=true 且 reason≥20 字，写入 configs 表 +
  gate_logs 哈希链审计（before/after/reason/payload_hash）；任一击穿即拒绝（403/422）
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from ..config import get_settings
from ..db import get_db
from ..deps import require_admin
from ..services import alerting, grading, llm_provider, speech_engine, trap_engine, zhihu_bridge
from ..services.audit_log import write_gate_log
from ..utils import ApiError, ok

router = APIRouter(prefix="/config", tags=["config"])

_KEY_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
REASON_MIN_LEN = 20

# P1-E1：configs 可写键白名单 —— 仅允许代码实际消费的热更新键（config_reader 接线点）：
#   grading.l3_min / speech.rule_min_llm / trap.simhash_threshold / trap.overlap_threshold /
#   llm.max_fail_streak / llm.daily_budget / llm.cooldown_seconds / zhihu.ingest_near_dup_threshold
# 其余键（含密钥类键名：api_key/secret/password/token/key 等）一律拒绝直写，防明文密钥入库。
_ALLOWED_CONFIG_KEYS: frozenset[str] = frozenset({
    "grading.l3_min",
    "speech.rule_min_llm",
    "trap.simhash_threshold",
    "trap.overlap_threshold",
    "llm.max_fail_streak",
    "llm.daily_budget",
    "llm.cooldown_seconds",
    "zhihu.ingest_near_dup_threshold",
})
_SENSITIVE_KEY_PARTS: tuple[str, ...] = (
    "api_key", "secret", "password", "passwd", "token", "credential", "private", "salt",
)


class ConfigUpdateRequest(BaseModel):
    key: str = Field(description="配置键（如 llm.daily_budget / grading.l3_min）")
    value: object = Field(description="JSON 值（数字/字符串/布尔/列表/对象）")
    confirm: bool = Field(description="敏感操作双确认：必须为 true")
    reason: str = Field(min_length=1, description="操作理由（≥20 字）")


@router.get("")
def get_config(db=Depends(get_db)) -> dict:
    """当前配置合成（阈值/LLM 路由/降级链 + configs 表覆盖值；不泄露密钥）。"""
    settings = get_settings()
    db_rows = {r["cfg_key"]: r["value"] for r in db.execute("SELECT cfg_key, value FROM configs")}
    overrides = {}
    for k, raw in db_rows.items():
        # P1-E1：非白名单键（含历史残留的 llm.api_key/bootstrap.admin_key 等）不回显
        if k not in _ALLOWED_CONFIG_KEYS:
            continue
        try:
            overrides[k] = json.loads(raw)
        except json.JSONDecodeError:
            overrides[k] = raw

    return ok({
        "llm": {
            "base_url": settings.llm_base_url,
            "model": settings.llm_model,
            "timeout": settings.llm_timeout,
            "daily_budget": settings.llm_daily_budget,
            "api_key_set": bool(settings.llm_api_key),
            "configured": settings.llm_configured,
        },
        "degrade": {
            "max_fail_streak": llm_provider.MAX_FAIL_STREAK,
            "fence_open": llm_provider.FENCE_OPEN,
            "no_key_mode": not settings.llm_configured,
        },
        "zhihu": {
            "rate_limit": settings.zhihu_rate_limit,
            "ingest_near_dup_threshold": zhihu_bridge.INGEST_NEAR_DUP_THRESHOLD,  # R7(L2-B)
        },
        "speech": {
            "rule_min_llm": speech_engine.RULE_MIN_LLM,
            "pattern_count_hint": "见 speech_patterns 表",
        },
        "trap": {
            "simhash_threshold": trap_engine.SIMHASH_THRESHOLD,
            "overlap_threshold": trap_engine.OVERLAP_THRESHOLD,
        },
        "grading": {
            "l3_min": grading.L3_MIN,
            "levels": grading.GRADE_LEVELS,
            "alertable_grades": list(alerting.ALERTABLE_GRADES),
        },
        "overrides": overrides,
    })


@router.put("", dependencies=[Depends(require_admin)])
def put_config(payload: ConfigUpdateRequest, db=Depends(get_db)) -> dict:
    """敏感配置更新：require_admin + confirm+reason 双确认 + GateLog 审计（D6 / P1-1）。"""
    if not payload.confirm:
        raise ApiError("confirm_required", "敏感操作必须 confirm=true（双确认）", 403)
    reason = payload.reason.strip()
    if len(reason) < REASON_MIN_LEN:
        raise ApiError("reason_too_short",
                       f"reason 至少 {REASON_MIN_LEN} 字，当前 {len(reason)} 字", 422)
    if not _KEY_RE.match(payload.key):
        raise ApiError("invalid_config_key",
                       "key 须为小写字母开头，仅含 [a-z0-9_.-]", 422)
    # P1-E1：键名白名单（精确匹配可写键；含密钥类敏感键名一律拒绝）
    if payload.key not in _ALLOWED_CONFIG_KEYS:
        raise ApiError(
            "config_key_not_allowed",
            f"配置键 {payload.key!r} 不在白名单，拒绝写入（敏感/未定义键禁止直写）",
            422,
        )
    if any(part in payload.key for part in _SENSITIVE_KEY_PARTS):
        raise ApiError("config_key_not_allowed", "配置键含敏感字段名（api_key/secret 等），拒绝写入", 422)

    value = payload.value
    try:
        json.dumps(value)
    except (TypeError, ValueError):
        raise ApiError("invalid_config_value", "value 必须可 JSON 序列化", 422)

    old = db.execute("SELECT value FROM configs WHERE cfg_key=?", (payload.key,)).fetchone()
    before = {payload.key: json.loads(old["value"])} if old else {}

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    db.execute(
        "INSERT OR REPLACE INTO configs (cfg_key, value, updated_at) VALUES (?, ?, ?)",
        (payload.key, json.dumps(value, ensure_ascii=False), now),
    )

    after = {payload.key: value}
    payload_json = json.dumps({"key": payload.key, "value": value}, ensure_ascii=False, sort_keys=True)
    # P1-C6：GateLog 链式审计
    gate_log_id = write_gate_log(
        db,
        action="config.put",
        payload_json=payload_json,
        before=json.dumps(before, ensure_ascii=False),
        after=json.dumps(after, ensure_ascii=False),
        reason=reason,
        ts=now,
    )
    db.execute(
        "INSERT INTO events (kind, payload) VALUES ('config_updated', ?)",
        (json.dumps({"key": payload.key}, ensure_ascii=False),),
    )
    db.commit()
    return ok({
        "key": payload.key,
        "value": value,
        "updated_at": now,
        "audit": {"gate_log_id": gate_log_id, "action": "config.put", "before": before},
    })