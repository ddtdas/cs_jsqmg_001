"""告警规则引擎（闸控）：只对 L3+ 推送，alerts 表落库、unread 标记。

对应主方案 §4.1 alerting 职责与 §11「L3+ 才推告警」合规闸控：
  - evaluate(): 分级完成后调用；grade ∈ {L3,L4,L5} 才写 alerts 表；L1/L2 静默
  - 幂等去重：同一 detection_id 只告警一次（payload 内含 detection_id 查询）
  - mark_read(): POST /api/v1/alerts/{id}/read
"""

from __future__ import annotations

import json

from ..utils import ApiError

ALERTABLE_GRADES = ("L3", "L4", "L5")


class AlertService:
    def evaluate(self, conn, *, detection_id: int, grade: str,
                 grade_reason: list | None = None, channel: str = "webui",
                 commit: bool = True) -> dict:
        """分级闸控：只对 L3+ 生成正式告警。返回 {alerted, alert_id?, duplicate?, level}。

        P1-C 修复：新增 commit 开关——grading.finalize 以 commit=False 并入单事务
        （先写 grade+event，再写告警，最后一次 commit，异常 rollback，三写原子）；
        直连调用（测试/其他流程）保持默认 commit=True，行为不变。
        """
        if grade not in ALERTABLE_GRADES:
            return {"alerted": False, "level": grade, "reason": "L1/L2 不生成正式告警（闸控）"}

        # 幂等去重：同一 detection_id 只告警一次。
        # P1-2：改用 json_extract 精确提取，避免 LIKE 数字前缀误匹配
        # （旧实现 '"detection_id": 5' 会把 det_id=50 误判为 5 的重复）。
        dup = conn.execute(
            "SELECT COUNT(*) AS n FROM alerts WHERE json_extract(payload, '$.detection_id') = ?",
            (detection_id,),
        ).fetchone()["n"]
        if dup:
            return {"alerted": False, "duplicate": True, "level": grade}

        payload = json.dumps({
            "detection_id": detection_id,
            "grade": grade,
            "reason": (grade_reason or [])[:6],
        }, ensure_ascii=False)
        cur = conn.execute(
            "INSERT INTO alerts (rule_id, level, channel, payload) VALUES (NULL, ?, ?, ?)",
            (grade, channel, payload),
        )
        # P1-A1：告警生成事件
        conn.execute(
            "INSERT INTO events (kind, payload) VALUES ('alert_created', ?)",
            (json.dumps({
                "alert_id": int(cur.lastrowid),
                "detection_id": detection_id,
                "grade": grade,
            }, ensure_ascii=False),),
        )
        if commit:
            conn.commit()
        return {"alerted": True, "alert_id": int(cur.lastrowid), "level": grade}

    def list_alerts(self, conn, *, level: str | None = None,
                    unread_only: bool = False, limit: int = 50) -> list[dict]:
        limit = max(1, min(limit, 500))
        sql = "SELECT * FROM alerts"
        conds: list[str] = []
        args: list = []
        if level:
            conds.append("level=?")
            args.append(level)
        if unread_only:
            conds.append("unread=1")
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        rows = conn.execute(sql, tuple(args)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["payload"] = json.loads(d["payload"] or "{}")
            except json.JSONDecodeError:
                pass
            out.append(d)
        return out

    def mark_read(self, conn, alert_id: int) -> dict:
        cur = conn.execute("UPDATE alerts SET unread=0 WHERE id=?", (alert_id,))
        if cur.rowcount == 0:
            raise ApiError("alert_not_found", f"告警不存在: id={alert_id}", 404)
        conn.commit()
        # P1-A1：告警已读事件
        conn.execute(
            "INSERT INTO events (kind, payload) VALUES ('alert_read', ?)",
            (json.dumps({"alert_id": alert_id}, ensure_ascii=False),),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM alerts WHERE id=?", (alert_id,)).fetchone()
        return dict(row)