"""反钓鱼伪装服务（CoverIdentityService）—— 多层伪装身份 + 接触事件 + 锁定范围。

《蜜罐反钓鱼伪装》设计：用户配置多层伪装身份（受害者A→转介绍B→朋友C…），
主动暴露假联系信息诱导骗子接触；接触事件落库（脱敏，D7）+ IOC 提取 +
同号多次接触锁定范围（escalated）+ 蜜饵/检测关联反哺供应链图谱。

合规（设计 §六）：假信息仅用于防御验证（HITL：暴露/回复由用户手动触发）；
真实骗子号码脱敏 + IOC 掩码，明文不落库；暴露/接触事件可审计
（events；toggle/delete 等敏感操作由 API 层 gate_logs 审计）。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from ..utils import ApiError
from .desensitize import DesensitizeService
from .soc_lib import SocLibService

# persona_type 合法枚举（设计 §一）
PERSONA_TYPES: frozenset[str] = frozenset({"受害者A", "转介绍B", "朋友C"})
# contact_type 合法枚举（设计 §三）
CONTACT_TYPES: frozenset[str] = frozenset({"phone_call", "wechat_add", "qq_add", "email"})
# expose_strategy 合法枚举（设计 §一）
EXPOSE_STRATEGIES: frozenset[str] = frozenset({"主动", "被动", "埋饵"})
# IOC 顺序（与 soc_lib._IOC_ORDER 对齐）
_IOC_ORDER = ("phones", "emails", "qq", "wechat", "bank_cards")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _mask_fake(value: str | None) -> str:
    """假信息脱敏展示（D7）：保首2尾2；空值返回 '-'。"""
    v = (value or "").strip()
    if not v:
        return "-"
    if len(v) <= 4:
        return v[0] + "*" * (len(v) - 1)
    return v[:2] + "*" * (len(v) - 4) + v[-2:]


def _mask_ioc(ioc_type: str, value: str) -> str:
    """IOC 掩码（与社工库掩码口径一致）：手机/卡 首2尾2；邮箱 首字符+@域名；QQ/微信 首2尾1。"""
    if ioc_type in ("phones", "bank_cards"):
        if len(value) <= 4:
            return "*" * len(value)
        return value[:2] + "*" * (len(value) - 4) + value[-2:]
    if ioc_type == "emails":
        local, _, domain = value.partition("@")
        if not local or not domain:
            return "*" * len(value)
        return local[0] + "@" + domain
    if ioc_type in ("qq", "wechat"):
        if len(value) <= 3:
            return "*" * len(value)
        return value[:2] + "*" * (len(value) - 3) + value[-1:]
    return "*" * len(value)


class CoverIdentityService:
    """伪装身份 CRUD + 主动暴露 + 接触登记（脱敏/IOC/锁定）+ 嵌套地图。"""

    # ------------------------------------------------------------- 创建/查询
    def create_identity(self, data: dict, conn) -> dict:
        """创建伪装身份：layer = parent 层+1（无 parent=1）；name 唯一校验。

        入参 dict：{name, persona_type?, parent_id?, avatar_desc?, bio_desc?,
        fake_phone?, fake_wechat?, fake_qq?, fake_email?, expose_strategy?}
        """
        name = str(data.get("name") or "").strip()
        if not name:
            raise ApiError("name_required", "伪装名 name 不能为空", 422)
        persona_type = str(data.get("persona_type") or "受害者A").strip()
        if persona_type not in PERSONA_TYPES:
            raise ApiError(
                "invalid_persona_type",
                f"persona_type 可选 {sorted(PERSONA_TYPES)}，实际 {persona_type!r}",
                422,
            )
        expose_strategy = str(data.get("expose_strategy") or "主动").strip()
        if expose_strategy not in EXPOSE_STRATEGIES:
            raise ApiError(
                "invalid_expose_strategy",
                f"expose_strategy 可选 {sorted(EXPOSE_STRATEGIES)}，实际 {expose_strategy!r}",
                422,
            )
        parent_id = data.get("parent_id")
        layer = 1
        if parent_id is not None:
            parent_id = int(parent_id)
            parent = conn.execute(
                "SELECT id, layer FROM cover_identities WHERE id=?", (parent_id,)
            ).fetchone()
            if parent is None:
                raise ApiError("parent_not_found", f"上层伪装不存在: id={parent_id}", 404)
            layer = int(parent["layer"]) + 1
        dup = conn.execute(
            "SELECT id FROM cover_identities WHERE name=?", (name,)
        ).fetchone()
        if dup is not None:
            raise ApiError("identity_name_exists", f"伪装名已存在: {name}", 409)
        cur = conn.execute(
            "INSERT INTO cover_identities "
            "(name, persona_type, layer, parent_id, avatar_desc, bio_desc, "
            " fake_phone, fake_wechat, fake_qq, fake_email, expose_strategy) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                name,
                persona_type,
                layer,
                parent_id,
                str(data.get("avatar_desc") or "").strip() or None,
                str(data.get("bio_desc") or "").strip() or None,
                str(data.get("fake_phone") or "").strip() or None,
                str(data.get("fake_wechat") or "").strip() or None,
                str(data.get("fake_qq") or "").strip() or None,
                str(data.get("fake_email") or "").strip() or None,
                expose_strategy,
            ),
        )
        conn.commit()
        return self.get_identity(int(cur.lastrowid), conn)

    def get_identity(self, identity_id: int, conn) -> dict:
        """单条伪装（含被接触次数/已锁定数）；不存在抛 404 identity_not_found。"""
        row = conn.execute(
            "SELECT * FROM cover_identities WHERE id=?", (identity_id,)
        ).fetchone()
        if row is None:
            raise ApiError("identity_not_found", f"伪装身份不存在: id={identity_id}", 404)
        d = dict(row)
        d["contact_count"] = self._contact_count(conn, identity_id)
        d["escalated_count"] = self._contact_count(conn, identity_id, escalated=1)
        return d

    def list_identities(self, conn) -> list[dict]:
        """全部伪装身份（layer, id 升序），每项含层数/启用态/被接触次数。"""
        out: list[dict] = []
        for r in conn.execute("SELECT * FROM cover_identities ORDER BY layer, id"):
            d = dict(r)
            d["contact_count"] = self._contact_count(conn, d["id"])
            d["escalated_count"] = self._contact_count(conn, d["id"], escalated=1)
            out.append(d)
        return out

    @staticmethod
    def _contact_count(conn, identity_id: int, escalated: int | None = None) -> int:
        if escalated is None:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM contact_events WHERE identity_id=?",
                (identity_id,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM contact_events WHERE identity_id=? AND escalated=?",
                (identity_id, escalated),
            ).fetchone()
        return int(row["n"]) if row else 0

    # ------------------------------------------------------------- 启停/删除
    def toggle(self, identity_id: int, conn) -> dict:
        """翻转启用态（0<->1）；不存在抛 404。返回更新后伪装（含被接触次数）。"""
        row = conn.execute(
            "SELECT id, enabled FROM cover_identities WHERE id=?", (identity_id,)
        ).fetchone()
        if row is None:
            raise ApiError("identity_not_found", f"伪装身份不存在: id={identity_id}", 404)
        enabled = 0 if row["enabled"] else 1
        conn.execute(
            "UPDATE cover_identities SET enabled=? WHERE id=?", (enabled, identity_id)
        )
        return self.get_identity(identity_id, conn)

    def delete(self, identity_id: int, conn) -> dict:
        """删除伪装：下层伪装 parent_id 置空（防孤儿引用）；接触事件保留（审计）。

        不存在抛 404；返回 {deleted:true, id, name}。
        """
        row = conn.execute(
            "SELECT id, name FROM cover_identities WHERE id=?", (identity_id,)
        ).fetchone()
        if row is None:
            raise ApiError("identity_not_found", f"伪装身份不存在: id={identity_id}", 404)
        conn.execute(
            "UPDATE cover_identities SET parent_id=NULL WHERE parent_id=?", (identity_id,)
        )
        conn.execute("DELETE FROM cover_identities WHERE id=?", (identity_id,))
        return {"deleted": True, "id": identity_id, "name": row["name"]}

    # ------------------------------------------------------------- 主动暴露
    def expose(self, identity_id: int, conn) -> dict:
        """主动暴露：登记暴露事件 events(kind='cover_expose')，
        返回该伪装假信息脱敏清单（供回复骗子时"泄露"，D7 掩码展示）。

        清单项 {field, label, value(假信息原文), masked(脱敏展示)}；
        仅返回已配置的假信息字段。
        """
        row = conn.execute(
            "SELECT * FROM cover_identities WHERE id=?", (identity_id,)
        ).fetchone()
        if row is None:
            raise ApiError("identity_not_found", f"伪装身份不存在: id={identity_id}", 404)
        fields = [
            ("fake_phone", "手机号", row["fake_phone"]),
            ("fake_wechat", "微信", row["fake_wechat"]),
            ("fake_qq", "QQ", row["fake_qq"]),
            ("fake_email", "邮箱", row["fake_email"]),
        ]
        checklist = [
            {"field": f, "label": label, "value": v, "masked": _mask_fake(v)}
            for f, label, v in fields
            if (v or "").strip()
        ]
        payload = {
            "identity_id": identity_id,
            "identity_name": row["name"],
            "layer": row["layer"],
            "exposed": [{"field": c["field"], "masked": c["masked"]} for c in checklist],
            "ts": _now(),
        }
        conn.execute(
            "INSERT INTO events (kind, payload) VALUES ('cover_expose', ?)",
            (json.dumps(payload, ensure_ascii=False),),
        )
        conn.commit()
        return {
            "identity_id": identity_id,
            "identity_name": row["name"],
            "layer": row["layer"],
            "checklist": checklist,
            "exposed_count": len(checklist),
            "event": "cover_expose",
        }

    # ------------------------------------------------------------- 接触登记
    def register_contact(
        self,
        identity_id: int,
        contact_type: str,
        contact_value: str,
        trap_id: int | None,
        det_id: int | None,
        conn,
    ) -> dict:
        """登记骗子接触：脱敏落库 + IOC 提取 + 同值多次锁定 + 蜜饵/检测关联。

        - 脱敏：DesensitizeService.regex_redact（D7）→ contact_events.contact_value
        - IOC：SocLibService.extract_iocs(原文) → 命中写 events(kind='cover_contact',
          payload 含 identity/层/掩码 IOC)
        - 锁定：同 contact_value（脱敏后）出现 >=2 次 → escalated=1 +
          events(kind='cover_locked')
        - 关联：trap_id 提供时 best-effort 反哺供应链图谱（记录 cover_trap_link，
          失败不中断主流程）
        """
        contact_type = str(contact_type or "").strip().lower()
        if contact_type not in CONTACT_TYPES:
            raise ApiError(
                "invalid_contact_type",
                f"contact_type 可选 {sorted(CONTACT_TYPES)}，实际 {contact_type!r}",
                422,
            )
        value = str(contact_value or "").strip()
        if not value:
            raise ApiError("contact_value_required", "contact_value 不能为空", 422)
        identity = conn.execute(
            "SELECT * FROM cover_identities WHERE id=?", (identity_id,)
        ).fetchone()
        if identity is None:
            raise ApiError("identity_not_found", f"伪装身份不存在: id={identity_id}", 404)
        layer_at = int(identity["layer"])

        # 1) 脱敏（复用 DesensitizeService 正则层，D7；全掩为空时兜底占位）
        red = DesensitizeService().regex_redact(value)
        redacted = (red["redacted"] or "").strip()
        if not redacted:
            redacted = "*" * len(value) if value else "*"

        cur = conn.execute(
            "INSERT INTO contact_events "
            "(identity_id, contact_type, contact_value, trap_id, det_id, layer_at) "
            "VALUES (?,?,?,?,?,?)",
            (identity_id, contact_type, redacted, trap_id, det_id, layer_at),
        )
        event_id = int(cur.lastrowid)

        # 2) IOC 提取（原文）→ cover_contact 事件（仅掩码，明文不落库）
        iocs = SocLibService().extract_iocs(value)
        ioc_hits: list[dict] = []
        for ioc_type in _IOC_ORDER:
            for ioc_value in (iocs.get(ioc_type) or [])[:3]:
                ioc_hits.append(
                    {"ioc_type": ioc_type, "mask": _mask_ioc(ioc_type, str(ioc_value))}
                )
        if ioc_hits:
            conn.execute(
                "INSERT INTO events (kind, payload) VALUES ('cover_contact', ?)",
                (
                    json.dumps(
                        {
                            "identity_id": identity_id,
                            "identity_name": identity["name"],
                            "layer": layer_at,
                            "contact_type": contact_type,
                            "contact_event_id": event_id,
                            "iocs": ioc_hits,
                        },
                        ensure_ascii=False,
                    ),
                ),
            )

        # 3) 锁定范围：同 contact_value（脱敏后）>=2 次 → escalated=1 + cover_locked
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM contact_events WHERE contact_value=?",
            (redacted,),
        ).fetchone()["n"]
        escalated = 0
        if int(n) >= 2:
            conn.execute(
                "UPDATE contact_events SET escalated=1 WHERE contact_value=?", (redacted,)
            )
            conn.execute(
                "INSERT INTO events (kind, payload) VALUES ('cover_locked', ?)",
                (
                    json.dumps(
                        {
                            "identity_id": identity_id,
                            "identity_name": identity["name"],
                            "layer": layer_at,
                            "contact_value_mask": redacted,
                            "count": int(n),
                        },
                        ensure_ascii=False,
                    ),
                ),
            )
            escalated = 1

        # 4) 蜜饵/检测关联（可选 best-effort：记录 cover_trap_link，失败不中断）
        trap_link = self._link_trap(event_id, identity, trap_id, det_id, conn)

        conn.commit()
        return {
            "event_id": event_id,
            "identity_id": identity_id,
            "identity_name": identity["name"],
            "layer_at": layer_at,
            "contact_type": contact_type,
            "contact_value_mask": redacted,
            "replaced": red["replaced"],
            "ioc_count": len(ioc_hits),
            "escalated": escalated,
            "trap_link": trap_link,
        }

    def _link_trap(
        self, event_id: int, identity, trap_id: int | None, det_id: int | None, conn
    ) -> dict:
        """反哺供应链图谱（best-effort）：蜜饵存在即写 cover_trap_link 关联事件。

        通过 events(kind='trap_hit_escalated') 反查该蜜饵关联检测 id；任何异常
        返回 {linked:false, reason:...}，不抛出、不影响接触登记主流程。
        """
        try:
            if trap_id is None:
                return {"linked": False, "reason": "no_trap_id"}
            trap = conn.execute(
                "SELECT id, bait_text, status FROM honey_facts WHERE id=?", (int(trap_id),)
            ).fetchone()
            if trap is None:
                return {"linked": False, "reason": "trap_not_found"}
            det_ids: list[int] = []
            if det_id is not None:
                det_ids.append(int(det_id))
            for r in conn.execute(
                "SELECT payload FROM events WHERE kind='trap_hit_escalated' "
                "ORDER BY id DESC LIMIT 10"
            ):
                try:
                    p = json.loads(r["payload"])
                except (TypeError, ValueError):
                    continue
                if int(p.get("trap_id", 0)) == int(trap_id) and p.get("detection_id"):
                    det_ids.append(int(p["detection_id"]))
            det_ids = list(dict.fromkeys(det_ids))
            conn.execute(
                "INSERT INTO events (kind, payload) VALUES ('cover_trap_link', ?)",
                (
                    json.dumps(
                        {
                            "contact_event_id": event_id,
                            "identity_id": identity["id"],
                            "identity_name": identity["name"],
                            "trap_id": int(trap_id),
                            "det_ids": det_ids,
                            "status": trap["status"],
                        },
                        ensure_ascii=False,
                    ),
                ),
            )
            return {"linked": True, "trap_id": int(trap_id), "det_ids": det_ids}
        except Exception:  # noqa: BLE001 —— 关联失败不影响主流程
            return {"linked": False, "reason": "link_failed_ignored"}

    # ------------------------------------------------------------- 查询/地图
    def contacts(
        self,
        conn,
        identity_id: int | None = None,
        contact_type: str | None = None,
        escalated: bool | None = None,
        limit: int = 100,
    ) -> list[dict]:
        """接触记录（id 倒序，limit 上限 500）：按身份/类型/锁定过滤。
        每项带 identity_name / identity_layer（LEFT JOIN，删除后可为空）。
        """
        sql = (
            "SELECT ce.*, ci.name AS identity_name, ci.layer AS identity_layer "
            "FROM contact_events ce LEFT JOIN cover_identities ci ON ci.id=ce.identity_id "
            "WHERE 1=1"
        )
        params: list = []
        if identity_id is not None:
            sql += " AND ce.identity_id=?"
            params.append(int(identity_id))
        if contact_type:
            sql += " AND ce.contact_type=?"
            params.append(str(contact_type).strip().lower())
        if escalated is not None:
            sql += " AND ce.escalated=?"
            params.append(1 if escalated else 0)
        sql += " ORDER BY ce.id DESC LIMIT ?"
        params.append(min(int(limit), 500))
        return [dict(r) for r in conn.execute(sql, params)]

    def cover_map(self, conn) -> dict:
        """伪装嵌套图：identities(全量含接触数) + tree(按 parent 嵌套) +
        layers(每层被接触数) + total_contacts。"""
        identities = self.list_identities(conn)
        by_id = {n["id"]: n for n in identities}
        children: dict[int, list[dict]] = {}
        for n in identities:
            pid = n["parent_id"]
            if pid is not None and pid in by_id:
                children.setdefault(pid, []).append(
                    {
                        "id": n["id"],
                        "name": n["name"],
                        "persona_type": n["persona_type"],
                        "layer": n["layer"],
                        "contact_count": n["contact_count"],
                        "escalated_count": n["escalated_count"],
                    }
                )
        for lst in children.values():
            lst.sort(key=lambda x: x["layer"])
        tree = [
            {
                "id": n["id"],
                "name": n["name"],
                "persona_type": n["persona_type"],
                "layer": n["layer"],
                "contact_count": n["contact_count"],
                "escalated_count": n["escalated_count"],
                "children": children.get(n["id"], []),
            }
            for n in identities
            if n["parent_id"] is None or n["parent_id"] not in by_id
        ]
        tree.sort(key=lambda x: x["layer"])
        layers = [
            {"layer": int(r["layer_at"]), "contacts": int(r["n"])}
            for r in conn.execute(
                "SELECT layer_at, COUNT(*) AS n FROM contact_events "
                "GROUP BY layer_at ORDER BY layer_at"
            )
        ]
        return {
            "identities": identities,
            "tree": tree,
            "layers": layers,
            "total_contacts": sum(l["contacts"] for l in layers),
        }