"""社工库防御服务（SocLibService）—— 合规、降级式的社工库情报查询。

合规边界（防御原则 / D7 脱敏与最小化）：
1) 只查嫌疑对象：analyze() 仅对上游已判定为嫌疑对象的检测项（det_id + 提取出的 IOCs）
   发起查询，不主动收集、不批量遍历任意个人数据；
2) 结果仅情报补充：查询结果只作为威胁情报写入事件流（events, kind='soc_lib_hit'），
   仅用于防御研判；事件 payload 只含掩码后的 IOC，不回写个人敏感明文；
3) 不抛异常降级：HIBP 无密钥 / 本地库缺失 / 网络失败 / 限流等任何异常，
   一律降级返回 {source:'none', found:false, detail:'社工库不可用(降级)'}，绝不向上抛出。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re

logger = logging.getLogger(__name__)

try:  # 依赖缺失时在线查询整体降级为不可用，不阻塞业务
    import httpx
except Exception:  # pragma: no cover
    httpx = None

_HIBP_BASE = "https://haveibeenpwned.com/api/v3/breachedaccount/{email}"

# IOC 提取正则（与需求约定一致）
_RE_PHONE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_RE_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
_RE_QQ = re.compile(r"[1-9]\d{4,10}")
_RE_WECHAT = re.compile(r"[a-zA-Z][a-zA-Z0-9_-]{5,19}")
_RE_BANK_CARD = re.compile(r"\d{13,19}")

_IOC_ORDER = ("phones", "emails", "qq", "wechat", "bank_cards")


def _sha256(value: str) -> str:
    """项目约定哈希风格：sha256(utf-8 明文).hexdigest()，明文不落库。"""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _mask(ioc_type: str, value: str) -> str:
    """按类型掩码：手机/卡 首2尾2；邮箱 首字母+@域名；QQ/微信 首2尾1。"""
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


class SocLibService:
    """社工库防御服务：IOC 提取 → HIBP / 本地泄露库查询 → 命中事件落库（降级不抛异常）。"""

    # ------------------------------------------------------------- IOC 提取
    def extract_iocs(self, text: str) -> dict:
        """正则提取 IOCs，返回 {phones, emails, qq, wechat, bank_cards}（去重保序）。"""
        text = text or ""
        # 互斥提取：先提手机号（带数字边界），QQ 再排除与手机号重叠的串，保证同一串不双提
        phones = _RE_PHONE.findall(text)
        phone_set = set(phones)
        found = {
            "phones": phones,
            "emails": _RE_EMAIL.findall(text),
            "qq": [q for q in _RE_QQ.findall(text) if q not in phone_set],
            "wechat": _RE_WECHAT.findall(text),
            "bank_cards": _RE_BANK_CARD.findall(text),
        }
        return {k: list(dict.fromkeys(v)) for k, v in found.items()}

    # ------------------------------------------------------------- 在线 / 本地查询
    def query(self, email: str | None = None, phone: str | None = None) -> dict:
        """社工库查询：HIBP（需 AF_HIBP_API_KEY 且 email）→ 本地 leak_records（sha256 匹配 ioc_value_hash）。

        - 200 → {source:'hibp', found:true, breaches:[名称]}
        - 404 → {source:'hibp', found:false}
        - 本地表存在 → {source:'local', found, breach_name}
        - 任何异常 / 源不可用 → {source:'none', found:false, detail:'社工库不可用(降级)'}（绝不抛出）
        """
        try:
            hibp = self._query_hibp(email)
            local = self._query_local(email or phone)
        except Exception as exc:  # 兜底：任何异常一律降级
            logger.exception("soc_lib query degraded: %s", exc)
            return {"source": "none", "found": False, "detail": "社工库不可用(降级)"}

        # 命中优先返回；否则返回确已执行过查询的来源结果（未命中也是有效情报）
        for result in (hibp, local):
            if result and result.get("found"):
                return result
        for result in (hibp, local):
            if result and result.get("source") in ("hibp", "local"):
                return result
        return {"source": "none", "found": False, "detail": "社工库不可用(降级)"}

    def _query_hibp(self, email: str | None) -> dict | None:
        """HIBP v3 breachedaccount：200→命中；404→未命中；其余（无 key/非邮箱/429/5xx/网络异常）→ None。"""
        key = os.environ.get("AF_HIBP_API_KEY")
        if not key or not email or httpx is None:
            return None
        try:
            resp = httpx.get(
                _HIBP_BASE.format(email=email),
                timeout=8.0,
                headers={"Hibp-Api-Key": key},
            )
        except Exception:
            return None
        if resp.status_code == 200:
            data = resp.json()
            names = [b.get("Name") for b in data if isinstance(b, dict) and b.get("Name")]
            return {"source": "hibp", "found": True, "breaches": names}
        if resp.status_code == 404:
            return {"source": "hibp", "found": False}
        return None

    def _query_local(self, value: str | None) -> dict | None:
        """本地泄露库：仅当 leak_records 表存在时，按 sha256(value) 匹配 ioc_value_hash。"""
        if not value:
            return None
        digest = _sha256(value.strip())
        try:
            from ..db import get_conn

            conn = get_conn()
            if not conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='leak_records'"
            ).fetchone():
                return None  # 表不存在 → 本地源不可用
            hit = conn.execute(
                "SELECT breach_name FROM leak_records WHERE ioc_value_hash=? LIMIT 1",
                (digest,),
            ).fetchone()
        except Exception:
            return None
        if hit is None:
            return {"source": "local", "found": False, "breach_name": None}
        return {"source": "local", "found": True, "breach_name": hit["breach_name"]}

    # ------------------------------------------------------------- 分析落库
    def analyze(self, det_id, iocs: dict, conn) -> dict:
        """逐类取第一个 IOC 调 query；命中写 events(kind='soc_lib_hit')；返回 {analyzed, hits}。

        hits: [{ioc_type, ioc_value_mask, found, source, breach}]；单类失败降级，不中断整体。
        """
        iocs = iocs or {}
        hits: list[dict] = []
        analyzed = 0
        for ioc_type in _IOC_ORDER:
            values = iocs.get(ioc_type) or []
            if not values:
                continue
            analyzed += 1
            first = str(values[0])
            masked = _mask(ioc_type, first)
            try:
                if ioc_type == "emails":
                    result = self.query(email=first)
                else:
                    # 非邮箱类型不触发 HIBP；本地库按 sha256(该值) 匹配各类型 IOC
                    result = self.query(phone=first)
            except Exception as exc:  # 防御：单类失败降级
                logger.exception("soc_lib analyze item degraded (%s): %s", ioc_type, exc)
                result = {"source": "none", "found": False, "detail": "社工库不可用(降级)"}

            breach = result.get("breach")
            if breach is None and isinstance(result.get("breaches"), list):
                breach = result["breaches"][0] if result["breaches"] else None
            hits.append(
                {
                    "ioc_type": ioc_type,
                    "ioc_value_mask": masked,
                    "found": bool(result.get("found")),
                    "source": result.get("source"),
                    "breach": breach,
                }
            )
            if result.get("found"):
                self._record_hit(conn, det_id, ioc_type, masked, result.get("source"), breach)
        return {"analyzed": analyzed, "hits": hits}

    def _record_hit(self, conn, det_id, ioc_type: str, masked: str, source, breach) -> None:
        """命中落库 events(kind='soc_lib_hit')，payload 仅含掩码 IOC（明文不落库）。"""
        payload = {
            "det_id": det_id,
            "ioc_type": ioc_type,
            "ioc_value_mask": masked,
            "source": source,
            "breach": breach,
        }
        try:
            conn.execute(
                "INSERT INTO events (kind, payload) VALUES ('soc_lib_hit', ?)",
                (json.dumps(payload, ensure_ascii=False),),
            )
            conn.commit()
        except Exception as exc:  # 事件落库失败不影响分析结果
            logger.exception("soc_lib event insert failed: %s", exc)

    # ------------------------------------------------------------- 状态
    def status(self, conn) -> dict:
        """{hibp_configured, local_records}：HIBP 密钥是否配置 + 本地泄露库记录数（表缺失记 0）。"""
        hibp_configured = bool(os.environ.get("AF_HIBP_API_KEY"))
        local_records = 0
        try:
            if conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='leak_records'"
            ).fetchone():
                local_records = conn.execute("SELECT COUNT(*) AS n FROM leak_records").fetchone()["n"]
        except Exception:
            local_records = 0
        return {"hibp_configured": hibp_configured, "local_records": local_records}