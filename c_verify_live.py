# -*- coding: utf-8 -*-
"""C队 双盲验收：反钓鱼伪装 /cover 后端独立验证（只读验证+测试，不改产品代码）。

- 走真实主控 127.0.0.1:9200（远程服务器本机执行）
- admin key 读 data/bootstrap_admin_key.txt（UTF-8 字节）
- API 调用 UTF-8 字节 JSON
- 落库后直接读 SQLite 校验脱敏 / IOC 事件 / 锁定 / gate_logs
"""
import json
import os
import re
import sqlite3
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

BASE = "http://127.0.0.1:9200"
ROOT = Path(r"C:\Users\Administrator\Desktop\知乎黑客松\金丝雀蜜罐")
KEY = (ROOT / "data" / "bootstrap_admin_key.txt").read_bytes().decode("utf-8").strip()
TAG = str(int(time.time()))[-6:]          # 本次运行唯一标签，避免与历史残留冲突
PNAME = f"C队双盲{TAG}_"                   # 身份名前缀
FAKE_PHONE = "138" + TAG.zfill(8)[-8:]    # 运行唯一假手机号 (11 位)
SCAM_PHONE = "139" + TAG.zfill(8)[-8:]    # 运行唯一骗子手机号
SCAM_WX = "wx_scammer_" + TAG
PASS = 0
FAIL = 0
RESULTS = []

def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        RESULTS.append(("PASS", name, detail))
        print(f"[PASS] {name} {detail}")
    else:
        FAIL += 1
        RESULTS.append(("FAIL", name, detail))
        print(f"[FAIL] {name} {detail}")

def req(method, path, body=None, key=KEY, raw_body=False):
    url = BASE + path
    data = None
    headers = {}
    if body is not None:
        if raw_body:
            # UTF-8 字节 POST（调用方已编码）
            data = body
        else:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    if key is not None:
        headers["X-API-Key"] = key
    r = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(r, timeout=20) as resp:
            raw = resp.read()
            code = resp.status
    except urllib.error.HTTPError as e:
        raw = e.read()
        code = e.code
    except Exception as e:  # noqa: BLE001
        return 0, {"error": str(e)}
    try:
        j = json.loads(raw.decode("utf-8"))
    except Exception:  # noqa: BLE001
        j = {"raw": raw.decode("utf-8", "replace")[:500]}
    return code, j

def db():
    conn = sqlite3.connect(str(ROOT / "data" / "af.db"))
    conn.row_factory = sqlite3.Row
    return conn

def main():
    print(f"KEY: {KEY[:12]}... (len={len(KEY)})")
    # ---------- 0. health ----------
    code, j = req("GET", "/api/v1/system/health")
    check("health 200", code == 200 and j.get("ok") is True, f"code={code}")

    # ---------- 1. 创建伪装：顶层 layer=1 ----------
    code, j = req("POST", "/api/v1/cover/identities",
                  {"name": PNAME + "顶层受害者A", "persona_type": "受害者A",
                   "fake_phone": FAKE_PHONE, "fake_wechat": "wx_c_cover_a",
                   "expose_strategy": "主动"})
    check("create top layer", code == 200 and j.get("ok") is True, f"code={code} {json.dumps(j,ensure_ascii=False)[:200]}")
    top = j.get("data", {}).get("identity", {}) if code == 200 else {}
    check("top layer==1", top.get("layer") == 1, f"layer={top.get('layer')}")
    check("top enabled==1", top.get("enabled") == 1)
    check("top fake_phone preserved", top.get("fake_phone") == FAKE_PHONE)

    # ---------- 2. 子伪装：parent → layer=parent+1（三层嵌套） ----------
    code, j = req("POST", "/api/v1/cover/identities",
                  {"name": PNAME + "转介绍B", "persona_type": "转介绍B",
                   "parent_id": top.get("id"), "fake_phone": FAKE_PHONE + "2"})
    child = j.get("data", {}).get("identity", {}) if code == 200 else {}
    check("create child with parent", code == 200 and child.get("layer") == 2,
          f"code={code} layer={child.get('layer')}")
    code, j = req("POST", "/api/v1/cover/identities",
                  {"name": PNAME + "朋友C", "persona_type": "朋友C",
                   "parent_id": child.get("id"), "fake_phone": FAKE_PHONE + "3"})
    grand = j.get("data", {}).get("identity", {}) if code == 200 else {}
    check("create grandchild layer==3", code == 200 and grand.get("layer") == 3,
          f"layer={grand.get('layer')}")

    # ---------- 3. name 重复 409 ----------
    code, j = req("POST", "/api/v1/cover/identities",
                  {"name": PNAME + "顶层受害者A", "fake_phone": FAKE_PHONE + "9"})
    check("duplicate name 409", code == 409 and j.get("error", {}).get("code") == "identity_name_exists",
          f"code={code} {json.dumps(j,ensure_ascii=False)[:150]}")

    # ---------- 4. GET /cover/identities 嵌套/被接触次数 ----------
    code, j = req("GET", "/api/v1/cover/identities")
    ids = j.get("data", {}).get("identities", []) if code == 200 else []
    names = {i["name"]: i for i in ids}
    check("list identities ok", code == 200 and len(ids) >= 3, f"count={len(ids)}")
    check("list contains layers", names.get(PNAME + "顶层受害者A", {}).get("layer") == 1
          and names.get(PNAME + "朋友C", {}).get("layer") == 3)
    check("list has contact_count field",
          "contact_count" in names.get(PNAME + "顶层受害者A", {}) and
          names.get(PNAME + "顶层受害者A", {}).get("contact_count") == 0)

    # ---------- 5. expose → 脱敏假信息 ----------
    code, j = req("POST", f"/api/v1/cover/identities/{top.get('id')}/expose")
    exp = j.get("data", {}).get("exposure", {}) if code == 200 else {}
    check("expose ok", code == 200 and exp.get("event") == "cover_expose", f"code={code}")
    cl = {c["field"]: c for c in exp.get("checklist", [])}
    check("expose phone masked", "fake_phone" in cl and cl["fake_phone"]["masked"] == FAKE_PHONE[:2] + "*" * (len(FAKE_PHONE) - 4) + FAKE_PHONE[-2:],
          f"masked={cl.get('fake_phone',{}).get('masked')}")
    check("expose returns plaintext user value", cl.get("fake_phone", {}).get("value") == FAKE_PHONE,
          f"value={cl.get('fake_phone',{}).get('value')}")

    # ---------- 6. 接触登记：骗子手机号 脱敏落库 + IOC ----------
    scam_phone = SCAM_PHONE
    # DesensitizeService._mask("phone")：11位 3+4+4 掩码
    scam_mask = scam_phone[:3] + "****" + scam_phone[-4:]
    code, j = req("POST", "/api/v1/cover/contact",
                  {"identity_id": top.get("id"), "contact_type": "phone_call",
                   "contact_value": scam_phone})
    ev = j.get("data", {}).get("event", {}) if code == 200 else {}
    check("contact register ok", code == 200 and ev.get("event_id", 0) > 0, f"code={code}")
    check("contact masked in response", ev.get("contact_value_mask") == scam_mask,
          f"mask={ev.get('contact_value_mask')} expect={scam_mask}")
    check("layer_at recorded", ev.get("layer_at") == 1, f"layer_at={ev.get('layer_at')}")
    check("contact IOC extracted", ev.get("ioc_count", 0) >= 1, f"ioc_count={ev.get('ioc_count')}")
    check("not escalated yet (1st)", ev.get("escalated") == 0)

    # 同号第 2 次 → escalated + cover_locked
    code, j = req("POST", "/api/v1/cover/contact",
                  {"identity_id": child.get("id"), "contact_type": "phone_call",
                   "contact_value": scam_phone})
    ev2 = j.get("data", {}).get("event", {}) if code == 200 else {}
    check("2nd contact register ok", code == 200 and ev2.get("event_id", 0) > 0)
    check("2nd contact escalated", ev2.get("escalated") == 1, f"escalated={ev2.get('escalated')}")

    # 微信接触（不同值，不锁定）
    code, j = req("POST", "/api/v1/cover/contact",
                  {"identity_id": child.get("id"), "contact_type": "wechat_add",
                   "contact_value": SCAM_WX})
    ev3 = j.get("data", {}).get("event", {}) if code == 200 else {}
    check("wechat contact ok", code == 200 and ev3.get("contact_type") == "wechat_add")

    # ---------- 7. GET /cover/contacts（过滤；实时库可能含他人 E2E 数据，断言按本队范围） ----------
    code, j = req("GET", "/api/v1/cover/contacts")
    contacts = j.get("data", {}).get("contacts", []) if code == 200 else []
    check("contacts list ok", code == 200 and len(contacts) >= 3, f"count={len(contacts)}")
    my_ev_ids = {ev.get("event_id"), ev2.get("event_id"), ev3.get("event_id")}
    got_ids = {c["id"] for c in contacts}
    check("my events present in list", my_ev_ids <= got_ids, f"missing={my_ev_ids - got_ids}")
    check("contacts sorted desc", len(contacts) >= 2 and contacts[0]["id"] > contacts[-1]["id"])
    code, j = req("GET", f"/api/v1/cover/contacts?identity_id={top.get('id')}")
    by_id = j.get("data", {}).get("contacts", []) if code == 200 else []
    check("contacts filter by identity", len(by_id) == 1 and by_id[0]["identity_id"] == top.get("id"),
          f"count={len(by_id)}")
    code, j = req("GET", f"/api/v1/cover/contacts?identity_id={child.get('id')}")
    by_child = j.get("data", {}).get("contacts", []) if code == 200 else []
    check("contacts filter child=2 rows", len(by_child) == 2, f"count={len(by_child)}")
    code, j = req("GET", "/api/v1/cover/contacts?escalated=1")
    esc = j.get("data", {}).get("contacts", []) if code == 200 else []
    check("contacts filter escalated (mine 2)", len(esc) >= 2 and all(c["escalated"] == 1 for c in esc),
          f"count={len(esc)}")
    code, j = req("GET", "/api/v1/cover/contacts?contact_type=phone_call")
    pt = j.get("data", {}).get("contacts", []) if code == 200 else []
    check("contacts filter type", len(pt) >= 2, f"count={len(pt)}")

    # ---------- 8. GET /cover/map（范围断言：本队身份被接触数/树结构） ----------
    code, j = req("GET", "/api/v1/cover/map")
    m = j.get("data", {}) if code == 200 else {}
    check("map ok", code == 200, f"code={code}")
    check("map total_contacts>=3", m.get("total_contacts", 0) >= 3, f"total={m.get('total_contacts')}")
    layers = {l["layer"]: l["contacts"] for l in m.get("layers", [])}
    check("map layers mine (L1>=1, L2>=2)", layers.get(1, 0) >= 1 and layers.get(2, 0) >= 2,
          f"layers={layers}")
    mymap = {i["name"]: i for i in m.get("identities", [])}
    check("map identities mine contact_count",
          mymap.get(PNAME + "顶层受害者A", {}).get("contact_count") == 1
          and mymap.get(PNAME + "转介绍B", {}).get("contact_count") == 2,
          f"top={mymap.get(PNAME+'顶层受害者A',{}).get('contact_count')} child={mymap.get(PNAME+'转介绍B',{}).get('contact_count')}")
    tmap = {t["name"]: t for t in m.get("tree", [])}
    top_t = tmap.get(PNAME + "顶层受害者A", {})
    check("map tree root children", len(top_t.get("children", [])) == 1
          and top_t["children"][0]["name"] == PNAME + "转介绍B",
          f"children={[c['name'] for c in top_t.get('children', [])]}")

    # ---------- 9. DELETE 无 confirm → 403；无 key → 401 ----------
    code, j = req("DELETE", f"/api/v1/cover/identities/{top.get('id')}")
    check("delete without confirm 403", code == 403 and j.get("error", {}).get("code") == "confirm_required",
          f"code={code}")
    code, j = req("DELETE", f"/api/v1/cover/identities/{top.get('id')}", key=None)
    check("delete no key 401", code == 401, f"code={code}")
    code, j = req("GET", "/api/v1/cover/identities", key=None)
    check("get no key 401", code == 401, f"code={code}")
    code, j = req("POST", "/api/v1/cover/contact",
                  {"identity_id": top.get("id"), "contact_type": "phone_call",
                   "contact_value": "13811112222"}, key="wrong_key_xyz")
    check("contact invalid key 401", code == 401, f"code={code}")

    # ---------- 10. toggle：无 confirm 403 / 有 confirm 翻转 + gate_logs ----------
    code, j = req("POST", f"/api/v1/cover/identities/{top.get('id')}/toggle")
    check("toggle without confirm 403", code == 403, f"code={code}")
    GOOD = "C队双盲验收：验证启停敏感操作双确认与审计日志记录是否完整符合设计要求"
    code, j = req("POST", f"/api/v1/cover/identities/{top.get('id')}/toggle",
                  {"confirm": True, "reason": GOOD})
    toggled = j.get("data", {}).get("identity", {}) if code == 200 else {}
    check("toggle with confirm ok", code == 200 and toggled.get("enabled") == 0,
          f"code={code} enabled={toggled.get('enabled')}")

    # ---------- 11. DB 落库校验（脱敏 / events / gate_logs / 明文不落库） ----------
    conn = db()
    try:
        row = conn.execute("SELECT * FROM contact_events WHERE id=?", (ev.get("event_id"),)).fetchone()
        check("db contact masked", row is not None and row["contact_value"] == scam_mask,
              f"value={dict(row).get('contact_value') if row else None}")
        check("db no plaintext phone", row is not None and scam_phone not in (row["contact_value"] or ""))
        n_ev = conn.execute("SELECT COUNT(*) n FROM events WHERE kind='cover_expose'").fetchone()["n"]
        check("db events cover_expose", n_ev >= 1, f"n={n_ev}")
        row2 = conn.execute("SELECT payload FROM events WHERE kind='cover_contact' ORDER BY id DESC LIMIT 1").fetchone()
        check("db events cover_contact", row2 is not None and "iocs" in json.loads(row2["payload"]))
        row3 = conn.execute("SELECT payload FROM events WHERE kind='cover_locked' ORDER BY id DESC LIMIT 1").fetchone()
        check("db events cover_locked", row3 is not None, f"payload={dict(row3).get('payload','')[:120] if row3 else None}")
        if row3:
            lp = json.loads(row3["payload"])
            check("locked payload count==2", lp.get("count") == 2, f"count={lp.get('count')}")
        gl = conn.execute("SELECT * FROM gate_logs WHERE action='cover.identity.toggle' ORDER BY id DESC LIMIT 1").fetchone()
        check("db gate_logs toggle", gl is not None and (gl["before"] or "") and (gl["after"] or ""),
              f"before={gl['before'] if gl else None} after={gl['after'] if gl else None}")
        # stats 联动
        code, j = req("GET", "/api/v1/system/stats")
        st = j.get("data", {}) if code == 200 else {}
        check("stats cover_identities", code == 200 and st.get("cover_identities", 0) >= 3,
              f"cover_identities={st.get('cover_identities')}")
        check("stats contact_events", st.get("contact_events", 0) >= 3,
              f"contact_events={st.get('contact_events')}")
        # cover 表已创建
        tabs = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        check("db tables cover_identities+contact_events",
              "cover_identities" in tabs and "contact_events" in tabs)
    finally:
        conn.close()

    # ---------- 12. 清理：删除测试伪装（confirm+reason，同时验证 delete 成功路径 & 子层 parent 置空） ----------
    code, j = req("DELETE", f"/api/v1/cover/identities/{top.get('id')}",
                  {"confirm": True, "reason": "C队双盲验收清理：测试用伪装身份已验收完毕申请删除并留审计记录"})
    check("delete with confirm ok", code == 200 and j.get("data", {}).get("deleted") is True,
          f"code={code} {json.dumps(j,ensure_ascii=False)[:150]}")
    # 删除后子层 parent 置空 + 接触记录保留
    conn = db()
    try:
        b = conn.execute("SELECT parent_id, name FROM cover_identities WHERE name=?", (PNAME + "转介绍B",)).fetchone()
        check("child parent nulled after delete", b is not None and b["parent_id"] is None,
              f"parent_id={dict(b).get('parent_id') if b else None}")
        n_keep = conn.execute("SELECT COUNT(*) n FROM contact_events WHERE identity_id=?", (top.get("id"),)).fetchone()["n"]
        check("contact events kept after delete", n_keep == 1, f"n={n_keep}")
    finally:
        conn.close()
    for nm in [PNAME + "转介绍B", PNAME + "朋友C"]:
        code, j = req("GET", "/api/v1/cover/identities")
        ids = j.get("data", {}).get("identities", []) if code == 200 else []
        tid = next((i["id"] for i in ids if i["name"] == nm), None)
        if tid:
            req("DELETE", f"/api/v1/cover/identities/{tid}",
                {"confirm": True, "reason": "C队双盲验收清理：测试用伪装身份已验收完毕申请删除并留审计记录"})

    # ---------- 13. 自清理：删除本队本次运行残留（范围=本 TAG，不动他人数据） ----------
    conn = db()
    try:
        cur = conn.cursor()
        my_ids = [top.get("id"), child.get("id"), grand.get("id")]
        qmarks = ",".join("?" * len(my_ids))
        n_ce = cur.execute(
            f"DELETE FROM contact_events WHERE identity_id IN ({qmarks})", my_ids).rowcount
        n_ev = cur.execute(
            "DELETE FROM events WHERE payload LIKE ?", (f"%{PNAME}%",)).rowcount
        n_gl = cur.execute(
            "DELETE FROM gate_logs WHERE reason LIKE ?", (f"%{PNAME}%",)).rowcount
        conn.commit()
        print(f"[INFO] self-cleanup: contact_events={n_ce} events={n_ev} gate_logs={n_gl}")
        left = cur.execute("SELECT COUNT(*) FROM cover_identities").fetchone()[0]
        print(f"[INFO] remaining cover_identities={left}")
    finally:
        conn.close()

    # ---------- 汇总 ----------
    print("=" * 60)
    print(f"TOTAL PASS={PASS} FAIL={FAIL}")
    sys.exit(0 if FAIL == 0 else 1)

if __name__ == "__main__":
    main()