#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""P10 端到端功能验收脚本（可重复运行）。

用法（主控须已启动，默认 127.0.0.1:9200）：
    .venv\\Scripts\\python.exe tests\\e2e_acceptance.py [--base http://127.0.0.1:9200] [--api-key af_admin_xxx]

覆盖 0-14 项：
  0 启动健康  1 认证链路  2 话术检测(R2)  3 五级分级(R4)  4 蜜饵全生命周期(R1)
  5 账号速查(R3)  6 证据包(R5)  7 案例库(R6)  8 告警(L3+闸控)  9 知乎通道(P2)
  10 事件流(P6.5)  11 配置(P6.5)  12 WebUI(P7)  13 MCP(P8)  14 全链路串联

输出：逐项 PASS/FAIL 打印 + 汇总 JSON（--out scripts/_e2e_results.json）。
退出码：0 = 全部通过；1 = 存在失败。
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import secrets
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.desensitize import DesensitizeService  # noqa: E402

RESULTS: list[dict] = []


def check(name: str, passed: bool, detail: str = "") -> None:
    RESULTS.append({"name": name, "passed": bool(passed), "detail": detail})
    mark = "PASS" if passed else "FAIL"
    print(f"[{mark}] {name}  {detail}")
    return passed


class E2E:
    def __init__(self, base: str, api_key: str):
        self.base = base.rstrip("/")
        self.key = api_key
        self.client = httpx.Client(timeout=30.0, follow_redirects=False)

    # ------------------------------------------------------------------ helpers
    def _headers(self, admin: bool = False) -> dict:
        h = {"Content-Type": "application/json"}
        if admin:
            h["X-API-Key"] = self.key
        return h

    def req(self, method: str, path: str, *, admin: bool = False, json_body=None, params=None) -> httpx.Response:
        return self.client.request(
            method, f"{self.base}{path}",
            headers=self._headers(admin),
            json=json_body,
            params=params,
        )

    def data(self, r: httpx.Response) -> dict:
        try:
            return r.json()
        except Exception:
            return {}

    # ------------------------------------------------------------------ steps
    def step0_health(self) -> None:
        up = False
        for _ in range(30):
            try:
                r = self.req("GET", "/api/v1/system/health")
                if r.status_code == 200:
                    up = True
                    break
            except Exception:
                time.sleep(1)
        if not up:
            check("0-主控健康", False, "主控不可达")
            raise SystemExit("master unreachable")
        d = self.data(r)["data"]
        check("0-主控健康", True,
              f"status={d.get('status')} version={d.get('version')} db={d.get('db')} llm={d.get('llm')}")
        check("0-版本号", d.get("version") == "1.0.0", f"version={d.get('version')}")

    def step1_auth(self) -> None:
        r = self.req("POST", "/api/v1/auth/bootstrap-info")
        d = self.data(r)
        check("1-认证/bootstrap-info", r.status_code == 200 and d.get("ok") and d["data"].get("initialized") is True,
              f"initialized={d.get('data', {}).get('initialized')}")

        r2 = self.req("GET", "/api/v1/system/stats", admin=True)
        check("1-认证/带key受保护端点", r2.status_code == 200 and self.data(r2).get("ok"),
              f"status={r2.status_code}")

        r3 = self.req("GET", "/api/v1/system/stats")
        body = self.data(r3)
        check("1-认证/无key拒绝401封包", r3.status_code == 401
              and body.get("ok") is False and "code" in body.get("error", {}),
              f"status={r3.status_code} code={body.get('error', {}).get('code')}")

    def step2_scan(self) -> list[int]:
        scams = [
            "杀猪盘带你投资稳赚不赔，加老师微信领取内幕消息",
            "刷单返利，垫付解锁任务，一单一结佣金，日入五百",
            "您好，这里是XX银行风控中心，您的账户异常，请将资金转入安全账户核实",
            "稳赚不赔的理财项目，导师带你内幕消息，先充5000试试水",
            "恭喜您中奖了！请先缴纳保证金和手续费才能领取奖品",
        ]
        normals = [
            "今天天气不错，我们去公园散步吧",
            "这份季度报告周三之前需要交到领导办公室",
            "周末一起吃饭吗？我订了那家新开的川菜馆",
        ]
        hit_ids: list[int] = []
        ok_all = True
        for i, t in enumerate(scams, 1):
            r = self.req("POST", "/api/v1/scan/text", json_body={"text": t, "source": "manual"})
            d = self.data(r).get("data", {})
            good = (r.status_code == 200 and d.get("verdict") != "normal"
                    and isinstance(d.get("hits"), list) and len(d["hits"]) >= 1
                    and d.get("grade") in ("L1", "L2", "L3", "L4", "L5"))
            if good:
                hit_ids.append(d["detection_id"])
            check(f"2-话术检测/诈骗话术{i}『{t[:12]}…』", good,
                  f"verdict={d.get('verdict')} grade={d.get('grade')} hits={len(d.get('hits') or [])}")
            if not good:
                ok_all = False
        for i, t in enumerate(normals, 1):
            r = self.req("POST", "/api/v1/scan/text", json_body={"text": t, "source": "manual"})
            d = self.data(r).get("data", {})
            check(f"2-话术检测/正常语料{i}『{t[:10]}…』", r.status_code == 200 and d.get("verdict") == "normal",
                  f"verdict={d.get('verdict')}")
            if d.get("verdict") != "normal":
                ok_all = False
        r = self.req("GET", "/api/v1/scan/hits")
        items = self.data(r).get("data", [])
        check("2-话术检测/scan/hits有记录", isinstance(items, list) and len(items) >= 5,
              f"hits={len(items)}")
        check("2-话术检测/至少5条诈骗话术入链", len(hit_ids) >= 5, f"detection_ids={len(hit_ids)}")
        if not ok_all:
            pass  # 单项已记录失败
        return hit_ids

    def step3_grading(self, det_id: int) -> None:
        r = self.req("GET", "/api/v1/grading/levels")
        d = self.data(r).get("data", [])
        levels = {x.get("level") for x in d if isinstance(x, dict)}
        check("3-分级/levels含L1-L5", r.status_code == 200 and isinstance(d, list)
              and all(f"L{i}" in levels for i in range(1, 6)),
              f"levels={sorted(levels)}")
        r2 = self.req("GET", f"/api/v1/grading/explain/{det_id}")
        d2 = self.data(r2).get("data", {})
        has_chain = isinstance(d2.get("grade_reason"), list) and len(d2["grade_reason"]) >= 1
        check("3-分级/explain证据链", r2.status_code == 200 and has_chain and d2.get("grade") in
              ("L1", "L2", "L3", "L4", "L5"),
              f"grade={d2.get('grade')} reason_len={len(d2.get('grade_reason') or [])}")

    def step4_traps(self) -> None:
        # P1-1：写操作（generate-draft/deploy/monitor）需 admin
        r = self.req("POST", "/api/v1/traps/generate-draft", admin=True,
                     json_body={"platform": "评论区", "note": "E2E验收"})
        d = self.data(r).get("data", {})
        tid = d.get("id")
        check("4-蜜饵/生成草稿不发布", r.status_code == 200 and tid and d.get("status") == "draft"
              and d.get("fingerprint") and "hitl_hint" in d,
              f"id={tid} status={d.get('status')} disguise={d.get('disguise_score')}")

        r2 = self.req("POST", f"/api/v1/traps/{tid}/deploy", admin=True,
                      json_body={"target_url": "https://www.zhihu.com/question/1"})
        check("4-蜜饵/deploy→active", r2.status_code == 200 and self.data(r2).get("data", {}).get("status") == "active",
              f"status={self.data(r2).get('data', {}).get('status')}")

        r3 = self.req("POST", f"/api/v1/traps/{tid}/monitor", admin=True)
        check("4-蜜饵/monitor→monitored", r3.status_code == 200 and self.data(r3).get("data", {}).get("status") == "monitored",
              f"status={self.data(r3).get('data', {}).get('status')}")

        bait = d.get("bait_text", "")
        # L1 修复（R7）：与 DoD「改写 5-10 字仍召回」对齐的 **5 字改写**变体——
        # 尾 5 字替换（实测对全部离线模板 overlap ≥0.667 > 0.6，命中稳定）。
        # 原「bait[:len-6]+长后缀」构造改写 16/26≈62% 超出梯度（overlap 0.559<0.6），
        # 在 P3-6 确定性模板下不稳定（复验 L1）。
        variant = (bait[:-5] + "真挺不错呀") if len(bait) >= 12 else (bait + "真挺不错呀")
        r4 = self.req("POST", f"/api/v1/traps/{tid}/check-hit", json_body={"text": variant})
        d4 = self.data(r4).get("data", {})
        hit_ok = r4.status_code == 200 and d4.get("hit") is True
        check("4-蜜饵/踩饵命中实锤", hit_ok, f"hit={d4.get('hit')} sim={d4.get('similarity')}")

        r5 = self.req("GET", f"/api/v1/traps/{tid}")
        d5 = self.data(r5).get("data", {})
        check("4-蜜饵/命中即退役", r5.status_code == 200 and d5.get("status") == "retired",
              f"status={d5.get('status')}")

        r6 = self.req("GET", "/api/v1/traps")
        items = self.data(r6).get("data", [])
        check("4-蜜饵/traps列表", r6.status_code == 200 and isinstance(items, list) and len(items) >= 1,
              f"total={len(items)}")

    def step5_accounts(self) -> None:
        # R4-F3：画像 upsert 需已认证（匿名 check 只读评估不落库）→ 此处带 admin key
        r = self.req("POST", "/api/v1/accounts/check", admin=True, json_body={
            "url_name": "e2e_suspect_001",
            "signals": {"registered_at": "2026-01-01", "reported_count": 3, "trap_hit_ids": [1]},
        })
        d = self.data(r).get("data", {})
        check("5-账号速查/check返回风险等级", r.status_code == 200 and d.get("risk_level") in ("green", "yellow", "red"),
              f"risk={d.get('risk_level')}")

        r2 = self.req("GET", "/api/v1/accounts/e2e_suspect_001")
        d2 = self.data(r2).get("data", {})
        check("5-账号速查/get画像", r2.status_code == 200 and d2.get("url_name") == "e2e_suspect_001",
              f"url_name={d2.get('url_name')}")

        acct_id = d2.get("id") or d.get("account_id")
        if acct_id:
            r3 = self.req("GET", f"/api/v1/accounts/{acct_id}/timeline")
            check("5-账号速查/timeline", r3.status_code == 200, f"status={r3.status_code}")
        else:
            check("5-账号速查/timeline", False, "无 account_id")

    def step6_evidence(self, det_ids: list[int]) -> None:
        # P1-1：写操作（build/freeze/export）需 admin
        r = self.req("POST", "/api/v1/evidence/build", admin=True, json_body={"det_ids": det_ids[:2]})
        d = self.data(r).get("data", {})
        pkg = d.get("pkg_id") or d.get("id")
        check("6-证据包/build", r.status_code == 200 and pkg, f"pkg_id={pkg}")

        r2 = self.req("GET", f"/api/v1/evidence/{pkg}", admin=True)
        d2 = self.data(r2).get("data", {})
        v = d2.get("verified") if isinstance(d2.get("verified"), dict) else {}
        check("6-证据包/get+verify", r2.status_code == 200 and v.get("intact") is True,
              f"verified={v}")

        r3 = self.req("POST", f"/api/v1/evidence/{pkg}/freeze", admin=True)
        check("6-证据包/freeze", r3.status_code == 200 and self.data(r3).get("data", {}).get("frozen") is True,
              f"frozen={self.data(r3).get('data', {}).get('frozen')}")

        r4 = self.req("GET", f"/api/v1/evidence/{pkg}/export", admin=True, params={"fmt": "json"})
        r4b = self.req("GET", f"/api/v1/evidence/{pkg}/export", admin=True, params={"fmt": "text"})
        check("6-证据包/export json+text", r4.status_code == 200 and r4b.status_code == 200,
              f"json={r4.status_code} text={r4b.status_code}")

        for kind in ("96110", "platform", "pibyao"):
            r5 = self.req("GET", f"/api/v1/evidence/{pkg}/report-template", params={"kind": kind})
            t = self.data(r5).get("data", {}).get("text", "")
            ok = r5.status_code == 200 and t and "个人" in t and "138" not in t
            check(f"6-证据包/举报模板[{kind}]合规", ok, f"len={len(t)}")
            self.templates[kind] = t

    def step7_cases(self, det_id: int) -> None:
        # P1-1/P1-2：publish 需 admin + confirm+reason 双确认（先过闸控，敏感载荷再被脱敏校验拦截）
        reason = "这是一条超过二十个字的证据案例发布理由说明文本"
        r = self.req("POST", "/api/v1/cases/publish", admin=True, json_body={
            "det_id": det_id, "redacted_payload": "加我微信 13812345678，稳赚不赔",
            "confirm": True, "reason": reason,
        })
        body = self.data(r)
        check("7-案例库/含敏感拒绝", r.status_code == 422 and body.get("error", {}).get("code") == "sensitive_data",
              f"status={r.status_code} code={body.get('error', {}).get('code')}")

        d = DesensitizeService().regex_redact("加我微信 13812345678，稳赚不赔，导师带单")
        r2 = self.req("POST", "/api/v1/cases/publish", admin=True, json_body={
            "det_id": det_id,
            "redacted_payload": d["redacted"],
            "desensitize_log": d["replaced"],
            "graph_tags": ["fake_investment"],
            "confirm": True,
            "reason": reason,
        })
        d2 = self.data(r2).get("data", {})
        check("7-案例库/脱敏后发布", r2.status_code == 200 and d2.get("id"),
              f"case_id={d2.get('id')} redacted={d2.get('redacted_payload')}")

        r3 = self.req("GET", "/api/v1/cases", params={"tag": "fake_investment"})
        d3 = self.data(r3).get("data", {})
        check("7-案例库/列表查询", r3.status_code == 200 and d3.get("total", 0) >= 1, f"total={d3.get('total')}")

        r4 = self.req("GET", "/api/v1/cases/graph")
        g = self.data(r4).get("data", {})
        check("7-案例库/图谱聚合", r4.status_code == 200 and g.get("total", 0) >= 1
              and any(x.get("name") == "fake_investment" for x in g.get("by_tag", [])),
              f"total={g.get('total')} by_tag={[(x.get('name'), x.get('value')) for x in g.get('by_tag', [])]}")

    def step8_alerts(self) -> None:
        r = self.req("GET", "/api/v1/alerts", admin=True, params={"unread_only": True, "limit": 50})
        items = self.data(r).get("data", [])
        check("8-告警/L3+未读存在", r.status_code == 200 and isinstance(items, list) and len(items) >= 1,
              f"unread={len(items)}")
        if items:
            aid = items[0]["id"]
            r2 = self.req("POST", f"/api/v1/alerts/{aid}/read", admin=True)
            d2 = self.data(r2).get("data", {})
            check("8-告警/read生效", r2.status_code == 200 and d2.get("unread") == 0,
                  f"alert_id={aid} unread={d2.get('unread')}")
        else:
            check("8-告警/read生效", False, "无未读告警可标记")

    def step9_zhihu(self) -> None:
        r = self.req("GET", "/api/v1/zhihu/channels")
        chs = self.data(r).get("data", {}).get("channels", [])
        check("9-知乎/四通道健康结构", r.status_code == 200 and len(chs) == 4
              and all(c.get("channel") in ("ch-a", "ch-b", "ch-c", "ch-d") for c in chs),
              f"channels={[c.get('channel') for c in chs]}")

        text = (f"E2E验收专用{int(time.time())}-{secrets.token_hex(4)}："
                "冒充客服退款诈骗 13800138000 请点击链接领取理赔（内部测试文本）")
        r2 = self.req("POST", "/api/v1/zhihu/import", admin=True,
                      json_body={"text": text, "source": "import", "from_url_name": "e2e_scammer"})
        d2 = self.data(r2).get("data", {})
        check("9-知乎/手动导入→pending", r2.status_code == 200 and d2.get("ingested", 0) >= 1,
              f"ingested={d2.get('ingested')} duplicates={d2.get('duplicates')}")

        # P1-1：POST /scan/inbox 批量消费为写操作，需 admin
        r3 = self.req("POST", "/api/v1/scan/inbox", admin=True)
        d3 = self.data(r3).get("data", {})
        items3 = d3.get("items") or []
        first3 = items3[0] if items3 else {}
        check("9-知乎/scan/inbox批量消费", r3.status_code == 200 and d3.get("processed", 0) >= 1
              and first3.get("verdict") != "normal" and first3.get("rule_score", 0) > 0,
              f"processed={d3.get('processed')} first_verdict={first3.get('verdict')} rule_score={first3.get('rule_score')}")

        # P2-7：GET /scan/inbox 读取对话原文需 admin
        r4 = self.req("GET", "/api/v1/scan/inbox", admin=True)
        items = self.data(r4).get("data", [])
        check("9-知乎/inbox结果列表", r4.status_code == 200 and isinstance(items, list) and len(items) >= 1,
              f"items={len(items)}")

    def step10_events(self) -> None:
        r = self.req("GET", "/api/v1/events", admin=True, params={"page": 1, "page_size": 5})
        d = self.data(r).get("data", {})
        check("10-事件流/列表分页", r.status_code == 200 and d.get("total", 0) >= 1 and len(d.get("items", [])) >= 1,
              f"total={d.get('total')}")
        if d.get("items"):
            eid = d["items"][0]["id"]
            r2 = self.req("GET", f"/api/v1/events/{eid}", admin=True)
            check("10-事件流/单查", r2.status_code == 200 and self.data(r2).get("data", {}).get("id") == eid,
                  f"id={eid}")
        r3 = self.req("GET", "/api/v1/events/999999", admin=True)
        body = self.data(r3)
        check("10-事件流/不存在404封包", r3.status_code == 404 and body.get("ok") is False,
              f"status={r3.status_code}")

    def step11_config(self) -> None:
        r = self.req("GET", "/api/v1/config")
        d = self.data(r).get("data", {})
        check("11-配置/GET合成结构", r.status_code == 200
              and all(k in d for k in ("llm", "degrade", "zhihu", "speech", "trap", "grading", "overrides")),
              f"keys={sorted(d.keys())}")
        check("11-配置/不泄露密钥", "api_key_set" in d.get("llm", {}) and "config_put" not in d, "api_key_set 布尔化")

        # P1-1：PUT /config 需 admin（confirm+reason 闸控保持）
        r2 = self.req("PUT", "/api/v1/config", admin=True, json_body={
            "key": "grading.l3_min", "value": 9.5, "confirm": False, "reason": "这是一条超过二十个字的配置修改理由说明文本",
        })
        b2 = self.data(r2)
        check("11-配置/PUT无confirm拒绝403", r2.status_code == 403 and b2.get("error", {}).get("code") == "confirm_required",
              f"status={r2.status_code} code={b2.get('error', {}).get('code')}")

        r3 = self.req("PUT", "/api/v1/config", admin=True, json_body={
            "key": "grading.l3_min", "value": 9.5, "confirm": True, "reason": "这是一条超过二十个字的配置修改理由说明文本",
        })
        b3 = self.data(r3)
        check("11-配置/PUT带confirm成功", r3.status_code == 200 and b3.get("data", {}).get("key") == "grading.l3_min"
              and b3.get("data", {}).get("audit", {}).get("gate_log_id", 0) >= 1,
              f"gate_log_id={b3.get('data', {}).get('audit', {}).get('gate_log_id')}")

        r4 = self.req("GET", "/api/v1/config")
        ov = self.data(r4).get("data", {}).get("overrides", {})
        check("11-配置/overrides生效", r4.status_code == 200 and ov.get("grading.l3_min") == 9.5,
              f"overrides={ov}")

    def step12_webui(self) -> None:
        r = self.req("GET", "/ui/")
        check("12-WebUI//ui/返回SPA", r.status_code == 200 and "text/html" in r.headers.get("content-type", ""),
              f"status={r.status_code}")
        r2 = self.req("GET", "/")
        check("12-WebUI/根路径重定向/ui/", r2.status_code in (307, 308) and "/ui/" in r2.headers.get("location", ""),
              f"status={r2.status_code} location={r2.headers.get('location')}")
        r3 = self.req("GET", "/ui/some/deep/route")
        check("12-WebUI/SPA fallback", r3.status_code == 200 and "text/html" in r3.headers.get("content-type", ""),
              f"status={r3.status_code}")
        r4 = self.req("GET", "/ui/assets/not-exist-xyz.js")
        check("12-WebUI/资源404不吞", r4.status_code == 404, f"status={r4.status_code}")

    def step13_mcp(self) -> None:
        spec = importlib.util.spec_from_file_location("af_mcp_server_e2e", ROOT / "mcp" / "server.py")
        mod = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(mod)

        async def go():
            proxy = mod.MasterProxy(self.base, self.key)
            srv = mod.build_mcp_server(proxy=proxy)
            try:
                out = {}
                r = await srv.call_tool("af_health", {})
                out["af_health"] = json.loads(r.content[0].text)
                r = await srv.call_tool("af_scan_text", {"text": "杀猪盘带你投资稳赚不赔"})
                out["af_scan_text"] = json.loads(r.content[0].text)
                r = await srv.call_tool("af_list_traps", {})
                out["af_list_traps"] = json.loads(r.content[0].text)
                r = await srv.call_tool("af_list_events", {"page": 1, "page_size": 3})
                out["af_list_events"] = json.loads(r.content[0].text)
                return out
            finally:
                await proxy.aclose()

        out = asyncio.run(go())
        check("13-MCP/af_health", out["af_health"].get("status") == "ok", f"{out['af_health']}")
        s = out["af_scan_text"]
        check("13-MCP/af_scan_text", s.get("verdict") != "normal" and s.get("detection_id"),
              f"verdict={s.get('verdict')} grade={s.get('grade')}")
        t = out["af_list_traps"]
        items = t.get("traps") or t.get("items") or []
        check("13-MCP/af_list_traps", isinstance(items, list), f"traps={len(items)}")
        e = out["af_list_events"]
        check("13-MCP/af_list_events", isinstance(e.get("items"), list) and e.get("total", 0) >= 1,
              f"total={e.get('total')}")

    def step14_chain(self) -> dict:
        """全链路串联：导入→批量扫描→分级→告警→证据→冻结→模板→脱敏发布→图谱→事件。"""
        text = (f"E2E全链路串联{int(time.time())}-{secrets.token_hex(4)}："
                "杀猪盘导师带你理财稳赚不赔，加V信领取名额（内部测试）")
        r1 = self.req("POST", "/api/v1/zhihu/import", admin=True, json_body={"text": text, "source": "import"})
        n1 = self.data(r1).get("data", {}).get("ingested", 0)
        r2 = self.req("POST", "/api/v1/scan/inbox", admin=True)
        d2 = self.data(r2).get("data", {})
        first = (d2.get("items") or [{}])[0]
        det_id = first.get("detection_id")
        grade = first.get("grade")
        verdict = first.get("verdict")

        r3 = self.req("GET", f"/api/v1/grading/explain/{det_id}") if det_id else None
        chain_len = len((self.data(r3).get("data", {}).get("grade_reason") or [])) if r3 and r3.status_code == 200 else 0

        r4 = self.req("GET", "/api/v1/alerts", admin=True, params={"unread_only": True, "limit": 5})
        unread = len(self.data(r4).get("data", []))

        r5 = self.req("POST", "/api/v1/evidence/build", admin=True,
                      json_body={"det_ids": [det_id]} if det_id else {"det_ids": [1]})
        pkg = self.data(r5).get("data", {}).get("pkg_id") or self.data(r5).get("data", {}).get("id")
        if pkg:
            self.req("POST", f"/api/v1/evidence/{pkg}/freeze", admin=True)
        r6 = self.req("GET", f"/api/v1/evidence/{pkg}/report-template", params={"kind": "96110"}) if pkg else None
        tmpl_ok = bool(r6 and r6.status_code == 200 and "96110" in self.data(r6).get("data", {}).get("text", ""))

        dd = DesensitizeService().regex_redact(text)
        r7 = self.req("POST", "/api/v1/cases/publish", admin=True, json_body={
            "det_id": det_id, "redacted_payload": dd["redacted"],
            "desensitize_log": dd["replaced"], "graph_tags": ["fake_investment", "pig_butchering"],
            "confirm": True, "reason": "这是一条超过二十个字的证据案例发布理由说明文本",
        }) if det_id else None
        case_ok = bool(r7 and r7.status_code == 200)

        r8 = self.req("GET", "/api/v1/cases/graph")
        g = self.data(r8).get("data", {})
        r9 = self.req("GET", "/api/v1/events", admin=True, params={"page_size": 5})
        ev_total = self.data(r9).get("data", {}).get("total", 0)

        summary = {
            "import_written": n1, "processed": d2.get("processed"),
            "detection_id": det_id, "verdict": verdict, "grade": grade,
            "explain_chain_len": chain_len, "unread_alerts": unread,
            "evidence_pkg": pkg, "report_template_ok": tmpl_ok,
            "case_published": case_ok, "graph_total": g.get("total"), "events_total": ev_total,
        }
        all_ok = (n1 >= 1 and d2.get("processed", 0) >= 1 and det_id and grade in ("L1", "L2", "L3", "L4", "L5")
                  and chain_len >= 1 and pkg and tmpl_ok and case_ok and g.get("total", 0) >= 1)
        check("14-全链路串联", all_ok, json.dumps(summary, ensure_ascii=False))
        return summary

    def compliance(self) -> None:
        """合规红线逐项检查（D3/D7）。"""
        t96110 = self.templates.get("96110", "")
        tplatform = self.templates.get("platform", "")
        tpibyao = self.templates.get("pibyao", "")
        check("合规-举报模板个人署名不冒充官方",
              all("个人" in t for t in (t96110, tplatform, tpibyao) if t) and "96110" in t96110,
              f"96110含个人={ '个人' in t96110} platform含个人={'个人' in tplatform} pibyao含个人={'个人' in tpibyao}")
        check("合规-模板不含官方受理措辞",
              "已受理" not in t96110 and "已冻结" not in t96110 and "我们已处理" not in t96110,
              "模板仅引导官方通道")
        # HITL：蜜饵草稿含 hitl_hint（step4 已验）；案例发布强制脱敏（step7 已验）；cookie 注入为敏感操作（admin）
        r = self.req("POST", "/api/v1/zhihu/channels/ch_a/login", json_body={
            "cookie": "z_c0=abc; d_c0=def; garbage"})
        body = self.data(r)
        # 需 admin：无 key 应 401；有 key 时垃圾 cookie 应 422 invalid_cookie —— 二者都证明系统不代登录/不裸存
        check("合规-cookie注入受控", r.status_code in (401, 422) and body.get("ok") is False,
              f"status={r.status_code} code={body.get('error', {}).get('code')}")


def main() -> int:
    ap = argparse.ArgumentParser(prog="e2e_acceptance.py", description="金丝雀蜜罐 P10 端到端验收")
    ap.add_argument("--base", default="http://127.0.0.1:9200")
    ap.add_argument("--api-key", default=None)
    ap.add_argument("--out", default=str(ROOT / "scripts" / "_e2e_results.json"))
    args = ap.parse_args()

    key = args.api_key
    if not key:
        kf = ROOT / "data" / "bootstrap_admin_key.txt"
        if kf.exists():
            key = kf.read_text(encoding="utf-8").strip()
    if not key:
        print("未提供 --api-key 且 data/bootstrap_admin_key.txt 不存在")
        return 2

    e2e = E2E(args.base, key)
    e2e.templates = {}
    print(f"== P10 端到端验收  base={args.base}  key_prefix={key[:9]} ==")

    e2e.step0_health()
    e2e.step1_auth()
    det_ids = e2e.step2_scan()
    det_id0 = det_ids[0] if det_ids else None
    if det_id0:
        e2e.step3_grading(det_id0)
    e2e.step4_traps()
    e2e.step5_accounts()
    if det_ids:
        e2e.step6_evidence(det_ids)
    if det_id0:
        e2e.step7_cases(det_id0)
    e2e.step8_alerts()
    e2e.step9_zhihu()
    e2e.step10_events()
    e2e.step11_config()
    e2e.step12_webui()
    e2e.step13_mcp()
    chain = e2e.step14_chain()
    e2e.compliance()

    passed = sum(1 for x in RESULTS if x["passed"])
    failed = sum(1 for x in RESULTS if not x["passed"])
    print(f"\n== 汇总: {passed} PASS / {failed} FAIL / 共 {len(RESULTS)} 项 ==")

    payload = {
        "base": args.base,
        "run_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "passed": passed, "failed": failed, "total": len(RESULTS),
        "chain": chain,
        "items": RESULTS,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"结果已写入 {out}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())