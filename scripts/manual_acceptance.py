r"""金丝雀蜜罐 全功能手动验收脚本（真实调用主控 REST API + MCP）。

覆盖：认证 / 话术检测 / 分级 / 蜜饵 / 账号 / 证据 / 案例 / 告警 / 知乎通道 / 事件 /
配置 / 供应链反查 / 检测详情 / WebUI 托管。每项输出 [PASS]/[FAIL]。
运行：.venv\Scripts\python.exe scripts\manual_acceptance.py
"""
import json
import time

import httpx

BASE = "http://127.0.0.1:9200"
API = BASE + "/api/v1"
KEY = open("data/bootstrap_admin_key.txt", encoding="utf-8").read().strip()
H = {"X-API-Key": KEY}

results = []


def check(name, cond, detail=""):
    ok = bool(cond)
    results.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  — {detail}" if detail and not ok else ""))


def req(method, path, **kw):
    return httpx.request(method, API + path, headers=H, timeout=20, **kw)


# ============ 1. 认证 ============
r = httpx.post(API + "/auth/bootstrap-info", timeout=10)
check("认证·bootstrap-info 公开可访问", r.status_code == 200 and r.json().get("ok"))
r = httpx.post(API + "/auth/local-login", timeout=10)
check("认证·本地一键登录(local-login) 返回 key", r.status_code == 200 and r.json()["ok"] and r.json()["data"].get("key"))
r = httpx.get(API + "/auth/current-key", timeout=10)
check("认证·current-key 回环返回 key", r.status_code == 200 and r.json()["ok"])
r = httpx.get(API + "/system/stats", timeout=10)  # 无 key 应 401
check("认证·无 key 访问受保护端点 401", r.status_code == 401)

# ============ 2. 话术检测 ============
fraud = "稳赚不赔的内幕消息，投资理财高回报，加我微信带你赚钱，先垫付1000试试水"
r = httpx.post(API + "/scan/text", json={"text": fraud, "source": "zhihu"}, headers=H, timeout=20)
d = r.json().get("data", {})
det_id = d.get("detection_id")
check("话术·诈骗文本命中(verdict≠normal)", r.status_code == 200 and d.get("verdict") != "normal", f"verdict={d.get('verdict')} grade={d.get('grade')}")
r = httpx.post(API + "/scan/text", json={"text": "今天天气不错，我去图书馆看书学习了"}, headers=H, timeout=20)
check("话术·正常语料 0 误报", r.json()["data"].get("verdict") == "normal")

# ============ 3. 分级 ============
r = httpx.get(API + "/grading/levels", headers=H, timeout=10)
check("分级·levels 返回 L1-L5", r.status_code == 200 and len(r.json().get("data", [])) >= 5)
if det_id:
    r = httpx.get(API + f"/grading/explain/{det_id}", headers=H, timeout=10)
    check("分级·explain 返回证据链", r.status_code == 200 and r.json().get("ok"))

# ============ 4. 蜜饵全生命周期 ============
r = httpx.post(API + "/traps/generate-draft", json={}, headers=H, timeout=20)
trap = r.json().get("data", {})
trap_id = trap.get("id")
check("蜜饵·生成草稿(HITL 不发布)", r.status_code == 200 and trap.get("status") == "draft", f"status={trap.get('status')}")
if trap_id:
    r = httpx.post(API + f"/traps/{trap_id}/deploy", headers=H, timeout=10)
    check("蜜饵·deploy 显式发布", r.status_code == 200)
    r = httpx.post(API + f"/traps/{trap_id}/monitor", headers=H, timeout=10)
    check("蜜饵·monitor 进入监控", r.status_code == 200)
    r = httpx.post(API + f"/traps/{trap_id}/check-hit", json={"text": trap.get("bait_text", "")}, headers=H, timeout=10)
    check("蜜饵·check-hit 踩饵命中即退役", r.status_code == 200 and r.json().get("data", {}).get("hit"))

# ============ 5. 账号速查 ============
r = httpx.post(API + "/accounts/check", json={"url_name": "acceptance_test_account"}, headers=H, timeout=10)
check("账号·check 返回风险等级", r.status_code == 200 and r.json().get("ok"))

# ============ 6. 证据包 ============
det_ids = [det_id] if det_id else []
r = httpx.post(API + "/evidence/build", json={"det_ids": det_ids}, headers=H, timeout=10)
pkg = r.json().get("data", {})
pkg_id = pkg.get("id")
check("证据·build 组装", r.status_code == 200 and r.json().get("ok"))
if pkg_id:
    r = httpx.post(API + f"/evidence/{pkg_id}/freeze", headers=H, timeout=10)
    check("证据·freeze 冻结", r.status_code == 200 and r.json().get("data", {}).get("frozen"))
    r = httpx.get(API + f"/evidence/{pkg_id}/export", params={"fmt": "json"}, headers=H, timeout=10)
    check("证据·export 导出", r.status_code == 200)
    r = httpx.get(API + f"/evidence/{pkg_id}/report-template", params={"kind": "96110"}, headers=H, timeout=10)
    check("证据·举报模板(96110)", r.status_code == 200)

# ============ 7. 案例库 ============
r = httpx.post(API + "/cases/publish", json={"det_id": det_id, "redacted_payload": "含手机号13812345678的载荷", "confirm": True, "reason": "手动验收测试发布案例，验证脱敏强制"+ "x"*10}, headers=H, timeout=10)
check("案例·含敏感载荷 422 拒绝", r.status_code == 422)
r = httpx.post(API + "/cases/publish", json={"det_id": det_id, "redacted_payload": "脱敏后的案例载荷内容", "confirm": True, "reason": "手动验收测试发布脱敏案例验证流程"+ "x"*10}, headers=H, timeout=10)
check("案例·脱敏后发布成功", r.status_code == 200 and r.json().get("ok"))
r = httpx.get(API + "/cases/graph", headers=H, timeout=10)
check("案例·graph 图谱数据", r.status_code == 200 and r.json().get("ok"))

# ============ 8. 告警 ============
r = httpx.get(API + "/alerts", params={"limit": 10}, headers=H, timeout=10)
alerts = r.json().get("data", [])
check("告警·list 返回", r.status_code == 200)
if alerts and alerts[0].get("unread"):
    r = httpx.post(API + f"/alerts/{alerts[0]['id']}/read", headers=H, timeout=10)
    check("告警·mark read", r.status_code == 200)

# ============ 9. 知乎通道 ============
r = httpx.get(API + "/zhihu/channels", headers=H, timeout=10)
check("知乎·channels 四通道", r.status_code == 200 and len(r.json().get("data", {}).get("channels", [])) >= 4)

# ============ 10. 事件流 ============
r = httpx.get(API + "/events", params={"page": 1, "page_size": 10}, headers=H, timeout=10)
check("事件·list 分页", r.status_code == 200 and r.json().get("data", {}).get("total", 0) > 0)

# ============ 11. 配置 ============
r = httpx.get(API + "/config", headers=H, timeout=10)
check("配置·GET 合成(不泄密钥)", r.status_code == 200 and r.json().get("ok"))
r = httpx.put(API + "/config", json={"key": "llm.daily_budget", "value": 0, "confirm": False, "reason": "x"}, headers=H, timeout=10)
check("配置·PUT 无 confirm 403", r.status_code == 403)

# ============ 12. 供应链反查 ============
r = httpx.post(API + "/supply-chain/query", json={"seed": "https://www.example.com"}, headers=H, timeout=20)
d = r.json().get("data", {})
check("供应链·query 本地起链", r.status_code == 200 and d.get("domain") == "www.example.com" and len(d.get("graph", {}).get("nodes", [])) > 0)
r = httpx.get(API + "/supply-chain/results", headers=H, timeout=10)
check("供应链·results 列出报告", r.status_code == 200 and len(r.json().get("data", [])) >= 1)
r = httpx.get(API + "/supply-chain/results/hgrz.pro", headers=H, timeout=10)
check("供应链·读取 workflow 报告", r.status_code == 200 and len(r.json().get("data", {}).get("graph", {}).get("nodes", [])) >= 15)

# ============ 13. 检测详情聚合 ============
if det_id:
    r = httpx.get(API + f"/detections/{det_id}/full", headers=H, timeout=10)
    fd = r.json().get("data", {})
    check("详情·full 四要素聚合", r.status_code == 200 and all(k in fd for k in ("detection", "speech_hits", "platform_link", "events")))
    r = httpx.get(API + f"/detections/{det_id}/platform-link", headers=H, timeout=10)
    check("详情·platform-link", r.status_code == 200)

# ============ 14. WebUI 托管 ============
for path in ("/ui/", "/ui/console", "/ui/supply-chain", "/ui/dashboard"):
    r = httpx.get(BASE + path, timeout=10)
    check(f"WebUI·{path}", r.status_code == 200)

# ============ 汇总 ============
passed = sum(1 for _, ok, _ in results if ok)
total = len(results)
print(f"\n{'='*60}\n总计: {passed}/{total} 通过")
failed = [(n, d) for n, ok, d in results if not ok]
if failed:
    print("失败项:")
    for n, d in failed:
        print(f"  - {n}: {d}")
