"""五级信号融合分级（R4，P5）：rule hits + LLM 复核 + 蜜饵实锤 + 账号信号 → L1–L5。

对应主方案 §4.1 grading 职责与 §9 P5 DoD：
- 分级必须可解释：输出 grade_reason 证据链（[{signal, weight, note}]），不只是一个数字
- 闸控（§11）：只对 L3+ 生成正式告警；≤L3 不生成正式报告（alerting.py 执行）
- 决策树：
    L5 = 蜜饵实锤命中（trap_hit_count>0）
      |  LLM 判 fraud + judge 置信度≥0.8 + 账号红信号
    L4 = LLM 复核判 fraud（+ 话术多证据）
    L3 = 多信号/高权重话术命中（未送 LLM 或 LLM 未确认 fraud；rule-only 封顶 L3）
    L2 = 弱信号（1 条低权重命中）
    L1 = 无信号
  注：LLM 不可用（降级链）时最多 L3——保守合规姿态。
"""

from __future__ import annotations

import json
import logging

from ..utils import ApiError
from .config_reader import cfg_float

logger = logging.getLogger(__name__)

# 分级档位描述（GET /grading/levels 返回）
GRADE_LEVELS: list[dict] = [
    {"level": "L1", "min_score": 0, "desc": "无信号：正常内容"},
    {"level": "L2", "min_score": 0.01, "desc": "弱信号：单条低权重话术命中"},
    {"level": "L3", "min_score": 10, "desc": "多信号/高权重话术命中（未送 LLM 或 LLM 未确认诈骗）；rule-only 封顶"},
    {"level": "L4", "min_score": None, "desc": "LLM 复核确认 fraud + 话术多证据"},
    {"level": "L5", "min_score": None, "desc": "蜜饵实锤命中，或 LLM fraud+高置信+账号红信号"},
]

L3_MIN = 10.0


class GradingService:
    """五级融合分级。数据从 detections/speech_hits 表读取，外部信号（蜜饵/账号）由参数注入。"""

    @staticmethod
    def is_trap_escalated(conn, detection_id: int) -> bool:
        """幂等判定（R3 抽取共享，P3-1）：检测是否已因蜜饵实锤升级为 L5。

        口径：grade=L5 且 grade_reason 证据链含 trap_hit 信号。供 scheduler
        （job_trap_recheck 多饵命中防重）与 api/traps（手工 check-hit 联动）
        共用，避免两处逻辑漂移。
        """
        row = conn.execute(
            "SELECT grade, grade_reason FROM detections WHERE id=?", (detection_id,)
        ).fetchone()
        if row is None or row["grade"] != "L5":
            return False
        try:
            reason = json.loads(row["grade_reason"] or "[]")
        except (json.JSONDecodeError, TypeError):
            return False
        return any(isinstance(s, dict) and s.get("signal") == "trap_hit" for s in reason)

    def grade_detection(
        self,
        conn,
        detection_id: int,
        *,
        trap_hit_count: int = 0,
        account_risk: str | None = None,
    ) -> dict:
        """对一次检测评分：返回 grade + 可解释证据链（不落库，供 explain 即时计算）。"""
        det = conn.execute("SELECT * FROM detections WHERE id=?", (detection_id,)).fetchone()
        if det is None:
            raise ApiError("detection_not_found", f"检测记录不存在: id={detection_id}", 404)

        hits = conn.execute(
            """
            SELECT sh.matched_text, sh.score, sp.pattern, sp.category
            FROM speech_hits sh JOIN speech_patterns sp ON sp.id = sh.pattern_id
            WHERE sh.det_id = ? ORDER BY sh.score DESC
            """,
            (detection_id,),
        ).fetchall()

        signals: list[dict] = []
        for h in hits:
            signals.append({
                "signal": "rule_hit",
                "weight": float(h["score"]),
                "note": f"话术'{h['pattern']}'命中（分类 {h['category']}）",
            })
        rule_total = round(sum(s["weight"] for s in signals), 2)
        # 有规则分但无命中明细时的兜底
        if not signals and float(det["rule_score"] or 0) > 0:
            signals.append({"signal": "rule_score", "weight": float(det["rule_score"]), "note": "规则得分（无命中明细）"})
            rule_total = round(float(det["rule_score"]), 2)

        llm_verdict = det["llm_verdict"]
        judge_conf = float(det["judge_confidence"]) if det["judge_confidence"] is not None else None

        if llm_verdict == "fraud":
            signals.append({"signal": "llm_fraud", "weight": 12.0, "note": "LLM 复核判定为诈骗"})
        elif llm_verdict == "suspicious":
            signals.append({"signal": "llm_suspicious", "weight": 6.0, "note": "LLM 复核判定为可疑"})
        if judge_conf is not None and judge_conf >= 0.8:
            signals.append({"signal": "llm_confidence_high", "weight": 2.0, "note": f"judge 置信度 {judge_conf:.2f}"})

        trap_hit = int(trap_hit_count or 0) > 0
        if trap_hit:
            signals.append({"signal": "trap_hit", "weight": 20.0, "note": f"蜜饵实锤命中 {int(trap_hit_count)} 次"})

        if account_risk == "red":
            signals.append({"signal": "account_red", "weight": 10.0, "note": "账号红信号"})
        elif account_risk == "yellow":
            signals.append({"signal": "account_yellow", "weight": 4.0, "note": "账号黄信号"})

        score = round(sum(s["weight"] for s in signals), 2)
        # P2-1：L3 判定阈值支持 configs 表热更新覆盖（grading.l3_min，env/常量默认 10.0）
        l3_min = cfg_float(conn, "grading.l3_min", L3_MIN)
        grade = self._decide(
            score=score,
            llm_fraud=(llm_verdict == "fraud"),
            judge_conf=judge_conf,
            trap_hit=trap_hit,
            account_red=(account_risk == "red"),
            l3_min=l3_min,
        )
        return {
            "detection_id": detection_id,
            "grade": grade,
            "score": score,
            "grade_reason": signals,          # 证据链（可解释核心）
            "rule_score": rule_total,
            "llm_verdict": llm_verdict,
            "judge_confidence": judge_conf,
        }

    @staticmethod
    def _decide(*, score: float, llm_fraud: bool, judge_conf: float | None,
                trap_hit: bool, account_red: bool, l3_min: float = L3_MIN) -> str:
        if trap_hit:
            return "L5"
        if llm_fraud and judge_conf is not None and judge_conf >= 0.8 and account_red:
            return "L5"
        if llm_fraud:
            return "L4"
        if score >= l3_min:
            return "L3"
        if score > 0:
            return "L2"
        return "L1"

    def finalize(
        self,
        conn,
        detection_id: int,
        *,
        trap_hit_count: int = 0,
        account_risk: str | None = None,
    ) -> dict:
        """评分 + 落库 grade/grade_reason + L3+ 告警闸控。扫描/检测主流程调用。

        P1-C 修复：三写（UPDATE detections + INSERT events + evaluate 告警）由
        原三次独立 commit 改为单事务——同一 conn，最后一次性 commit；
        任一步异常 rollback，不留部分提交（中断不产生"已分级无事件/有告警无分级"残态）。
        返回结构保持既有约定（grade/score/grade_reason 证据链）。

        P0-修复1（主动防御自动触发）：对分级 L3/L4/L5 的检测，在分级+告警落库
        完成后自动调用 SocLibService().analyze（post-commit，不影响分级主事务），
        并把 analyze 结果（掩码 hits 摘要）回写 se_factors.soc_analysis；
        analyze 保持「异常全降级不抛」，外层再包防御式 try/except（失败不影响分级主流程）。
        """
        r = self.grade_detection(conn, detection_id,
                                 trap_hit_count=trap_hit_count, account_risk=account_risk)
        try:
            conn.execute(
                "UPDATE detections SET grade=?, grade_reason=? WHERE id=?",
                (r["grade"], json.dumps(r["grade_reason"], ensure_ascii=False), detection_id),
            )

            # P1-A1：分级完成事件（detection_scanned 之后的第二个流节点）
            conn.execute(
                "INSERT INTO events (kind, payload) VALUES ('detection_graded', ?)",
                (json.dumps({
                    "detection_id": detection_id,
                    "grade": r["grade"],
                    "score": r["score"],
                }, ensure_ascii=False),),
            )

            from .alerting import AlertService

            # commit=False：告警写入并入本事务，最后一次 commit 统一提交（P1-C）
            AlertService().evaluate(conn, detection_id=detection_id,
                                    grade=r["grade"], grade_reason=r["grade_reason"],
                                    commit=False)
            conn.commit()
        except Exception:
            conn.rollback()
            raise

        # P0-修复1：L3/L4/L5 分级完成后自动触发社工库分析（post-commit + 防御式降级）
        if r["grade"] in ("L3", "L4", "L5"):
            self._auto_soc_analysis(conn, detection_id)
        return r

    @staticmethod
    def _auto_soc_analysis(conn, detection_id: int) -> None:
        """L3+ 检测自动触发社工库核查：IOC 提取 → analyze → 回写 se_factors.soc_analysis。

        防御式：analyze 内部已保证「异常全降级不抛」；此处再加一层 try/except 兜底，
        任何失败仅记日志，绝不影响分级主流程/主事务（analyze 只写 events(soc_lib_hit)
        与 se_factors 回写，均独立小事务）。
        """
        try:
            from .soc_lib import SocLibService

            svc = SocLibService()
            row = conn.execute(
                "SELECT content, se_factors FROM detections WHERE id=?", (detection_id,)
            ).fetchone()
            if row is None:
                return
            content = row["content"] or ""
            iocs = svc.extract_iocs(content)
            result = svc.analyze(detection_id, iocs, conn)

            # 回写 se_factors.soc_analysis：解析现有 JSON → 加 soc_analysis 键 → 存回（坏 JSON 容错）
            se_factors: dict = {}
            raw = row["se_factors"]
            if raw:
                try:
                    parsed = json.loads(raw)
                    if isinstance(parsed, dict):
                        se_factors = parsed
                except (json.JSONDecodeError, TypeError):
                    se_factors = {}
            se_factors["soc_analysis"] = result
            conn.execute(
                "UPDATE detections SET se_factors=? WHERE id=?",
                (json.dumps(se_factors, ensure_ascii=False), detection_id),
            )
            conn.commit()
        except Exception:
            logger.exception("soc_lib auto-analysis degraded for detection %s", detection_id)