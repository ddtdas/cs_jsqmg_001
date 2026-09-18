"""P8 MCP 挂载与透传测试（对齐 Mellivora test_mcp_mount.py）。

覆盖（DoD）：
1. 全工具挂载：list_tools ≥ 25 个；af_ 前缀；名称唯一；description 非空；
   input_schema 合法（type=object + properties）；关键工具 af_health/af_scan_text/af_list_traps 必在。
2. 三连实测（mock 主控，httpx.MockTransport）：
   af_health / af_scan_text / af_list_traps —— 验证 {ok,data} 解包、body/query 参数透传、
   错误封包 {ok:false,error} 经 ProxyError 透传（code/message 不加工）。
3. 真实主控集成三连（可选）：启动 uvicorn 子进程跑真实 FastAPI 主控，
   AF_MASTER_URL/AF_API_KEY 生效，经真实 HTTP 调用 af_health/af_scan_text/af_list_traps；
   主控无法启动则 skip 并记录原因（mock 已覆盖透传逻辑）。
4. af_register_agent 自描述配置 + --http CLI 参数解析。

运行：pytest tests/test_mcp_mount.py -q（项目根）。
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
MIN_TOOLS = 25
REQUIRED_TOOLS = ("af_health", "af_scan_text", "af_list_traps")


def _load_mcp_server():
    """以文件路径加载本项目 mcp/server.py。

    不能 `import mcp.server`：项目根 mcp/ 是命名空间段（无 __init__.py），顶层名 mcp
    被 site-packages 的官方 mcp SDK 常规包占据（常规包优先于命名空间段），
    因此本项目模块只能按文件路径加载；SDK 本身在模块内部按 mcp.server.mcpserver 正常解析。
    """
    spec = importlib.util.spec_from_file_location("af_mcp_server", ROOT / "mcp" / "server.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


mcp_mod = _load_mcp_server()


# ---------------------------------------------------------------------------
# mock 主控（httpx.MockTransport：按 method+path 路由）
# ---------------------------------------------------------------------------
def _mock_master(routes):
    """routes: {(method, path): (status, body) | callable(request)->Response}"""

    def handler(request: httpx.Request) -> httpx.Response:
        route = routes.get((request.method, request.url.path))
        if route is None:
            return httpx.Response(
                404,
                json={"ok": False, "error": {"code": "mock_unmapped", "message": f"mock 未注册 {(request.method, request.url.path)}"}},
            )
        if callable(route):
            return route(request)
        status, body = route
        return httpx.Response(status, json=body)

    return httpx.MockTransport(handler)


def _base_routes() -> dict:
    return {
        ("GET", "/api/v1/system/health"): (
            200,
            {"ok": True, "data": {"status": "ok", "db": True, "llm": "not_configured", "zhihu": "idle"}},
        ),
        ("GET", "/api/v1/traps"): (
            200,
            {"ok": True, "data": {"traps": [{"id": 1, "status": "active", "hit_count": 0}], "total": 1}},
        ),
        ("POST", "/api/v1/scan/text"): (
            200,
            {"ok": True, "data": {"detection_id": 42, "grade": "L2", "grade_reason": "规则命中"}},
        ),
        ("POST", "/api/v1/scan/inbox"): (200, {"ok": True, "data": {"queued": 3}}),
        ("GET", "/api/v1/events"): (
            404,
            {"ok": False, "error": {"code": "not_found", "message": "端点未实现（规划中）"}},
        ),
    }


def _run(coro):
    """同步测试内跑协程（复用 asyncio.run，规避 pytest-asyncio 配置依赖）。"""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# 1. 全工具挂载
# ---------------------------------------------------------------------------
def test_mount_all_tools():
    async def go():
        proxy = mcp_mod.MasterProxy("http://mock", "af_admin_test", transport=_mock_master(_base_routes()))
        srv = mcp_mod.build_mcp_server(proxy=proxy)
        try:
            tools = await srv.list_tools()
            names = [t.name for t in tools]
            assert len(tools) >= MIN_TOOLS, f"挂载工具数 {len(tools)} < {MIN_TOOLS}"
            assert len(set(names)) == len(names), "工具名存在重复"
            for t in tools:
                assert t.name.startswith("af_"), f"工具名缺 af_ 前缀: {t.name}"
                assert t.description, f"{t.name} 缺 description"
                schema = t.input_schema
                assert isinstance(schema, dict) and schema.get("type") == "object", f"{t.name} 非法 input_schema"
                assert isinstance(schema.get("properties", {}), dict), f"{t.name} properties 缺失"
            for must in REQUIRED_TOOLS:
                assert must in names, f"必备工具 {must} 未挂载"
            return names
        finally:
            await proxy.aclose()

    names = _run(go())
    print(f"\n[P8] 挂载 {len(names)} 个工具: {', '.join(names)}")


# ---------------------------------------------------------------------------
# 2. 三连实测（mock 主控透传逻辑）
# ---------------------------------------------------------------------------
def test_three_tools_mock_passthrough():
    async def go():
        proxy = mcp_mod.MasterProxy("http://mock", "af_admin_test", transport=_mock_master(_base_routes()))
        srv = mcp_mod.build_mcp_server(proxy=proxy)
        try:
            # af_health
            r = await srv.call_tool("af_health", {})
            data = json.loads(r.content[0].text)
            assert data["status"] == "ok" and data["db"] is True

            # af_scan_text
            r = await srv.call_tool("af_scan_text", {"text": "杀猪盘带你投资", "source": "zhihu"})
            data = json.loads(r.content[0].text)
            assert data["detection_id"] == 42 and data["grade"] == "L2"

            # af_list_traps
            r = await srv.call_tool("af_list_traps", {"status": "active"})
            data = json.loads(r.content[0].text)
            assert data["total"] == 1 and data["traps"][0]["status"] == "active"
            return True
        finally:
            await proxy.aclose()

    assert _run(go())


def test_param_passthrough_body_and_query():
    captured = {}
    RETIRE_REASON = "这是一条超过二十个字长度的真实退役测试理由说明文字"
    PUBLISH_REASON = "这是一条超过二十个字长度的真实入库发布测试理由说明文字"
    CONFIG_REASON = "这是一条超过二十个字长度的真实配置修改测试理由说明文字"

    def scan_route(request: httpx.Request) -> httpx.Response:
        captured["scan_body"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True, "data": {"detection_id": 7, "grade": "L3"}})

    def traps_route(request: httpx.Request) -> httpx.Response:
        captured["traps_query"] = dict(request.url.params)
        return httpx.Response(200, json={"ok": True, "data": []})

    def events_route(request: httpx.Request) -> httpx.Response:
        captured["events_query"] = dict(request.url.params)
        return httpx.Response(200, json={"ok": True, "data": {"items": [], "total": 0, "page": 1}})

    def inbox_route(request: httpx.Request) -> httpx.Response:
        captured["inbox_body"] = request.content
        return httpx.Response(200, json={"ok": True, "data": {"processed": 0, "items": []}})

    def retire_route(request: httpx.Request) -> httpx.Response:
        captured["retire_body"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True, "data": {"id": 1, "status": "retired"}})

    def publish_route(request: httpx.Request) -> httpx.Response:
        captured["publish_body"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True, "data": {"id": 9}})

    def config_put_route(request: httpx.Request) -> httpx.Response:
        captured["config_body"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True, "data": {"key": "x"}})

    routes = {
        ("POST", "/api/v1/scan/text"): scan_route,
        ("GET", "/api/v1/traps"): traps_route,
        ("GET", "/api/v1/events"): events_route,
        ("POST", "/api/v1/scan/inbox"): inbox_route,
        ("POST", "/api/v1/traps/3/retire"): retire_route,
        ("POST", "/api/v1/cases/publish"): publish_route,
        ("PUT", "/api/v1/config"): config_put_route,
    }

    async def go():
        proxy = mcp_mod.MasterProxy("http://mock", "af_admin_test", transport=_mock_master(routes))
        srv = mcp_mod.build_mcp_server(proxy=proxy)
        try:
            await srv.call_tool("af_scan_text", {"text": "在么，我教你理财", "source": "zhihu"})
            await srv.call_tool("af_list_traps", {"status": "monitored", "limit": 5})

            # R4：af_list_events 对齐主控 page/page_size（limit 已移除）
            await srv.call_tool("af_list_events", {"kind": "hit", "page": 2, "page_size": 35})
            # R4：af_scan_inbox 无 body（主控 POST /scan/inbox 无请求体）
            await srv.call_tool("af_scan_inbox", {})
            # R4：敏感操作 confirm+reason 必须原样透传到 body
            await srv.call_tool(
                "af_retire_trap",
                {"trap_id": 3, "confirm": True, "reason": RETIRE_REASON},
            )
            await srv.call_tool(
                "af_publish_case",
                {"det_id": 5, "redacted_payload": "脱敏内容", "confirm": True, "reason": PUBLISH_REASON},
            )
            await srv.call_tool(
                "af_config_update",
                {"key": "grading.l3_min", "value": 9.5, "confirm": True, "reason": CONFIG_REASON},
            )
        finally:
            await proxy.aclose()

    _run(go())
    assert captured["scan_body"] == {"text": "在么，我教你理财", "source": "zhihu"}, captured
    assert captured["traps_query"] == {"status": "monitored", "limit": "5"}, captured
    assert captured["events_query"] == {"kind": "hit", "page": "2", "page_size": "35"}, captured
    assert captured["inbox_body"] == b"", "af_scan_inbox 不应携带 body"
    assert captured["retire_body"] == {"confirm": True, "reason": RETIRE_REASON}, captured
    assert captured["publish_body"]["confirm"] is True and captured["publish_body"]["reason"] == PUBLISH_REASON, captured
    assert captured["config_body"] == {"key": "grading.l3_min", "value": 9.5, "confirm": True, "reason": CONFIG_REASON}, captured


def test_error_envelope_propagates():
    """{ok:false,error} 必须经 ProxyError 透传（code/message 原样，不吞不造）。"""

    async def go():
        proxy = mcp_mod.MasterProxy("http://mock", "af_admin_test", transport=_mock_master(_base_routes()))
        srv = mcp_mod.build_mcp_server(proxy=proxy)
        try:
            # proxy 层：unwrap 直接抛 ProxyError
            with pytest.raises(mcp_mod.ProxyError) as ei:
                await proxy.request("GET", "/api/v1/events")
            assert ei.value.code == "not_found"
            assert "未实现" in ei.value.message

            # call_tool 层：SDK 包装为 ToolError（R1-A8 修复后保留原文），cause 即 ProxyError
            with pytest.raises(Exception) as ei2:
                await srv.call_tool("af_list_events", {"kind": "hit"})
            cause = ei2.value.__cause__
            assert isinstance(cause, mcp_mod.ProxyError), f"cause 应为 ProxyError，实际 {type(cause)}"
            assert cause.code == "not_found"
            # 客户端可见文本（isError content）应含 code+message，而非仅通用 'Error executing tool'
            text = str(ei2.value)
            assert "not_found" in text and "未实现" in text, text
            return True
        finally:
            await proxy.aclose()

    assert _run(go())


def test_error_code_passthrough_visible_to_agent():
    """R1-A8 DoD：mock 主控返回 {ok:false,error:{code:'trap_not_found',message}} →
    MCP 工具调用错误（isError 语义）中必须含 code 与 message 原文，不得被 SDK 吞成
    通用 'Error executing tool <name>'。"""

    routes = {
        ("GET", "/api/v1/traps/404"): (
            404,
            {"ok": False, "error": {"code": "trap_not_found", "message": "蜜饵不存在或已退役"}},
        ),
        ("POST", "/api/v1/traps/9/check-hit"): (
            409,
            {"ok": False, "error": {"code": "trap_not_active", "message": "蜜饵不在 active 状态"}},
        ),
    }

    async def go():
        proxy = mcp_mod.MasterProxy("http://mock", "af_admin_test", transport=_mock_master(routes))
        srv = mcp_mod.build_mcp_server(proxy=proxy)
        try:
            # GET 路径（af_get_trap）：错误码+消息进入客户端可见文本
            with pytest.raises(Exception) as ei:
                await srv.call_tool("af_get_trap", {"trap_id": 404})
            exc = ei.value
            # 必须是 SDK ToolError（预期失败，原文保留）而非 UnexpectedToolError（原文被吞）
            assert isinstance(exc, mcp_mod.ToolError), f"应保留原文的 ToolError，实际 {type(exc).__name__}"
            assert "UnexpectedToolError" not in type(exc).__name__, type(exc).__name__
            text = str(exc)
            assert "trap_not_found" in text, text
            assert "蜜饵不存在" in text, text
            cause = exc.__cause__
            assert isinstance(cause, mcp_mod.ProxyError), f"cause 应为 ProxyError，实际 {type(cause)}"
            assert cause.code == "trap_not_found"
            assert "蜜饵不存在" in cause.message

            # POST 路径（af_check_trap_hit）：同样原样透传
            with pytest.raises(Exception) as ei2:
                await srv.call_tool("af_check_trap_hit", {"trap_id": 9, "text": "测试"})
            text2 = str(ei2.value)
            assert "trap_not_active" in text2 and "不在 active" in text2, text2
            return (text, text2)
        finally:
            await proxy.aclose()

    t1, t2 = _run(go())
    print(f"\n[P8-A8] GET 错误透传: {t1}")
    print(f"[P8-A8] POST 错误透传: {t2}")


def test_master_unreachable_clean_error():
    """主控不可达（连接拒绝）→ ProxyError master_unreachable，不裸抛 httpx 异常。"""

    async def go():
        # 指向必然拒绝的端口；transport 缺省走真实网络
        proxy = mcp_mod.MasterProxy("http://127.0.0.1:1", "af_admin_test", timeout=1.0)
        srv = mcp_mod.build_mcp_server(proxy=proxy)
        try:
            with pytest.raises(Exception) as ei:
                await srv.call_tool("af_health", {})
            cause = ei.value.__cause__
            assert isinstance(cause, mcp_mod.ProxyError), f"cause 应为 ProxyError，实际 {type(cause)}"
            assert "master_unreachable" in cause.code, cause
            return True
        finally:
            await proxy.aclose()

    assert _run(go())


def test_register_agent_self_description():
    async def go():
        proxy = mcp_mod.MasterProxy("http://127.0.0.1:9200", "af_admin_x")
        srv = mcp_mod.build_mcp_server(proxy=proxy)
        try:
            r = await srv.call_tool("af_register_agent", {})
            cfg = json.loads(r.content[0].text)
            assert "mcpServers" in cfg and "af-honeypot" in cfg["mcpServers"]
            server_cfg = cfg["mcpServers"]["af-honeypot"]
            assert server_cfg["env"]["AF_MASTER_URL"] == "http://127.0.0.1:9200"
            assert str(ROOT / "mcp" / "server.py") in server_cfg["args"][0]
        finally:
            await proxy.aclose()

    _run(go())


def test_cli_args_parse():
    args = mcp_mod.parse_args([])
    assert args.http is False and args.port == 9201
    args = mcp_mod.parse_args(["--http", "--port", "9321", "--host", "0.0.0.0"])
    assert args.http is True and args.port == 9321 and args.host == "0.0.0.0"


# ---------------------------------------------------------------------------
# P1-C16：D1 三角同步新增透传工具（account-bridges / supply-chain / collector
#         update|delete|run-all|tech-stack / system embedded-dsh）—— 挂载 + mock 透传
# ---------------------------------------------------------------------------
NEW_TOOLS_C16 = (
    "af_account_bridges", "af_account_bridge_detail", "af_account_bridge_save",
    "af_account_bridge_test", "af_account_bridge_ingest", "af_account_bridge_agent_import",
    "af_account_bridge_delete", "af_supply_chain_query", "af_supply_chain_results",
    "af_collector_update_cell", "af_collector_delete_cell", "af_collector_run_all",
    "af_collector_tech_stack", "af_embedded_dsh",
)


def test_c16_new_tools_mounted():
    """P1-C16 DoD：14 个新增透传工具必须可被 mcp client 列出（挂载可见）。"""

    async def go():
        proxy = mcp_mod.MasterProxy("http://mock", "af_admin_test", transport=_mock_master(_base_routes()))
        srv = mcp_mod.build_mcp_server(proxy=proxy)
        try:
            tools = await srv.list_tools()
            names = [t.name for t in tools]
            assert len(tools) >= MIN_TOOLS + 14, f"挂载工具数 {len(tools)} 应 >= {MIN_TOOLS + 14}"
            missing = [n for n in NEW_TOOLS_C16 if n not in names]
            assert not missing, f"P1-C16 新增工具缺失: {missing}"
            for n in NEW_TOOLS_C16:
                t = next(t for t in tools if t.name == n)
                assert t.description, f"{n} 缺 description"
                assert isinstance(t.input_schema.get("properties", {}), dict), f"{n} 非法 input_schema"
            return names
        finally:
            await proxy.aclose()

    names = _run(go())
    print(f"\n[P1-C16] 挂载 {len(names)} 个工具（新增 14 个全在）")


def test_c16_new_tools_passthrough():
    """P1-C16 DoD：新增工具经 mock 主控实测透传 —— body/query 原样到达、{ok,data} 统一解包、
    None 参数剔除（af_collector_update_cell strategy=None 不落入 body）。"""
    captured = {}

    def bridge_save(request):
        captured["bridge_save"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True, "data": {"platform": "wechat", "status": "configured"}})

    def bridge_test(request):
        captured["bridge_test"] = request.content
        return httpx.Response(200, json={"ok": True, "data": {"ok": True, "detail": "ok", "latency_ms": 5}})

    def bridge_ingest(request):
        captured["bridge_ingest"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True, "data": {"detection_id": 88}})

    def bridge_delete(request):
        captured["bridge_delete"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True, "data": {"platform": "wechat", "status": "unconfigured"}})

    def coll_update(request):
        captured["coll_update"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True, "data": {"id": 26, "enabled": True}})

    def coll_delete(request):
        return httpx.Response(200, json={"ok": True, "data": {"id": 26, "deleted": True}})

    def sc_query(request):
        captured["sc_query"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True, "data": {"seed": "https://example.com", "domain": "example.com", "mode": "local_recon"}})

    def embedded(request):
        return httpx.Response(200, json={"ok": True, "data": {"port": 3092, "running": True, "token_url": "http://127.0.0.1:3092/?token=x"}})

    def simple_ok(request):
        return httpx.Response(200, json={"ok": True, "data": {"ok": True}})

    routes = {
        ("GET", "/api/v1/account-bridges"): simple_ok,
        ("GET", "/api/v1/account-bridges/wechat"): simple_ok,
        ("PUT", "/api/v1/account-bridges/wechat/config"): bridge_save,
        ("POST", "/api/v1/account-bridges/wechat/test"): bridge_test,
        ("POST", "/api/v1/account-bridges/wechat/ingest"): bridge_ingest,
        ("GET", "/api/v1/account-bridges/wechat/agent-import"): simple_ok,
        ("DELETE", "/api/v1/account-bridges/wechat"): bridge_delete,
        ("POST", "/api/v1/supply-chain/query"): sc_query,
        ("GET", "/api/v1/supply-chain/results"): simple_ok,
        ("PUT", "/api/v1/collector/matrix/26"): coll_update,
        ("DELETE", "/api/v1/collector/matrix/26"): coll_delete,
        ("POST", "/api/v1/collector/matrix/run-all"): simple_ok,
        ("GET", "/api/v1/collector/tech-stack"): simple_ok,
        ("GET", "/api/v1/system/embedded-dsh"): embedded,
    }

    async def go():
        proxy = mcp_mod.MasterProxy("http://mock", "af_admin_test", transport=_mock_master(routes))
        srv = mcp_mod.build_mcp_server(proxy=proxy)
        try:
            r = await srv.call_tool("af_account_bridges", {})
            assert json.loads(r.content[0].text)["ok"] is True
            r = await srv.call_tool("af_account_bridge_detail", {"platform": "wechat"})
            assert json.loads(r.content[0].text)["ok"] is True
            r = await srv.call_tool("af_account_bridge_save", {"platform": "wechat", "fields": {"url": "https://x", "token": "t"}})
            assert json.loads(r.content[0].text)["status"] == "configured"
            r = await srv.call_tool("af_account_bridge_test", {"platform": "wechat"})
            assert json.loads(r.content[0].text)["latency_ms"] == 5
            r = await srv.call_tool("af_account_bridge_ingest", {"platform": "wechat", "from_": "骗子A", "text": "加群领理财"})
            assert json.loads(r.content[0].text)["detection_id"] == 88
            r = await srv.call_tool("af_account_bridge_agent_import", {"platform": "wechat"})
            assert json.loads(r.content[0].text)["ok"] is True
            r = await srv.call_tool("af_account_bridge_delete", {"platform": "wechat"})
            assert json.loads(r.content[0].text)["status"] == "unconfigured"
            r = await srv.call_tool("af_supply_chain_query", {"seed": "https://example.com"})
            assert json.loads(r.content[0].text)["domain"] == "example.com"
            r = await srv.call_tool("af_supply_chain_results", {})
            assert json.loads(r.content[0].text)["ok"] is True
            r = await srv.call_tool("af_collector_update_cell", {"cell_id": 26, "enabled": True})
            assert json.loads(r.content[0].text)["enabled"] is True
            r = await srv.call_tool("af_collector_delete_cell", {"cell_id": 26})
            assert json.loads(r.content[0].text)["deleted"] is True
            r = await srv.call_tool("af_collector_run_all", {})
            assert json.loads(r.content[0].text)["ok"] is True
            r = await srv.call_tool("af_collector_tech_stack", {})
            assert json.loads(r.content[0].text)["ok"] is True
            r = await srv.call_tool("af_embedded_dsh", {})
            assert json.loads(r.content[0].text)["port"] == 3092
            return True
        finally:
            await proxy.aclose()

    assert _run(go())
    assert captured["bridge_save"]["fields"] == {"url": "https://x", "token": "t"}, captured
    assert captured["bridge_test"] == b"", "af_account_bridge_test 不应携带 body"
    assert captured["bridge_ingest"] == {"from": "骗子A", "text": "加群领理财"}, captured
    assert captured["bridge_delete"] == {"reason": "删除账号桥接配置"}, captured
    assert captured["coll_update"] == {"enabled": True}, "strategy=None 应被剔除: %s" % captured
    assert captured["sc_query"] == {"seed": "https://example.com"}, captured

# 3. 真实主控集成三连（uvicorn 子进程；起不来则 skip，mock 已覆盖透传逻辑）
# ---------------------------------------------------------------------------
def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_http(url: str, timeout: float = 25.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = httpx.get(url, timeout=2.0)
            if r.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def test_live_master_three_tools(tmp_path):
    """真实主控跑三连：af_health / af_scan_text / af_list_traps。"""
    port = _free_port()
    env = os.environ.copy()
    env["AF_DATA_DIR"] = str(tmp_path / "data")
    env["AF_PORT"] = str(port)
    env["AF_API_KEY"] = "af_admin_mcp_test"
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=str(ROOT), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        if not _wait_http(f"{base}/api/v1/system/health"):
            pytest.skip("真实主控未能启动，跳过集成三连（mock 测试已覆盖透传逻辑）")

        async def go():
            proxy = mcp_mod.MasterProxy(base, "af_admin_mcp_test")
            srv = mcp_mod.build_mcp_server(proxy=proxy)
            try:
                r = await srv.call_tool("af_health", {})
                health = json.loads(r.content[0].text)
                assert health["status"] == "ok", health

                r = await srv.call_tool("af_scan_text", {"text": "杀猪盘带你投资稳赚不赔"})
                scan = json.loads(r.content[0].text)
                assert isinstance(scan.get("detection_id"), int), scan
                assert scan.get("grade") in ("L1", "L2", "L3", "L4", "L5"), scan

                r = await srv.call_tool("af_list_traps", {})
                traps = json.loads(r.content[0].text)
                items = traps.get("items", traps.get("traps"))
                assert isinstance(items, list), traps

                # R4：af_list_events 用 page/page_size 实测（config_update 会插入 events，此处应有事件流）
                r = await srv.call_tool("af_list_events", {"page": 1, "page_size": 20})
                events = json.loads(r.content[0].text)
                assert isinstance(events.get("items"), list), events
                assert events.get("page") == 1 and events.get("page_size") == 20, events

                # R4：af_scan_inbox 真实主控触发（空 DB → processed 0，不应报错）
                r = await srv.call_tool("af_scan_inbox", {})
                inbox = json.loads(r.content[0].text)
                assert isinstance(inbox.get("processed"), int), inbox

                return {
                    "health": health["status"],
                    "scan_grade": scan.get("grade"),
                    "traps_total": len(items),
                    "events_total": events.get("total"),
                    "inbox_processed": inbox.get("processed"),
                }
            finally:
                await proxy.aclose()

        summary = _run(go())
        print(f"\n[P8-INTEGRATION] 真实主控三连 OK: {summary}")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_live_master_gate_confirm(tmp_path):
    """R4 联动验证：MCP 敏感工具 confirm+reason 透传后，主控闸控真实拦截/放行（P1-2）。

    起真实主控，实测 af_retire_trap / af_publish_case / af_config_update：
    - confirm=false → 主控 403 confirm_required（经 ProxyError 原样透传 code）
    - confirm=true 但 reason<20 字 → 422 reason_too_short
    - confirm=true + reason≥20 → 成功，且 gate_logs 哈希链落盘对应 action
    """
    import sqlite3 as _sqlite3

    port = _free_port()
    data_dir = tmp_path / "data"
    env = os.environ.copy()
    env["AF_DATA_DIR"] = str(data_dir)
    env["AF_PORT"] = str(port)
    env["AF_API_KEY"] = "af_admin_mcp_test"
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=str(ROOT), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        if not _wait_http(f"{base}/api/v1/system/health"):
            pytest.skip("真实主控未能启动，跳过闸控穿透验证")

        async def go():
            proxy = mcp_mod.MasterProxy(base, "af_admin_mcp_test")
            srv = mcp_mod.build_mcp_server(proxy=proxy)
            try:
                async def expect_gate(tool, args, code: str):
                    with pytest.raises(Exception) as ei:
                        await srv.call_tool(tool, args)
                    exc = ei.value
                    # R1-A8：错误码必须出现在客户端可见文本（isError content），非仅 cause 链
                    assert isinstance(exc, mcp_mod.ToolError), f"{tool} 应为 SDK ToolError，实际 {type(exc).__name__}"
                    assert code in str(exc), f"{tool}: code {code} 未出现在客户端可见文本: {str(exc)!r}"
                    cause = exc.__cause__
                    assert isinstance(cause, mcp_mod.ProxyError), f"{tool} cause 应为 ProxyError: {cause!r}"
                    assert cause.code == code, f"{tool}: 期望 {code}，实际 {cause.code}: {cause.message}"

                REASON_OK = "这是一条超过二十个字长度的真实操作理由说明文字"

                # ---- af_config_update：闸控拦截 → 放行 ----
                # 注意：主控 ConfigUpdateRequest.reason 有 min_length=1，给非空 reason 才能测到
                # confirm=false 的 403 confirm_required（空 reason 会先触发 422 validation_error）
                await expect_gate(
                    "af_config_update",
                    {"key": "grading.l3_min", "value": 9.5, "reason": "操作理由文本"},
                    "confirm_required",
                )
                await expect_gate(
                    "af_config_update",
                    {"key": "grading.l3_min", "value": 9.5, "confirm": True, "reason": "太短"},
                    "reason_too_short",
                )
                r = await srv.call_tool(
                    "af_config_update",
                    {"key": "grading.l3_min", "value": 9.5, "confirm": True, "reason": REASON_OK},
                )
                cfg = json.loads(r.content[0].text)
                assert cfg.get("key") == "grading.l3_min", cfg
                assert cfg["audit"]["action"] == "config.put", cfg

                # ---- af_retire_trap：先建蜜饵（管理员创建），再验证闸控 ----
                created = await proxy.request(
                    "POST", "/api/v1/traps",
                    body={"bait_text": "测试蜜饵文案：稳赚不赔带你投资", "note": "mcp-gate-test"},
                )
                trap_id = int(created["id"])
                await expect_gate("af_retire_trap", {"trap_id": trap_id}, "confirm_required")
                await expect_gate(
                    "af_retire_trap",
                    {"trap_id": trap_id, "confirm": True, "reason": "太短"},
                    "reason_too_short",
                )
                r = await srv.call_tool(
                    "af_retire_trap",
                    {"trap_id": trap_id, "confirm": True, "reason": REASON_OK},
                )
                retired = json.loads(r.content[0].text)
                assert retired.get("status") == "retired", retired

                # ---- af_publish_case：先扫描一条检测记录，再验证闸控 ----
                r = await srv.call_tool("af_scan_text", {"text": "杀猪盘带你投资稳赚不赔"})
                scan = json.loads(r.content[0].text)
                det_id = int(scan["detection_id"])
                await expect_gate(
                    "af_publish_case",
                    {"det_id": det_id, "redacted_payload": "骗子诱导下载APP", "confirm": False},
                    "confirm_required",
                )
                await expect_gate(
                    "af_publish_case",
                    {"det_id": det_id, "redacted_payload": "骗子诱导下载APP", "confirm": True, "reason": "太短"},
                    "reason_too_short",
                )
                r = await srv.call_tool(
                    "af_publish_case",
                    {"det_id": det_id, "redacted_payload": "骗子诱导下载APP", "confirm": True, "reason": REASON_OK},
                )
                published = json.loads(r.content[0].text)
                assert isinstance(published.get("id"), int), published

                # ---- gate_logs 哈希链落盘核对（三个 action 都应存在）----
                db_path = data_dir / "af.db"
                assert db_path.exists(), f"主控 DB 未生成: {db_path}"
                conn = _sqlite3.connect(str(db_path))
                try:
                    rows = conn.execute(
                        "SELECT action, reason FROM gate_logs ORDER BY id"
                    ).fetchall()
                finally:
                    conn.close()
                actions = {action for action, _ in rows}
                assert {"config.put", "trap.retire", "case.publish"} <= actions, rows
                # reason 原样落库（≥20 字那条）
                assert REASON_OK in [r_ for _, r_ in rows], rows

                return {
                    "config": cfg["audit"]["action"],
                    "retire_status": retired.get("status"),
                    "publish_id": published.get("id"),
                    "gate_logs": sorted(actions),
                }
            finally:
                await proxy.aclose()

        summary = _run(go())
        print(f"\n[R4-GATE] 真实主控 confirm+reason 闸控穿透 OK: {summary}")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()