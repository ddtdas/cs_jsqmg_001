"""系统端点：health / stats。

对应主方案 §4.2 system.py 路由表与 MCP af_health / af_stats 透传目标。
health 为公开端点（运维探活）；stats 为受保护端点（require_admin，D6）。
P2-1：stats 返回真实计数（蜜饵/检测/告警/案例 + 分级分布）。
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends

from ..config import Settings, get_settings
from ..db import get_db
from ..deps import require_admin
from ..utils import ApiError, ok

router = APIRouter(prefix="/system", tags=["system"])


@router.get("/health")
def health(
    db=Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict:
    """健康检查：主控/DB/LLM/知乎通道四灯（对齐 MCP af_health）。

    - db: 布尔（SELECT 1 实测）
    - llm: configured | not_configured（D5：未配置不崩溃，仅降级提示）
    - zhihu: idle（P2 落地通道健康探活前恒为 idle）
    """
    db_ok = True
    try:
        db.execute("SELECT 1;")
    except Exception:
        db_ok = False

    return ok({
        "status": "ok",
        "version": settings.version,
        "db": db_ok,
        "llm": "configured" if settings.llm_configured else "not_configured",
        "zhihu": "idle",
    })


@router.get("/stats", dependencies=[Depends(require_admin)])
def stats(db=Depends(get_db)) -> dict:
    """运行统计（真实计数，P2-1）：蜜饵/检测/分级分布/告警/案例/账号。"""
    def _n(sql: str) -> int:
        row = db.execute(sql).fetchone()
        return int(row[0]) if row else 0

    grades = {}
    for row in db.execute(
        "SELECT COALESCE(grade, 'L1') AS grade, COUNT(*) AS n FROM detections GROUP BY grade"
    ):
        grades[row["grade"]] = int(row["n"])

    return ok({
        "traps": _n("SELECT COUNT(*) FROM honey_facts"),
        "active_traps": _n("SELECT COUNT(*) FROM honey_facts WHERE status IN ('active','monitored')"),
        "detections": _n("SELECT COUNT(*) FROM detections"),
        "grades": grades,
        "alerts": _n("SELECT COUNT(*) FROM alerts"),
        "unread_alerts": _n("SELECT COUNT(*) FROM alerts WHERE unread=1"),
        "cases": _n("SELECT COUNT(*) FROM cases"),
        "accounts": _n("SELECT COUNT(*) FROM accounts"),
        "events": _n("SELECT COUNT(*) FROM events"),
        # P19：反钓鱼伪装看板联动（伪装身份数 / 接触事件数）
        "cover_identities": _n("SELECT COUNT(*) FROM cover_identities"),
        "contact_events": _n("SELECT COUNT(*) FROM contact_events"),
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
    })


@router.get("/audit-verify", dependencies=[Depends(require_admin)])
def audit_verify(db=Depends(get_db)) -> dict:
    """GateLog 哈希链完整性校验（P1-C6）：全链重算，断裂返回 409 audit_chain_broken。

    返回 {valid, checked}；篡改任一行（before/after/reason/action/ts/payload_json/prev_hash）
    即后续行全部断裂并被检出。
    """
    from ..services.audit_log import audit_verify_endpoint

    return audit_verify_endpoint(db)


# ---------------------------------------------------------------------------
# 内嵌自包含 DSH 实例（端口 3092，项目 dsh/ 目录）——状态 + 一键拉起/守护
# ---------------------------------------------------------------------------

def _dsh_port(dsh_dir) -> int:
    """解析内嵌 DSH 端口（与 dsh 启动脚本同口径）：CANARY_DSH_PORT → cordis.patch.yml → 3092。"""
    import os
    import re

    env_port = os.environ.get("CANARY_DSH_PORT", "").strip()
    if env_port.isdigit():
        return int(env_port)
    patch = dsh_dir / "cordis.patch.yml"
    try:
        if patch.exists():
            m = re.search(
                r"(?m)^\s*port\s*:\s*(\d+)\s*$",
                patch.read_text(encoding="utf-8", errors="replace"),
            )
            if m:
                return int(m.group(1))
    except Exception:
        pass
    return 3092


def _dsh_running(port: int) -> bool:
    """探测端口是否监听（内嵌 DSH 是否在跑）。"""
    import socket

    try:
        s = socket.socket()
        s.settimeout(1.5)
        s.connect(("127.0.0.1", port))
        s.close()
        return True
    except Exception:
        return False


def _dsh_token_url(out_log) -> str:
    """从 dsh/dsh-web.out.log 解析 0.1.5 带 token 的完整 URL。"""
    import re

    if not out_log.exists():
        return ""
    try:
        content = out_log.read_text(encoding="utf-8", errors="replace")
        m = re.search(r"http://127\.0\.0\.1:\d+/\?token=[A-Za-z0-9_-]+", content)
        return m.group(0) if m else ""
    except Exception:
        return ""


@router.get("/embedded-dsh", dependencies=[Depends(require_admin)])
def embedded_dsh() -> dict:
    """内嵌自包含 DSH 实例状态（P10）：读取 <proj>/dsh/dsh-web.out.log 解析 0.1.5 token URL。

     返回 { port, running, url, token_url }：
      - port: 3092（默认；CANARY_DSH_PORT 环境变量可覆盖，其次取 dsh/cordis.patch.yml 的 port）
      - running: 按上述 port 探测端口是否监听
      - url: http://127.0.0.1:{port}/（无 token 裸入口，浏览器打开会 401）
      - token_url: dsh/dsh-web.out.log 中的带 token 完整 URL（ConsoleView iframe 应使用）
    供前端「命令输入」页自动指向项目自包含实例，而非设备全局 3080。
    """
    from pathlib import Path

    proj_root = Path(__file__).resolve().parents[2]
    dsh_dir = proj_root / "dsh"
    out_log = dsh_dir / "dsh-web.out.log"
    port = _dsh_port(dsh_dir)
    return ok({
        "port": port,
        "running": _dsh_running(port),
        "url": f"http://127.0.0.1:{port}/",
        "token_url": _dsh_token_url(out_log),
        "log_path": str(out_log),
    })


@router.post("/embedded-dsh/start", dependencies=[Depends(require_admin)])
def embedded_dsh_start() -> dict:
    """一键拉起 + 守护（懒启动，无需心跳）：未运行则后台拉起内嵌 DSH，立即返回。

    前端「命令输入」页在访问/点击「一键拉起」时调用；若已运行则直接返回 token_url。
    拉起用 dsh\\start-embedded.ps1（内部 WMI 分离进程，父进程退出后仍常驻），
    本端点以后台线程同步执行启动脚本后立即返回（脚本内部用 WMI 分离拉起 node，
    父脚本退出后 node 仍常驻），前端轮询 GET /system/embedded-dsh 取就绪态。
    """
    import subprocess
    import threading
    from pathlib import Path

    proj_root = Path(__file__).resolve().parents[2]
    dsh_dir = proj_root / "dsh"
    out_log = dsh_dir / "dsh-web.out.log"
    port = _dsh_port(dsh_dir)

    if _dsh_running(port):
        return ok({
            "port": port,
            "running": True,
            "started": False,
            "token_url": _dsh_token_url(out_log),
        })

    ps1 = dsh_dir / "start-embedded.ps1"
    if not ps1.exists():
        raise ApiError("dsh_script_missing", f"内嵌 DSH 启动脚本缺失: {ps1}", 500)

    # 后台线程同步执行 start-embedded.ps1（其内部用 WMI 分离拉起 node，脚本退出后 node 常驻）。
    # 不阻塞事件循环；不传 close_fds（Windows 上 close_fds=True 与 stdio 重定向冲突）。
    def _run():
        try:
            subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ps1), "-NoBrowser"],
                cwd=str(dsh_dir),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=120,
            )
        except Exception:
            pass

    threading.Thread(target=_run, daemon=True).start()

    return ok({
        "port": port,
        "running": False,
        "started": True,
        "token_url": "",
        "message": "已在后台拉起内嵌 DSH，请稍后刷新获取就绪状态",
    })
