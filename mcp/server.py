"""金丝雀蜜罐 · MCP Server —— 纯透传代理（D1：无业务逻辑）。

形态关系（主方案 §7.1）：dsh / Claude Code / Codex 等 MCP 客户端
  -> 本文件（MCP 透传层）
  -> FastAPI 主控 REST API（/api/v1/*，唯一业务内核，WebUI/MCP/curl 同为客户端）

铁律（主方案 §11 D1 / D6）：
  1. 每个工具 = 一个 REST 调用封装（httpx），只做参数搬运与统一解包，禁止复制业务逻辑。
  2. 统一解包协议与主控 app/utils.py 严格一致：{ok:true,data} | {ok:false,error:{code,message}}；
     错误经 ProxyError 原样透传显示（code/message 不加工不吞并）。
     R1-A8：ProxyError 继承 SDK ToolError，客户端 isError 结果可见
     `Error executing tool <name>: [code] message`（错误码不再被 SDK 吞成通用文本）。
  3. 敏感操作（af_retire_trap / af_publish_case / af_config_update / af_freeze_evidence）透传 confirm+reason 闸控参数，
     由主控（GateLog 哈希链）裁决 —— MCP 层不自行放行或拦截。

环境变量：
  AF_MASTER_URL  主控地址（默认 http://127.0.0.1:9200）
  AF_API_KEY     主控 admin key（透传用，X-API-Key 头；空则匿名调用只读公开端点）

传输：
  默认 stdio；`--http --port 9201`（可配）启用 streamable-http（路径 /mcp）。

⚠️ 命名空间警示：本目录 mcp/ 是命名空间段（无 __init__.py），
  严禁创建 mcp/__init__.py —— 否则会遮蔽 site-packages 的官方 mcp SDK 包，
  导致 `from mcp.server.mcpserver import MCPServer` 失败（见 docs/MCP集成说明.md）。
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any

import httpx

# 官方 mcp SDK 2.x：FastMCP 已更名为 MCPServer（mcp.server.mcpserver）。
# 勿用旧 API `from mcp.server.fastmcp import FastMCP`（2.x 已删除该模块）。
from mcp.server.mcpserver import MCPServer
# ToolError：SDK 的"预期失败"类型。ProxyError 继承它（而非裸 Exception），
# 才能让 SDK 把 code/message 原文送入客户端 isError 结果（R1-A8 修复，见下方类注释）。
from mcp.server.mcpserver.exceptions import ToolError

DEFAULT_MASTER_URL = "http://127.0.0.1:9200"
SERVER_VERSION = "0.1.0"
SERVER_NAME = "af-honeypot"


# ---------------------------------------------------------------------------
# 统一解包协议（与主控 app/utils.py 严格一致，D1）
# ---------------------------------------------------------------------------
class ProxyError(ToolError):
    """主控错误透传：携带 code/message（来源 {ok:false,error:{code,message}}）。

    必须继承 SDK 的 ToolError 而非裸 Exception（R1-A8 修复）：
    - SDK 2.x 对 ToolError 视为"预期失败"：调用返回 isError=true，且 message 原样
      进入 content 供 agent 阅读，服务端仅 INFO 日志、无堆栈（E3 要求）。
    - 若为普通 Exception 则被 SDK 包装成 UnexpectedToolError，客户端只见
      'Error executing tool <name>'，code/message 全部被吞（A8 实测缺陷根因）。
    因此本类把错误码拼进消息文本（'[code] message'），借 ToolError 通道原样透传。
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message


def unwrap(payload: Any) -> Any:
    """统一解包：{ok:true,data} -> data；{ok:false,error} -> 抛 ProxyError。

    这是 MCP 层唯一的封包理解点 —— 只解包不加工，业务判断全部留给主控。
    """
    if not isinstance(payload, dict):
        raise ProxyError("bad_response", f"主控响应缺少统一封包（{type(payload).__name__}）")
    if payload.get("ok") is True:
        return payload.get("data")
    err = payload.get("error")
    if isinstance(err, dict):
        code = err.get("code") or "error"
        message = err.get("message") or "未知错误"
    else:
        code, message = "error", "未知错误"
    raise ProxyError(str(code), str(message))


# ---------------------------------------------------------------------------
# 主控客户端
# ---------------------------------------------------------------------------
class MasterProxy:
    """主控 REST 客户端：httpx 封装，惰性单例连接，支持注入 transport（测试用 mock）。"""

    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._transport = transport
        self._timeout = timeout
        self._client: httpx.AsyncClient | None = None

    @property
    def base_url(self) -> str:
        return self._base_url

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            headers = {"X-API-Key": self._api_key} if self._api_key else {}
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                headers=headers,
                timeout=self._timeout,
                transport=self._transport,
            )
        return self._client

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: Any = None,
    ) -> Any:
        """执行 REST 调用并统一解包（透传：params->query，body->json）。

        P1-D 修复：值为 None 的参数（如 af_grade_explain 的 account_risk、
        af_list_alerts 的 level）若原样透传，httpx 会把 query 编码成 'account_risk='
        空串，主控端 pattern 校验（如 ^(green|yellow|red)$）对空串必 422；
        这里透传前把 params/body 顶层键中值为 None 的剔除，不落入请求。
        （仅剔除空值键，不改写任何业务数据，符合 D1 纯透传。）
        """
        client = self._get_client()
        try:
            if params:
                params = {k: v for k, v in params.items() if v is not None}
            if isinstance(body, dict):
                body = {k: v for k, v in body.items() if v is not None}
            resp = await client.request(method, path, params=params, json=body)
        except httpx.HTTPError as exc:
            raise ProxyError("master_unreachable", f"主控不可达（{self._base_url}）: {exc}") from exc
        try:
            payload = resp.json()
        except ValueError:
            raise ProxyError(
                "bad_response", f"主控返回非 JSON 响应（HTTP {resp.status_code}）"
            ) from None
        return unwrap(payload)

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


# ---------------------------------------------------------------------------
# 工具注册（55 个，对齐主方案 §7.2 工具清单 + 主控实际端点，一一对应）
# 端点：app/api/*.py（/api/v1 前缀）。v1.0.0（P6.5 收尾）后 /events、/config、
# /scan/inbox 已全部落地，无规划端点；若连到旧版主控，404 会经 ProxyError 原样透传。
# P2-D 增补：/collector 采集矩阵三个透传工具（af_collector_matrix / _add_cell / _run）。
# ---------------------------------------------------------------------------
def build_mcp_server(
    proxy: MasterProxy | None = None,
    *,
    master_url: str | None = None,
    api_key: str | None = None,
) -> MCPServer:
    """构建 MCP Server（可注入 proxy 供测试：httpx.MockTransport 模拟主控）。"""
    proxy = proxy or MasterProxy(
        master_url if master_url is not None else os.environ.get("AF_MASTER_URL", DEFAULT_MASTER_URL),
        api_key if api_key is not None else os.environ.get("AF_API_KEY", ""),
    )

    async def call(method: str, path: str, *, params: dict[str, Any] | None = None, body: Any = None) -> Any:
        data = await proxy.request(method, path, params=params, body=body)
        # MCP 内容转换归一化：SDK 会把返回的 list/tuple 按内容块展开（空列表 → 空 content），
        # 非 dict 值也需包一层以保证 JSON 文本化。这里只做展示适配，不改写业务数据。
        if isinstance(data, dict):
            return data
        if isinstance(data, list):
            return {"items": data}
        return {"value": data}

    server = MCPServer(
        name=SERVER_NAME,
        version=SERVER_VERSION,
        instructions=(
            "金丝雀蜜罐 CanaryGuard AntiFraud 的 MCP 透传代理：蜜饵/话术检测/账号速查/"
            "五级分级/证据包/案例库/事件告警/知乎通道/运维。所有工具为 FastAPI 主控 "
            "REST API（/api/v1）的透传封装，无本地业务逻辑；敏感操作需 confirm+reason 由主控闸控。"
        ),
    )

    # ============================ 蜜饵（R1，/traps）============================
    @server.tool()
    async def af_list_traps(status: str | None = None, limit: int = 100) -> Any:
        """列出蜜饵（状态/命中数/伪装度）。status 可按 draft|active|monitored|hit|retired 过滤。"""
        return await call("GET", "/api/v1/traps", params={"status": status, "limit": limit})

    @server.tool()
    async def af_get_trap(trap_id: int) -> Any:
        """查询单个蜜饵详情。"""
        return await call("GET", f"/api/v1/traps/{trap_id}")

    @server.tool()
    async def af_generate_trap_draft(
        template_id: str | None = None, platform: str = "评论区", note: str | None = None
    ) -> Any:
        """生成蜜饵草稿（HITL：只产文案+指纹，不发布）。platform 为埋设位置提示。"""
        return await call(
            "POST", "/api/v1/traps/generate-draft",
            body={"template_id": template_id, "platform": platform, "note": note},
        )

    @server.tool()
    async def af_mark_trap_deployed(trap_id: int, target_url: str | None = None) -> Any:
        """用户手动发布蜜饵后标记 deployed（draft→active）。target_url 为发布位置（可空）。"""
        return await call(
            "POST", f"/api/v1/traps/{trap_id}/deploy", body={"target_url": target_url}
        )

    @server.tool()
    async def af_disable_trap(trap_id: int) -> Any:
        """停用蜜饵（enabled=0，仅 active/monitored 状态）。"""
        return await call("POST", f"/api/v1/traps/{trap_id}/disable")

    @server.tool()
    async def af_retire_trap(trap_id: int, confirm: bool = False, reason: str = "") -> Any:
        """退役蜜饵（敏感操作：主控强制 confirm=true + reason≥20 字，GateLog 审计；缺省 confirm=false 会被 403 拒绝）。"""
        return await call(
            "POST", f"/api/v1/traps/{trap_id}/retire",
            body={"confirm": confirm, "reason": reason},
        )

    @server.tool()
    async def af_check_trap_hit(trap_id: int, text: str) -> Any:
        """输入候选文本检测踩饵；命中即退役（hit→retired，实锤 D2）。P2-4：改 POST body 透传。"""
        return await call("POST", f"/api/v1/traps/{trap_id}/check-hit", body={"text": text})

    # ============================ 检测（R2/R4，/scan /accounts /grading）============================
    @server.tool()
    async def af_scan_text(text: str, source: str = "manual") -> Any:
        """单条文本话术判定（规则+LLM 双确认 + 五级分级 + L3+ 告警闸控）。source: zhihu|import|manual。"""
        return await call("POST", "/api/v1/scan/text", body={"text": text, "source": source})

    @server.tool()
    async def af_scan_inbox() -> Any:
        """触发 inbox 批量检测：消费 CH-D 导入的 pending detections（status pending→scanned，
        逐条规则+LLM + 五级分级）。结果查看可用 af_list_scan_hits（最近命中）或主控 GET /scan/inbox。"""
        return await call("POST", "/api/v1/scan/inbox")

    @server.tool()
    async def af_list_scan_hits(limit: int = 20) -> Any:
        """最近话术命中列表（detections 倒序 + 命中条数）。"""
        return await call("GET", "/api/v1/scan/hits", params={"limit": limit})

    @server.tool()
    async def af_detection_full(detection_id: int) -> Any:
        """检测详情聚合（四要素）：告警原因（分级证据链）+ 上下文（原文+命中话术+关联账号/蜜饵）+
        平台跳转 URL + 详细分析（话术明细+案例+事件时间线）。供 WebUI 详情抽屉与 agent 详查。"""
        return await call("GET", f"/api/v1/detections/{detection_id}/full")

    @server.tool()
    async def af_detection_platform_link(detection_id: int) -> Any:
        """检测对应平台跳转 URL（优先 target_url 问题点，否则平台首页兜底）。"""
        return await call("GET", f"/api/v1/detections/{detection_id}/platform-link")

    @server.tool()
    async def af_account_check(url_name: str, signals: dict | None = None) -> Any:
        """账号风险速查：绿黄红 + 证据链 + 共现账号。signals 为公开信号（白名单过滤）。"""
        return await call(
            "POST", "/api/v1/accounts/check", body={"url_name": url_name, "signals": signals}
        )

    @server.tool()
    async def af_account_get(url_name: str) -> Any:
        """查询已建档账号画像。"""
        return await call("GET", f"/api/v1/accounts/{url_name}")

    @server.tool()
    async def af_grade_explain(
        detection_id: int, trap_hit_count: int = 0, account_risk: str | None = None
    ) -> Any:
        """某次检测的五级分级与可解释证据链。account_risk: green|yellow|red（可选上下文）。"""
        return await call(
            "GET", f"/api/v1/grading/explain/{detection_id}",
            params={"trap_hit_count": trap_hit_count, "account_risk": account_risk},
        )

    @server.tool()
    async def af_grading_levels() -> Any:
        """五级分级档位定义（L1–L5）。"""
        return await call("GET", "/api/v1/grading/levels")

    # ============================ 情报（R5/R6，/evidence /cases）============================
    @server.tool()
    async def af_build_evidence(det_ids: list[int], screenshots: list[str] | None = None) -> Any:
        """组装证据包（文本/命中详情/时间戳/来源 + 哈希链）。det_ids 为关联检测记录 id 列表。"""
        return await call(
            "POST", "/api/v1/evidence/build",
            body={"det_ids": det_ids, "screenshots": screenshots},
        )

    @server.tool()
    async def af_get_evidence(pkg_id: int) -> Any:
        """证据包详情 + 完整性校验。"""
        return await call("GET", f"/api/v1/evidence/{pkg_id}")

    @server.tool()
    async def af_freeze_evidence(pkg_id: int, confirm: bool = False, reason: str = "") -> Any:
        """冻结证据包（哈希不可变）。敏感操作（P1-A）：主控强制 confirm=true + reason≥20 字，
        GateLog 审计；缺省 confirm=false 会被 403 拒绝。"""
        return await call(
            "POST", f"/api/v1/evidence/{pkg_id}/freeze",
            body={"confirm": confirm, "reason": reason},
        )

    @server.tool()
    async def af_export_evidence(pkg_id: int, fmt: str = "json") -> Any:
        """导出证据包（json 结构化 / text 可读文本）。"""
        return await call(
            "GET", f"/api/v1/evidence/{pkg_id}/export", params={"fmt": fmt}
        )

    @server.tool()
    async def af_report_template(pkg_id: int, kind: str = "96110") -> Any:
        """举报/辟谣模板（96110|platform|pibyao；仅个人署名 + 处置引导，不处置不冒充官方）。"""
        return await call(
            "GET", f"/api/v1/evidence/{pkg_id}/report-template", params={"kind": kind}
        )

    @server.tool()
    async def af_list_cases(page: int = 1, page_size: int = 20, tag: str | None = None) -> Any:
        """案例库公开查询（分页/按骗术标签过滤；只暴露脱敏载荷）。"""
        return await call(
            "GET", "/api/v1/cases", params={"page": page, "page_size": page_size, "tag": tag}
        )

    @server.tool()
    async def af_publish_case(
        det_id: int,
        redacted_payload: str,
        desensitize_log: list[dict] | None = None,
        graph_tags: list[str] | None = None,
        confirm: bool = False,
        reason: str = "",
    ) -> Any:
        """发布案例（敏感操作：脱敏强制校验前置 + 主控强制 confirm=true + reason≥20 字，GateLog 审计）。"""
        return await call(
            "POST", "/api/v1/cases/publish",
            body={
                "det_id": det_id,
                "redacted_payload": redacted_payload,
                "desensitize_log": desensitize_log,
                "graph_tags": graph_tags,
                "confirm": confirm,
                "reason": reason,
            },
        )

    @server.tool()
    async def af_case_graph() -> Any:
        """骗术图谱聚合数据（供 ECharts 渲染）。"""
        return await call("GET", "/api/v1/cases/graph")

    # ============================ 事件与告警（/events /alerts）============================
    @server.tool()
    async def af_list_events(kind: str | None = None, page: int = 1, page_size: int = 20) -> Any:
        """事件流（命中/退役/入库/配置变更等），分页 + 按 kind 过滤。page 从 1 起，page_size 1–100。"""
        return await call(
            "GET", "/api/v1/events", params={"kind": kind, "page": page, "page_size": page_size}
        )

    @server.tool()
    async def af_get_event(event_id: int) -> Any:
        """单条事件详情。"""
        return await call("GET", f"/api/v1/events/{event_id}")

    @server.tool()
    async def af_list_alerts(level: str | None = None, unread_only: bool = False, limit: int = 50) -> Any:
        """告警列表（L3+ 推送记录；level: L3|L4|L5，可按未读过滤）。"""
        return await call(
            "GET", "/api/v1/alerts",
            params={"level": level, "unread_only": unread_only, "limit": limit},
        )

    @server.tool()
    async def af_mark_alert_read(alert_id: int) -> Any:
        """标记告警已读。"""
        return await call("POST", f"/api/v1/alerts/{alert_id}/read")

    # ============================ 采集矩阵（/collector）============================
    @server.tool()
    async def af_collector_matrix() -> Any:
        """采集矩阵：平台×账号策略格子列表（平台/账号/采集策略/启停状态）。"""
        return await call("GET", "/api/v1/collector/matrix")

    @server.tool()
    async def af_collector_add_cell(
        platform: str,
        account: str = "",
        collect: str = "dm",
        frequency: str = "hourly",
        depth: int = 5,
        keyword: str = "",
        near_dup: float = 0.85,
    ) -> Any:
        """新增采集矩阵格子。platform 为目标平台；collect: dm|comments|posts|follows；
        frequency: minute|hourly|daily；depth 采集深度 1–100；keyword 采集关键词。"""
        return await call(
            "POST", "/api/v1/collector/matrix",
            body={
                "platform": platform,
                "account": account,
                "strategy": {
                    "collect": collect,
                    "frequency": frequency,
                    "depth": depth,
                    "keyword": keyword,
                    "near_dup": near_dup,
                },
            },
        )

    @server.tool()
    async def af_collector_run(cell_id: int) -> Any:
        """立即执行单个采集矩阵格子（真实网络采集，需 admin）。"""
        return await call("POST", f"/api/v1/collector/matrix/{cell_id}/run")
    @server.tool()
    async def af_collector_update_cell(cell_id: int, strategy: dict | None = None, enabled: bool | None = None) -> Any:
        """更新采集矩阵格子（需 admin，主控 require_admin）：strategy 为可选部分字典
        {collect,frequency,depth,keyword,near_dup}，enabled 为启停开关；None 参数自动剔除（D1 纯透传）。"""
        return await call(
            "PUT", f"/api/v1/collector/matrix/{cell_id}",
            body={"strategy": strategy, "enabled": enabled},
        )

    @server.tool()
    async def af_collector_delete_cell(cell_id: int) -> Any:
        """删除采集矩阵格子（需 admin，主控 require_admin）。"""
        return await call("DELETE", f"/api/v1/collector/matrix/{cell_id}")

    @server.tool()
    async def af_collector_run_all() -> Any:
        """立即执行全部采集矩阵格子（真实网络采集，需 admin，主控 require_admin）。"""
        return await call("POST", "/api/v1/collector/matrix/run-all")

    @server.tool()
    async def af_collector_tech_stack() -> Any:
        """采集技术栈调研结果（GitHub 仓库统计，需 admin，主控 require_admin）。"""
        return await call("GET", "/api/v1/collector/tech-stack")

    # ============================ 账号桥接（/account-bridges，P9）============================
    @server.tool()
    async def af_account_bridges() -> Any:
        """账号桥接平台矩阵（需 admin）：多平台调研结论（AI 可接入/接入方式/开源项目/状态），不含明文凭据。"""
        return await call("GET", "/api/v1/account-bridges")

    @server.tool()
    async def af_account_bridge_detail(platform: str) -> Any:
        """单平台桥接详情（需 admin）：矩阵字段 + config_fields（动态表单）+ has_config（不含明文）。"""
        return await call("GET", f"/api/v1/account-bridges/{platform}")

    @server.tool()
    async def af_account_bridge_save(platform: str, fields: dict, reason: str = "配置账号桥接") -> Any:
        """保存平台桥接配置（敏感操作，需 admin，主控 require_admin + gate_logs 审计）：
        敏感字段（cookie/token/password/secret）Fernet 双层加密落库。"""
        return await call(
            "PUT", f"/api/v1/account-bridges/{platform}/config",
            body={"fields": fields, "reason": reason},
        )

    @server.tool()
    async def af_account_bridge_test(platform: str) -> Any:
        """测试平台桥接连接（需 admin）：结构校验 + URL/token 的 http GET 连通（timeout 5s）。"""
        return await call("POST", f"/api/v1/account-bridges/{platform}/test")

    @server.tool()
    async def af_account_bridge_ingest(platform: str, from_: str, text: str) -> Any:
        """私信接入 webhook（需 admin）：桥接器推送私信 → detections(pending) + 账号情报 upsert
        + 事件。REST body 字段名为 from（Python 关键字，工具参数以 from_ 表示）。"""
        return await call(
            "POST", f"/api/v1/account-bridges/{platform}/ingest",
            body={"from": from_, "text": text},
        )

    @server.tool()
    async def af_account_bridge_agent_import(platform: str) -> Any:
        """本地 agent（DSH/Claude Code/Codex）一键导入桥接配置（需 admin），返回接入指引 JSON。"""
        return await call("GET", f"/api/v1/account-bridges/{platform}/agent-import")

    @server.tool()
    async def af_account_bridge_delete(platform: str, reason: str = "删除账号桥接配置") -> Any:
        """删除平台桥接配置（敏感操作，需 admin，主控 require_admin + gate_logs 审计；凭据不可恢复）。"""
        return await call(
            "DELETE", f"/api/v1/account-bridges/{platform}",
            body={"reason": reason},
        )

    # ============================ 供应链反查（/supply-chain，P10）============================
    @server.tool()
    async def af_supply_chain_query(seed: str) -> Any:
        """供应链反查起链（需 admin）：输入网址/概念 → 域名提取 → 本地 DNS 解析 + 相似域名族 + 基础信号（L1/L2/L6）。"""
        return await call("POST", "/api/v1/supply-chain/query", body={"seed": seed})

    @server.tool()
    async def af_supply_chain_results() -> Any:
        """列出 var/supply-chain/ 下完整反查报告（agent workflow 产物，需 admin）。"""
        return await call("GET", "/api/v1/supply-chain/results")

    # ============================ 运维（/system /zhihu /config）============================
    @server.tool()
    async def af_health() -> Any:
        """主控健康四灯：主控/DB/LLM 配置/知乎通道。"""
        return await call("GET", "/api/v1/system/health")

    @server.tool()
    async def af_stats() -> Any:
        """运行统计（蜜饵/检测/分级/案例）。"""
        return await call("GET", "/api/v1/system/stats")
    @server.tool()
    async def af_embedded_dsh() -> Any:
        """内嵌自包含 DSH 实例状态（需 admin，主控 require_admin）：port/running/url/token_url。"""
        return await call("GET", "/api/v1/system/embedded-dsh")

    @server.tool()
    async def af_zhihu_channels() -> Any:
        """知乎四通道健康（CH-A/CH-B/CH-C/CH-D）。"""
        return await call("GET", "/api/v1/zhihu/channels")

    @server.tool()
    async def af_zhihu_status() -> Any:
        """知乎通道综合状态：限速水位/退避冷却/会话（不含明文 cookie）。"""
        return await call("GET", "/api/v1/zhihu/status")

    @server.tool()
    async def af_zhihu_login(channel: str, cookie: str, scheme: str = "header") -> Any:
        """注入并加密存储知乎 cookie（HITL：cookie 由用户自行导出粘贴；仅 ch-a/ch-b）。"""
        return await call(
            "POST", f"/api/v1/zhihu/channels/{channel}/login",
            body={"cookie": cookie, "scheme": scheme},
        )

    @server.tool()
    async def af_zhihu_verify(channel: str) -> Any:
        """校验已存 cookie（结构校验：解密+解析+关键字段）。"""
        return await call("POST", f"/api/v1/zhihu/channels/{channel}/verify")

    @server.tool()
    async def af_zhihu_import(text: str, source: str = "manual", from_url_name: str = "") -> Any:
        """CH-D 手动导入：粘贴文本/JSON → 写入检测流水线（source: zhihu|import|manual）。"""
        return await call(
            "POST", "/api/v1/zhihu/import",
            body={"text": text, "source": source, "from_url_name": from_url_name},
        )

    @server.tool()
    async def af_config() -> Any:
        """读取当前配置合成（词库阈值/LLM 路由/降级链 + configs 表覆盖值；不泄露密钥）。"""
        return await call("GET", "/api/v1/config")

    @server.tool()
    async def af_config_update(key: str, value: Any = None, confirm: bool = False, reason: str = "") -> Any:
        """更新配置（敏感操作：主控强制 confirm=true + reason≥20 字，GateLog 哈希链审计；
        写操作需 admin key）。【R4】端点已落地，行为见主控 app/api/config.py。"""
        return await call(
            "PUT", "/api/v1/config",
            body={"key": key, "value": value, "confirm": confirm, "reason": reason},
        )

    @server.tool()
    async def af_register_agent() -> Any:
        """输出当前 MCP Server 的 dsh / Claude Code / Codex 接入配置（JSON，本机自描述，不发主控请求）。"""
        return {
            "mcpServers": {
                SERVER_NAME: {
                    "command": sys.executable,
                    "args": [os.path.abspath(__file__)],
                    "env": {
                        "AF_MASTER_URL": proxy.base_url,
                        "AF_API_KEY": "<admin_key 或 AF_API_KEY>",
                    },
                }
            },
            "note": (
                "替换 AF_API_KEY 为 data/bootstrap_admin_key.txt 中的值；"
                "Windows JSON 路径需双反斜杠转义。"
            ),
        }

    # 供测试/清理使用：把代理句柄挂到 server 上
    server._af_proxy = proxy  # type: ignore[attr-defined]
    return server


# ---------------------------------------------------------------------------
# 入口（stdio 默认；--http 启用 streamable-http）
# ---------------------------------------------------------------------------
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="af-honeypot-mcp",
        description="金丝雀蜜罐 MCP 透传代理（stdio 默认；--http 启用 streamable-http）",
    )
    parser.add_argument("--http", action="store_true", help="以 streamable-http 传输启动（默认 stdio）")
    parser.add_argument("--host", default="127.0.0.1", help="HTTP 传输监听地址（默认 127.0.0.1）")
    parser.add_argument("--port", type=int, default=9201, help="HTTP 传输端口（默认 9201）")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    server = build_mcp_server()
    if args.http:
        server.run(
            transport="streamable-http",
            host=args.host,
            port=args.port,
            streamable_http_path="/mcp",
        )
    else:
        server.run(transport="stdio")


if __name__ == "__main__":
    main()
