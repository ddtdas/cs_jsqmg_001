"""APScheduler 集成（P2-2：修复"调度器 0 注册"）。

蓝图 §二/§4.1 规划 9 类 job：
  job_scan_dm 10min · job_zhihu_sync 5min · job_trap_recheck 6h ·
  job_account_scan 2h · job_evidence · job_case_publish 每日 ·
  job_collector_matrix 1min · job_persona_sim 1min（P11 拟真账号到期发布）·
  job_knowledge_decay 每日（P18 知识库时效衰减重算）。

设计原则：
  - create_scheduler()：每次创建全新 BackgroundScheduler 并注册 9 类 job（幂等可重建，
    便于 lifespan 反复启停与测试隔离）；
  - 任务函数全部防御式：try/except 包住，异常只记日志不向外抛；
  - 长耗时阻塞（知乎抓取/批量扫描）走 asyncio.to_thread / asyncio.run 线程隔离；
  - 无 LLM / 无真实知乎登录时任务安全 no-op（不崩、可重试）；
  - job_case_publish 受 P1-2 confirm+reason 门控约束：调度器无人确认，
    只生成"就绪"事件提示人工发布，绝不绕过审计门直接入库。
"""

from __future__ import annotations

import json
import threading

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from ..utils import get_logger

log = get_logger("canary.scheduler")

_scheduler: BackgroundScheduler | None = None
_scheduler_lock = threading.Lock()


# ---------------------------------------------------------------------------
# 防御式装饰器
# ---------------------------------------------------------------------------
def _safe(name: str):
    """任务包装：任何异常只记日志，绝不向上抛出（调度线程崩溃防护）。"""

    def deco(fn):
        def wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001 —— 任务必须容错
                log.error("job %s failed: %s", name, exc, exc_info=exc)
                return None

        wrapper.__name__ = name
        return wrapper

    return deco


# ---------------------------------------------------------------------------
# 9 类 job（防御式实现）
# ---------------------------------------------------------------------------
@_safe("job_scan_dm")
def job_scan_dm() -> None:
    """扫描待处理私信（pending detections）：规则+LLM（可用时）+ 分级，pending→scanned。"""
    from .. import db as dbmod
    from ..services.grading import GradingService
    from ..services.speech_engine import SpeechEngine

    conn = dbmod.get_conn()
    rows = conn.execute(
        "SELECT id FROM detections WHERE status='pending' ORDER BY id ASC LIMIT 50"
    ).fetchall()
    if not rows:
        return

    async def _run():
        engine = SpeechEngine()
        for r in rows:
            det_id = int(r["id"])
            await engine.analyze_detection(det_id, conn=conn)
            GradingService().finalize(conn, detection_id=det_id)

    import asyncio
    asyncio.run(_run())
    log.info("job_scan_dm 处理 %d 条 pending", len(rows))


@_safe("job_zhihu_sync")
def job_zhihu_sync() -> None:
    """知乎通道同步：拉取新私信 → 写 detections(pending)。无 cookie/通道不可用=安全 no-op。"""
    from ..config import get_settings
    from ..services.zhihu_bridge import ChannelUnavailableError, ZhihuBridge

    bridge = ZhihuBridge(settings=get_settings())

    async def _run() -> int:
        try:
            items = await bridge.fetch_inbox()
        except ChannelUnavailableError as exc:
            log.info("job_zhihu_sync 通道不可用（安全 no-op）: %s", exc.note[:120])
            return 0
        n = 0
        for it in items:
            try:
                bridge.ingest_manual(it.text, source="zhihu", from_url_name=it.from_url_name)
                n += 1
            except ValueError:
                continue
        return n

    import asyncio
    n = asyncio.run(_run())
    if n:
        log.info("job_zhihu_sync 导入 %d 条私信", n)


def _escalate_detection(conn, *, trap_id: int, det_id: int, recovered: bool = False) -> bool:
    """蜜饵实锤升级触发检测：finalize(trap_hit_count=1) → L5+告警+事件（单事务约定）。

    返回 True=本次完成升级；False=该检测已因 trap 命中升级过（幂等跳过）。
    异常向上抛，由调用方决定补偿策略（P1-A）。
    """
    from ..services.grading import GradingService

    if GradingService.is_trap_escalated(conn, det_id):
        return False
    GradingService().finalize(conn, detection_id=det_id, trap_hit_count=1)
    conn.execute(
        "INSERT INTO events (kind, payload) VALUES ('trap_hit_escalated', ?)",
        (json.dumps({"trap_id": trap_id, "detection_id": det_id, "grade": "L5",
                     "recovered": recovered}, ensure_ascii=False),),
    )
    conn.commit()
    return True


def _escalate_pending(conn) -> tuple[int, int]:
    """P1-A（R2 严格验收）补偿：重放 trap_hit_escalation_pending 未决升级。

    升级路径（check_hit 提交 → finalize 提交 → 事件提交）任一步失败会让
    "蜜饵已退役 + 检测未升级"且不可重试（蜜饵退役后不再复查）。补偿机制：
    失败时写入未决事件，本 job 每轮先重放——finalize 幂等（已升级跳过）。

    R3（P2-1）收敛：重放后对 pending 事件行打终态标记，避免 events 无界增长
    与每轮全量重扫——
      - 升级成功或检测已升级 → payload 追加 resolved=true + resolved_at；
      - 检测不存在（已被删除）→ payload 追加 terminal=true + reason（终态，
        停止重试，不再每轮 detection_not_found）；
      - 仅瞬时异常（数据库繁忙等）保持原状，留待下轮重放。
    返回 (成功恢复数, 终态数)。审计轨迹保留在 events（append-only 约定，UPDATE 终态标记）。
    """
    rows = conn.execute(
        "SELECT id, payload FROM events WHERE kind='trap_hit_escalation_pending' "
        "AND json_valid(payload)=1 "
        "AND json_extract(payload, '$.resolved') IS NULL "
        "AND json_extract(payload, '$.terminal') IS NULL "
        "ORDER BY id"
    ).fetchall()
    done, terminal = 0, 0
    for r in rows:
        event_id = int(r["id"])
        try:
            payload = json.loads(r["payload"] or "{}")
            det_id = int(payload.get("detection_id") or 0)
            trap_id = int(payload.get("trap_id") or 0)
            if det_id <= 0:
                continue
            from ..services.grading import GradingService

            det = conn.execute(
                "SELECT id FROM detections WHERE id=?", (det_id,)
            ).fetchone()
            if det is None:
                # 终态：检测已删除，无物可升级，标记后停止重试
                payload.update({"terminal": True, "reason": "detection_deleted",
                                "resolved_at": _now_utc()})
                conn.execute("UPDATE events SET payload=? WHERE id=?",
                             (json.dumps(payload, ensure_ascii=False), event_id))
                conn.commit()
                terminal += 1
                continue
            try:
                upgraded = _escalate_detection(conn, trap_id=trap_id, det_id=det_id, recovered=True)
            except Exception as exc:  # noqa: BLE001 —— 瞬时失败留待下轮重放
                log.warning("trap_hit_escalation_pending det=%d 补偿失败（留待下轮）: %s", det_id, exc)
                continue
            payload.update({"resolved": True, "recovered": upgraded,
                            "resolved_at": _now_utc()})
            conn.execute("UPDATE events SET payload=? WHERE id=?",
                         (json.dumps(payload, ensure_ascii=False), event_id))
            conn.commit()
            done += 1
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
    return done, terminal


def _now_utc() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


@_safe("job_trap_recheck")
def job_trap_recheck(limit: int = 10) -> None:
    """蜜饵复查：最近 scanned 检测文本对 active/monitored 蜜饵做踩饵匹配，命中即退役。

    R1 严格验收修复（P0-1/P1-2）：
      - P0-1：TrapEngine.check_hit 是 async 协程，原实现直接调用不 await，协程从未
        执行，job 整体静默空转（蜜饵自动踩饵检测失效）。现按 job_scan_dm 同款
        asyncio.run 模式真正执行踩饵检测；
      - P1-2：实锤命中后立即升级触发检测为 L5（GradingService.finalize 单事务写
        grade + grade_reason + 告警 + 事件），并追加 trap_hit_escalated 关联事件，
        使蜜饵实锤进入告警/证据/案例流水线（job_case_publish 的 L5 判定恢复意义）。

    R2 严格验收修复：
      - P1-A：升级路径补偿式原子化——finalize 失败不再静默丢失，写入
        trap_hit_escalation_pending 未决事件，每轮 job 先 _escalate_pending 重放
        （幂等、可恢复、可审计）；
      - P3-1：_escalate_detection 幂等判定（已因 trap 命中升级 L5 则跳过），
        杜绝同检测被多蜜饵命中时的重复升级事件/重复社工库分析。
    """
    from .. import db as dbmod
    from ..services.trap_engine import TrapEngine

    conn = dbmod.get_conn()
    # P1-A 补偿：先重放未决升级（无论本轮是否有待复查蜜饵/候选）；R3 P2-1 终态收敛
    replayed, terminal = _escalate_pending(conn)
    traps = conn.execute(
        "SELECT id FROM honey_facts WHERE status IN ('active','monitored') AND enabled=1 "
        "ORDER BY id DESC LIMIT 20"
    ).fetchall()
    candidates = [
        (int(r["id"]), r["content"]) for r in conn.execute(
            "SELECT id, content FROM detections WHERE status='scanned' AND content IS NOT NULL "
            "ORDER BY id DESC LIMIT ?", (limit,),
        ).fetchall() if r["content"]
    ]
    if not traps or not candidates:
        if replayed or terminal:
            log.info("job_trap_recheck 补偿升级 %d 条未决，%d 条终态收敛",
                     replayed, terminal)
        return
    engine = TrapEngine()

    async def _run() -> None:
        for t in traps:
            tid = int(t["id"])
            for det_id, text in candidates:
                try:
                    res = await engine.check_hit(conn, tid, text)
                except Exception:
                    break  # 已退役/停用/非 active·monitored → 换下一个蜜饵
                if res["hit"]:
                    try:
                        if _escalate_detection(conn, trap_id=tid, det_id=det_id):
                            log.info("job_trap_recheck 蜜饵 #%d 命中 → 检测 #%d 升级 L5", tid, det_id)
                        else:
                            log.info("job_trap_recheck 蜜饵 #%d 命中 → 检测 #%d 已 L5（跳过重复升级）", tid, det_id)
                    except Exception as exc:  # noqa: BLE001
                        # P1-A：升级失败不静默——记未决事件，下轮 job 补偿重放
                        try:
                            conn.execute(
                                "INSERT INTO events (kind, payload) VALUES ('trap_hit_escalation_pending', ?)",
                                (json.dumps({"trap_id": tid, "detection_id": det_id,
                                             "grade": "L5"}, ensure_ascii=False),),
                            )
                            conn.commit()
                        except Exception:
                            conn.rollback()
                        log.error(
                            "job_trap_recheck det=%d 升级 L5 失败（已记未决，下轮补偿）: %s",
                            det_id, exc,
                        )
                    break  # 命中即退役 → 换下一个蜜饵

    import asyncio
    asyncio.run(_run())
    log.info("job_trap_recheck 复查 %d 个蜜饵（补偿 %d 条未决，%d 条终态收敛）",
             len(traps), replayed, terminal)


@_safe("job_account_scan")
def job_account_scan(limit: int = 10) -> None:
    """账号画像刷新：对已建档账号拉取公开信号（CH-C 优先；任何失败 no-op 跳过）。"""
    from .. import db as dbmod
    from ..config import get_settings
    from ..services.account_intel import AccountIntelService
    from ..services.zhihu_bridge import ZhihuBridge

    conn = dbmod.get_conn()
    rows = conn.execute(
        "SELECT url_name FROM accounts ORDER BY updated_at DESC LIMIT ?", (limit,)
    ).fetchall()
    if not rows:
        return

    bridge = ZhihuBridge(settings=get_settings())
    svc = AccountIntelService()

    async def _run():
        for r in rows:
            try:
                sig = await bridge.fetch_profile(r["url_name"])
            except Exception:
                continue  # 通道失败 no-op，下一轮重试
            signals = {
                "activity_level": sig.activity_level,
                "content_consistency": sig.content_consistency,
            }
            if sig.registered_at:
                signals["registered_at"] = sig.registered_at
            svc.check(conn, r["url_name"], signals)

    import asyncio
    asyncio.run(_run())
    log.info("job_account_scan 刷新 %d 个账号", len(rows))


@_safe("job_evidence")
def job_evidence(limit: int = 10) -> None:
    """证据包自动组装：为尚无证据包的 L4+ 检测构建证据包（det_ids 去重幂等）。"""
    from .. import db as dbmod
    from ..services.evidence import EvidenceService

    conn = dbmod.get_conn()
    rows = conn.execute(
        "SELECT id FROM detections WHERE grade IN ('L4','L5') ORDER BY id ASC LIMIT ?", (limit,)
    ).fetchall()
    svc = EvidenceService()
    built = 0
    for r in rows:
        det_id = int(r["id"])
        # P1-3（R1 严格验收）：去重由 LIKE 子串匹配改为 json_each 精确判定——
        # 原 `det_ids LIKE '%1%'` 会把 [11]/[51] 误判为 det 1 已有包而漏建证据；
        # 现在仅当 det_id 精确出现在某包的 JSON 数组中才算已建包。
        # P2-1（R2）：json_valid 门控（CASE 短路）——历史脏数据（det_ids 非合法
        # JSON）行不再让 json_each 抛 malformed JSON 导致整个 job 中止，直接跳过。
        dup = conn.execute(
            "SELECT 1 FROM evidence_packages WHERE CASE WHEN json_valid(det_ids) THEN "
            "EXISTS (SELECT 1 FROM json_each(det_ids) WHERE CAST(value AS TEXT) = ?) "
            "ELSE 0 END LIMIT 1",
            (str(det_id),),
        ).fetchone()
        if dup:
            continue
        try:
            svc.build(conn, [det_id])
            built += 1
        except Exception as exc:
            log.warning("job_evidence det=%d 构建失败: %s", det_id, exc)
    if built:
        log.info("job_evidence 构建 %d 个证据包", built)


@_safe("job_case_publish")
def job_case_publish(limit: int = 5) -> None:
    """每日案例就绪检查（P1-2 门控：调度器无人 confirm，只产出就绪事件提示人工发布）。"""
    from .. import db as dbmod
    from ..services.desensitize import DesensitizeService

    conn = dbmod.get_conn()
    rows = conn.execute(
        """SELECT d.id, d.content FROM detections d
           WHERE d.grade='L5' AND d.content IS NOT NULL
             AND NOT EXISTS (SELECT 1 FROM cases c WHERE c.det_id = d.id)
           ORDER BY d.id ASC LIMIT ?""", (limit,),
    ).fetchall()
    if not rows:
        return
    prepped = 0
    for r in rows:
        redacted = DesensitizeService().regex_redact(r["content"] or "")["redacted"]
        conn.execute(
            "INSERT INTO events (kind, payload) VALUES ('case_ready', ?)",
            (json.dumps({"det_id": r["id"], "redacted_payload": redacted[:500]},
                        ensure_ascii=False),),
        )
        prepped += 1
    conn.commit()
    log.info("job_case_publish 就绪 %d 条案例草稿（需人工 confirm 发布）", prepped)


# ---------------------------------------------------------------------------
# 调度器创建 / 快照
# ---------------------------------------------------------------------------
@_safe("job_collector_matrix")
def job_collector_matrix() -> None:
    """采集矩阵轮询（P10）：按 frequency 节流执行 enabled 单元格（防御式，异常不崩）。"""
    from .. import db as dbmod
    from ..services.collector import CollectorService

    conn = dbmod.get_conn()
    svc = CollectorService(conn=conn)
    cells = conn.execute(
        "SELECT * FROM collector_matrix WHERE enabled=1 ORDER BY id ASC"
    ).fetchall()
    for c in cells:
        cell = dict(c)
        try:
            strat = cell.get("strategy") or "{}"
            import json as _json
            try:
                strat = _json.loads(strat) if isinstance(strat, str) else strat
            except Exception:
                strat = {}
            freq = strat.get("frequency", "hourly")
            last = cell.get("last_run_at") or ""
            from datetime import datetime, timedelta
            now = datetime.now()
            try:
                last_dt = datetime.strptime(last, "%Y-%m-%d %H:%M:%S") if last else None
            except Exception:
                last_dt = None
            if freq == "minute" and (last_dt is None or (now - last_dt) >= timedelta(seconds=60)):
                svc.run_cell(int(cell["id"]))
            elif freq == "hourly" and (last_dt is None or (now - last_dt) >= timedelta(hours=1)):
                svc.run_cell(int(cell["id"]))
            elif freq == "daily" and (last_dt is None or (now - last_dt) >= timedelta(days=1)):
                svc.run_cell(int(cell["id"]))
        except Exception as exc:  # noqa: BLE001
            log.warning("job_collector_matrix cell=%s failed: %s", cell.get("id"), exc)
    log.info("job_collector_matrix 轮询 %d 个启用单元格", len(cells))


@_safe("job_persona_sim")
def job_persona_sim() -> None:
    """拟真账号到期发布（P11）：轮询 persona_schedules 到期计划逐条发布（persona_posts）。"""
    from .. import db as dbmod
    from ..services.persona_sim import PersonaSimService

    conn = dbmod.get_conn()
    n = PersonaSimService().publish_due(conn)
    if n:
        log.info("job_persona_sim 发布 %d 条拟真动态", n)


@_safe("job_knowledge_decay")
def job_knowledge_decay() -> None:
    """知识库时效衰减每日重算（P18）：decay_weight + weighted_score 落库（查询快）。"""
    from .. import db as dbmod
    from ..services.knowledge import KnowledgeDistiller

    conn = dbmod.get_conn()
    n = KnowledgeDistiller().refresh_decay(conn)
    if n:
        log.info("job_knowledge_decay 重算 %d 条知识条目", n)


def _register_jobs(sched: BackgroundScheduler) -> None:
    """注册蓝图 §二 的 9 类 job（唯一主动者；max_instances=1 + coalesce 防重入）。"""
    sched.add_job(job_scan_dm, IntervalTrigger(minutes=10), id="job_scan_dm",
                  max_instances=1, coalesce=True, replace_existing=True)
    sched.add_job(job_zhihu_sync, IntervalTrigger(minutes=5), id="job_zhihu_sync",
                  max_instances=1, coalesce=True, replace_existing=True)
    sched.add_job(job_trap_recheck, IntervalTrigger(hours=6), id="job_trap_recheck",
                  max_instances=1, coalesce=True, replace_existing=True)
    sched.add_job(job_account_scan, IntervalTrigger(hours=2), id="job_account_scan",
                  max_instances=1, coalesce=True, replace_existing=True)
    sched.add_job(job_evidence, IntervalTrigger(hours=12), id="job_evidence",
                  max_instances=1, coalesce=True, replace_existing=True)
    sched.add_job(job_case_publish, CronTrigger(hour=3, minute=0), id="job_case_publish",
                  max_instances=1, coalesce=True, replace_existing=True)
    sched.add_job(job_collector_matrix, IntervalTrigger(minutes=1), id="job_collector_matrix",
                  max_instances=1, coalesce=True, replace_existing=True)
    sched.add_job(job_persona_sim, IntervalTrigger(minutes=1), id="job_persona_sim",
                  max_instances=1, coalesce=True, replace_existing=True)
    sched.add_job(job_knowledge_decay, CronTrigger(hour=3, minute=30), id="job_knowledge_decay",
                  max_instances=1, coalesce=True, replace_existing=True)


def create_scheduler() -> BackgroundScheduler:
    """创建全新调度器并注册 9 类 job（幂等可重建，P2-2）。"""
    global _scheduler
    with _scheduler_lock:
        sched = BackgroundScheduler()
        _register_jobs(sched)
        _scheduler = sched
        return sched


def get_scheduler() -> BackgroundScheduler | None:
    """返回当前调度器单例。

    测试专用（R5 收尾标注）：生产无运维面板消费，仅供 tests/test_p10_collector.py
    断言调度器已启动。
    """
    return _scheduler


def job_snapshot() -> list[dict]:
    """调度快照：job id / 触发器 / 下次运行（供运维面板与测试断言）。

    测试专用（R5 收尾标注）：生产无消费方（运维面板未接入 /system/stats），
    仅供 tests/test_p2_engineering.py 断言 9 类 job 注册。
    """
    from datetime import datetime

    sched = _scheduler
    if sched is None:
        return []
    out = []
    for job in sched.get_jobs():
        nxt = None
        try:
            nxt = job.trigger.get_next_fire_time(None, datetime.now())
        except Exception:
            nxt = None
        out.append({
            "id": job.id,
            "trigger": str(job.trigger),
            "next_run": str(nxt) if nxt else None,
        })
    return out