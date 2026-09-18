"""案例库（R6，P6）：脱敏校验前置 → 入库 → 公开查询 → 骗术图谱聚合。

对应主方案 §4.1 cases 职责、§8 cases 表、§9 P6 DoD、§11（脱敏强制）：
- publish 强制脱敏：redacted_payload 必须先过 desensitize（代码层 assert_clean 校验，
  含手机/身份证/姓名等敏感信息一律拒绝入库，422）
- 图谱：graph_tags（骗术类型）与分级分布聚合，供 WebUI ECharts 直接渲染
- 公开查询只暴露脱敏载荷（D7：案例只存脱敏载荷）
- P1-D4：publish 写路径用 BEGIN IMMEDIATE 事务 + database-is-locked 重试，
  并发发布不再出现 500（database is locked）/ InterfaceError 误 404。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import datetime, timezone

from ..utils import ApiError, get_logger
from .audit_log import write_gate_log
from .desensitize import DesensitizeService


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# P1-D4：publish 写路径进程内串行锁（uvicorn 单进程线程池场景下彻底消除写锁竞争；
# 不用显式 BEGIN IMMEDIATE——CPython sqlite3 legacy 隔离模式下显式 BEGIN 与并发
# 组合会偶发 DatabaseError: no more rows available）
_PUBLISH_LOCK = threading.Lock()


class CaseService:
    def _derive_tags(self, conn, det_id: int) -> list[str]:
        """从检测命中的最高权重话术分类推导骗术标签。"""
        row = conn.execute(
            """
            SELECT sp.category FROM speech_hits sh
            JOIN speech_patterns sp ON sp.id = sh.pattern_id
            WHERE sh.det_id = ? ORDER BY sh.score DESC LIMIT 1
            """,
            (det_id,),
        ).fetchone()
        return [row["category"]] if row else ["other"]

    def publish(self, conn, *, det_id: int, redacted_payload: str,
                desensitize_log: list | None = None,
                graph_tags: list[str] | None = None) -> dict:
        """发布案例：脱敏强制校验前置；校验失败拒绝入库。

        P1-D4：整条写路径（校验+入库+事件）进程内串行化，并发发布不再出现
        500（database is locked）/ InterfaceError 误 404；跨进程仍以
        busy_timeout + 重试兜底。
        P1-4（R1 严格验收）：同一检测仅允许一个案例——发布前在同一串行锁内查重，
        同载荷幂等返回已存在案例（不再重复触发在线学习/蒸馏），异载荷 409 case_exists。
        """
        redacted_payload = (redacted_payload or "").strip()
        if not redacted_payload:
            raise ApiError("empty_payload", "案例载荷不能为空", 400)
        if conn.in_transaction:
            conn.rollback()  # 清理线程连接上的残留事务，避免写入状态错乱

        with _PUBLISH_LOCK:
            # P1-4（R1 严格验收）：同一检测只能发布一个案例（防重复刷库/毒化图谱/
            # 连锁重复在线学习与知识蒸馏）。同载荷重复发布 → 幂等返回已存在案例
            # （不再重复 boost/distill）；异载荷重复发布 → 409 case_exists 明确报错。
            existing = conn.execute(
                "SELECT id, redacted_payload FROM cases WHERE det_id=?", (det_id,)
            ).fetchone()
            if existing is not None:
                if existing["redacted_payload"] == redacted_payload:
                    return self.get(conn, existing["id"])
                raise ApiError(
                    "case_exists",
                    f"检测 #{det_id} 已发布案例 #{existing['id']}（同一检测不可重复发布）",
                    409,
                )
            last_err: Exception | None = None
            for attempt in range(4):
                try:
                    result = self._publish_tx(
                        conn, det_id=det_id, redacted_payload=redacted_payload,
                        desensitize_log=desensitize_log, graph_tags=graph_tags,
                    )
                    # P17 在线学习回写（触发点A）：发布成功 → 命中词条 weight+1（防御式，不影响主流程）
                    self._boost_hit_patterns(conn, det_id)
                    # P18 知识库蒸馏回写：发布成功的脱敏案例 → 知识条目（ktype=案例复盘，防御式）
                    self._distill_case_knowledge(conn, det_id, result)
                    return result
                except sqlite3.OperationalError as e:
                    last_err = e
                    conn.rollback()
                    time.sleep(0.05 * (attempt + 1))
            raise ApiError("db_busy", "数据库繁忙，请稍后重试", 503)

    def _publish_tx(self, conn, *, det_id: int, redacted_payload: str,
                    desensitize_log: list | None, graph_tags: list[str] | None) -> dict:
        """发布事务体（串行锁保护；sqlite3 默认延迟事务 + busy_timeout）。"""
        try:
            det = conn.execute("SELECT id FROM detections WHERE id=?", (det_id,)).fetchone()
            if det is None:
                raise ApiError("detection_not_found", f"检测记录不存在: id={det_id}", 404)

            # 代码层强制：入库前必须过脱敏校验（含敏感 → 拒绝）
            DesensitizeService.assert_clean(redacted_payload)

            tags = graph_tags or self._derive_tags(conn, det_id)
            cur = conn.execute(
                "INSERT INTO cases (det_id, redacted_payload, desensitize_log, graph_tags) VALUES (?, ?, ?, ?)",
                (det_id, redacted_payload,
                 json.dumps(desensitize_log or [], ensure_ascii=False),
                 json.dumps(tags, ensure_ascii=False)),
            )
            case_id = int(cur.lastrowid)
            conn.execute(
                "INSERT INTO events (kind, payload) VALUES ('case_published', ?)",
                (json.dumps({"case_id": case_id, "det_id": det_id, "tags": tags}, ensure_ascii=False),),
            )
            conn.commit()
            return self.get(conn, case_id)
        except Exception:
            conn.rollback()
            raise

    @staticmethod
    def _boost_hit_patterns(conn, det_id: int) -> dict:
        """在线学习回写（触发点A）：案例发布成功后，命中词条 weight+1（封顶 10）。

        《话术库适应更新》§2：人工确认并发布案例 = 实锤样本 → 命中话术权重上调，
        rule_match 实时查库 → 下次检测立即生效（热更新）。写 gate_logs(action='pattern.boost')。
        防御式：任何异常仅记日志并吞掉，绝不回抛影响发布主流程。
        """
        try:
            rows = conn.execute(
                """
                SELECT sp.id, sp.pattern, sp.weight, sp.category
                FROM speech_hits sh
                JOIN speech_patterns sp ON sp.id = sh.pattern_id
                WHERE sh.det_id = ?
                GROUP BY sp.id
                """,
                (det_id,),
            ).fetchall()
            boosted: list[dict] = []
            for r in rows:
                new_weight = min(10.0, float(r["weight"]) + 1.0)
                conn.execute(
                    "UPDATE speech_patterns SET weight=? WHERE id=?",
                    (new_weight, r["id"]),
                )
                boosted.append({
                    "pattern_id": r["id"], "pattern": r["pattern"],
                    "category": r["category"],
                    "before": float(r["weight"]), "after": new_weight,
                })
            for b in boosted:
                write_gate_log(
                    conn,
                    action="pattern.boost",
                    payload_json=json.dumps(
                        {"action": "pattern.boost", "pattern_id": b["pattern_id"],
                         "pattern": b["pattern"], "category": b["category"],
                         "det_id": det_id, "reason": "案例发布回写"},
                        ensure_ascii=False, sort_keys=True,
                    ),
                    before=json.dumps({"weight": b["before"]}, ensure_ascii=False),
                    after=json.dumps({"weight": b["after"]}, ensure_ascii=False),
                    reason="案例发布回写",
                )
            conn.commit()
            return {"boosted": len(boosted)}
        except Exception as e:  # 防御式：在线学习失败不影响发布主流程
            get_logger().warning("在线学习回写失败（det_id=%s）: %s", det_id, e)
            return {"boosted": 0, "error": str(e)}

    @staticmethod
    def _distill_case_knowledge(conn, det_id: int, case: dict) -> dict:
        """P18 知识库蒸馏回写：案例发布成功（人工确认）→ 蒸馏为知识条（ktype=案例复盘）。

        harm 由检测分级映射：L5→8 / L4→7 / L3→6，其余 5.0；source='case'。
        防御式：任何异常仅记日志并吞掉，绝不回抛影响案例发布主流程
        （与 P17 在线学习回写同级，异常不影响 publish 结果）。
        """
        try:
            from .knowledge import KnowledgeDistiller

            grade_row = conn.execute(
                "SELECT grade FROM detections WHERE id=?", (det_id,)
            ).fetchone()
            grade = (grade_row["grade"] if grade_row else "") or ""
            harm = {"L5": 8.0, "L4": 7.0, "L3": 6.0}.get(grade, 5.0)
            tags = case.get("graph_tags") or []
            subject = (tags[0] if tags else "案例") + "案例复盘"
            return KnowledgeDistiller().import_items(
                [{
                    "subject": subject,
                    "content": case.get("redacted_payload", ""),
                    "ktype": "案例复盘",
                    "harm": harm,
                    "source": "case",
                    "tags": tags,
                }],
                conn,
            )
        except Exception as exc:  # noqa: BLE001 —— 防御式，不影响发布主流程
            get_logger().warning("案例知识蒸馏失败（det_id=%s）: %s", det_id, exc)
            return {"imported": 0, "distilled": []}

    def get(self, conn, case_id: int) -> dict:
        row = conn.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()
        if row is None:
            raise ApiError("case_not_found", f"案例不存在: id={case_id}", 404)
        d = dict(row)
        d["graph_tags"] = json.loads(d["graph_tags"] or "[]")
        d["desensitize_log"] = json.loads(d["desensitize_log"] or "[]")
        return d

    def list_cases(self, conn, *, page: int = 1, page_size: int = 20, tag: str | None = None) -> dict:
        page = max(1, page)
        page_size = max(1, min(page_size, 100))
        conds: list[str] = []
        args: list = []
        if tag:
            conds.append("graph_tags LIKE ?")
            args.append(f'%"{tag}"%')
        where = (" WHERE " + " AND ".join(conds)) if conds else ""
        total = conn.execute(f"SELECT COUNT(*) AS n FROM cases{where}", tuple(args)).fetchone()["n"]
        rows = conn.execute(
            f"SELECT * FROM cases{where} ORDER BY id DESC LIMIT ? OFFSET ?",
            tuple(args + [page_size, (page - 1) * page_size]),
        ).fetchall()
        items = []
        for r in rows:
            d = dict(r)
            d["graph_tags"] = json.loads(d["graph_tags"] or "[]")
            d["desensitize_log"] = json.loads(d["desensitize_log"] or "[]")
            items.append(d)
        return {"page": page, "page_size": page_size, "total": total, "items": items}

    def graph(self, conn) -> dict:
        """骗术图谱聚合（供 ECharts）：by_tag 分布 + by_grade 分布。"""
        rows = conn.execute("SELECT graph_tags, det_id FROM cases").fetchall()
        by_tag: dict[str, int] = {}
        by_grade: dict[str, int] = {}
        for r in rows:
            for tag in json.loads(r["graph_tags"] or "[]"):
                by_tag[tag] = by_tag.get(tag, 0) + 1
            det = conn.execute("SELECT grade FROM detections WHERE id=?", (r["det_id"],)).fetchone()
            g = det["grade"] if det and det["grade"] else "L1"
            by_grade[g] = by_grade.get(g, 0) + 1
        return {
            "by_tag": [{"name": k, "value": v} for k, v in sorted(by_tag.items(), key=lambda x: -x[1])],
            "by_grade": [{"name": k, "value": v} for k, v in sorted(by_grade.items())],
            "total": len(rows),
        }