"""检测详情聚合端点：GET /api/v1/detections/{id}/full、/{id}/platform-link。

为 WebUI 详情抽屉提供一次性聚合数据（告警原因 + 上下文 + 平台跳转 + 详细分析），
MCP 透传层可映射为 af_detection_full / af_detection_platform_link。

统一封包 {ok,data}（MCP 依赖）。详情含对话原文（content），读取需 admin（数据最小化）。
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends

from ..db import get_db
from ..deps import require_admin
from ..utils import ApiError, ok

router = APIRouter(prefix="/detections", tags=["detections"])

# 平台首页兜底（target_url 缺失时 iframe 降级跳转目标）
PLATFORM_HOME: dict[str, str] = {
    "zhihu": "https://www.zhihu.com",
    "weibo": "https://weibo.com",
    "wechat": "",
    "douyin": "https://www.douyin.com",
    "rsshub": "https://rsshub.app",
    "telegram": "https://t.me",
    "discord": "https://discord.com",
    "other": "",
}


def _json_or(value, default):
    if value is None:
        return default
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return default


@router.get("/{det_id}/platform-link", dependencies=[Depends(require_admin)])
def detection_platform_link(det_id: int, db=Depends(get_db)) -> dict:
    """平台跳转 URL：优先 target_url（问题点），否则平台首页兜底。"""
    row = db.execute(
        "SELECT id, platform, target_url FROM detections WHERE id=?", (det_id,)
    ).fetchone()
    if row is None:
        raise ApiError("detection_not_found", f"检测不存在: id={det_id}", 404)
    platform = row["platform"] or "other"
    url = row["target_url"] or PLATFORM_HOME.get(platform, "")
    return ok({
        "detection_id": det_id,
        "platform": platform,
        "url": url,
        "home_fallback": not bool(row["target_url"]),
    })


@router.get("/{det_id}/full", dependencies=[Depends(require_admin)])
def detection_full(det_id: int, db=Depends(get_db)) -> dict:
    """检测详情聚合：原文 + 命中话术 + 分级证据 + 关联告警/账号/蜜饵/案例/事件 + 平台跳转。"""
    row = db.execute("SELECT * FROM detections WHERE id=?", (det_id,)).fetchone()
    if row is None:
        raise ApiError("detection_not_found", f"检测不存在: id={det_id}", 404)
    det = dict(row)

    # 命中话术明细（join 词库得到 pattern/category）
    hits = [
        dict(r) for r in db.execute(
            """
            SELECT sh.id, sh.matched_text, sh.score, sp.pattern, sp.category, sp.weight
            FROM speech_hits sh LEFT JOIN speech_patterns sp ON sp.id = sh.pattern_id
            WHERE sh.det_id = ?
            ORDER BY sh.score DESC
            """,
            (det_id,),
        ).fetchall()
    ]

    # 关联告警（payload.detection_id 精确匹配）
    # P1-2：json_extract 精确提取，覆盖任意 JSON 序列化形态（带空格/紧凑/无引号 key），
    # 且不会把 det_id=50 误匹配到已有 '"detection_id": 5' 的告警。
    alerts = [
        dict(r) for r in db.execute(
            "SELECT * FROM alerts WHERE json_extract(payload, '$.detection_id') = ? ORDER BY id DESC",
            (det_id,),
        ).fetchall()
    ]
    for a in alerts:
        a["payload"] = _json_or(a.get("payload"), {})

    # 关联案例
    cases = [dict(r) for r in db.execute(
        "SELECT id, det_id, redacted_payload, graph_tags, published_at FROM cases WHERE det_id=?",
        (det_id,),
    ).fetchall()]
    for c in cases:
        c["graph_tags"] = _json_or(c.get("graph_tags"), [])

    # 关联蜜饵（命中过该检测的蜜饵）
    traps = [dict(r) for r in db.execute(
        """
        SELECT hf.id, hf.bait_text, hf.status, hf.hit_count, hf.fingerprint
        FROM honey_facts hf
        WHERE hf.bait_text IN (
            SELECT DISTINCT sh.matched_text FROM speech_hits sh WHERE sh.det_id = ?
        )
        ORDER BY hf.id DESC
        """,
        (det_id,),
    ).fetchall()]

    # 关联账号（精确关联）：accounts.trap_hit_ids 存的是蜜饵记录 id（honey_facts.id），
    # 与 detections.id 是不同 id 空间，不能直接 det_id in trap_hit_ids（旧实现几乎永不命中）。
    # 正确定位：本检测匹配到的蜜饵（与上方 traps 同一口径：speech_hits.matched_text == honey_facts.bait_text）
    # 与账号 trap_hit_ids 取交集；不做 signals 文本子串匹配（会被 checked_at 日期等数字误命中），
    # 也不再用 accounts[:3] 兜底（会把无关账号硬塞进关联列表）。
    trap_ids = [r["id"] for r in db.execute(
        """
        SELECT hf.id FROM honey_facts hf
        WHERE hf.bait_text IN (
            SELECT DISTINCT sh.matched_text FROM speech_hits sh WHERE sh.det_id = ?
        )
        """,
        (det_id,),
    ).fetchall()]
    related_accounts = []
    trap_id_set = {str(t) for t in trap_ids}
    if trap_id_set:
        accounts = [dict(r) for r in db.execute(
            "SELECT id, url_name, risk_level, signals, trap_hit_ids, updated_at FROM accounts ORDER BY id DESC"
        ).fetchall()]
        for a in accounts:
            a["signals"] = _json_or(a.get("signals"), {})
            a["trap_hit_ids"] = _json_or(a.get("trap_hit_ids"), [])
            hit_ids = {str(h) for h in a["trap_hit_ids"]}
            if hit_ids & trap_id_set:
                related_accounts.append(a)

    # 关联事件（payload.detection_id 精确匹配；同 P1-2：json_extract 防数字前缀误匹配）
    events = [
        dict(r) for r in db.execute(
            "SELECT id, kind, payload, ts FROM events "
            "WHERE json_extract(payload, '$.detection_id') = ? "
            "ORDER BY id DESC LIMIT 50",
            (det_id,),
        ).fetchall()
    ]
    for e in events:
        e["payload"] = _json_or(e.get("payload"), {})

    platform = det.get("platform") or "other"
    url = det.get("target_url") or PLATFORM_HOME.get(platform, "")

    return ok({
        "detection": {
            "id": det["id"],
            "source": det.get("source"),
            "platform": platform,
            "content": det.get("content") or "",
            "rule_score": det.get("rule_score"),
            "ml_score": det.get("ml_score"),
            "llm_verdict": det.get("llm_verdict"),
            "judge_confidence": det.get("judge_confidence"),
            "grade": det.get("grade"),
            "grade_reason": _json_or(det.get("grade_reason"), []),
            "se_factors": _json_or(det.get("se_factors"), {}),
            "status": det.get("status"),
            "created_at": det.get("created_at"),
            "target_url": det.get("target_url"),
        },
        "speech_hits": hits,
        "alerts": alerts,
        "cases": cases,
        "traps": traps,
        "accounts": related_accounts,
        "events": events,
        "platform_link": {"platform": platform, "url": url},
    })
