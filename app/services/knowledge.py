"""知识库蒸馏服务（KnowledgeDistiller）。

《知识库蒸馏》设计 §四/§五：持续导入情报知识点 → 蒸馏（清洗/分类/脱敏/抽取/加权）→
按时间衰减 + 危害加权 → 供话术库联动（to_pattern）/ 检索 / 看板预警。
合规（§十）：明文不落库（D7）——content 存脱敏后文本，iocs 只存掩码值。

流水线：导入 → [清洗]去重/截断/HTML 清理 → [分类]ktype 关键词判定 →
[脱敏]复用 DesensitizeService → [抽取]高频关键词 + SocLibService.extract_iocs 掩码 →
[加权]weighted_score = harm_score × decay_weight → 入库 + knowledge_events（蒸馏审计）。
"""

from __future__ import annotations

import html as _html
import json
import re
from datetime import datetime, timedelta, timezone

from ..utils import ApiError
from .audit_log import write_gate_log
from .desensitize import DesensitizeService
from .soc_lib import SocLibService

# ktype 枚举与默认危害分（§五：新骗术 8 / 案例复盘 6 / 线索 5 / 平台漏洞 7 / 法律 3 / 其他 4）
KTYPE_HARM: dict[str, float] = {
    "新骗术": 8.0,
    "案例复盘": 6.0,
    "线索": 5.0,
    "平台漏洞": 7.0,
    "法律": 3.0,
    "其他": 4.0,
}
ALLOWED_KTYPES: frozenset[str] = frozenset(KTYPE_HARM)

# ktype 关键词判定表（规则，§四；可选 LLM 复核为后续增强项，缺省规则先行）
_KTYPE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "新骗术": ("新型", "新骗局", "骗局", "新套路", "变种", "最新骗", "新手法", "骗术"),
    "案例复盘": ("复盘", "案例", "过程还原", "经验教训"),
    "线索": ("线索", "疑似", "情报", "举报", "发现", "曝光"),
    "平台漏洞": ("漏洞", "风控绕过", "薅羊毛", "平台", "系统缺陷"),
    "法律": ("法律", "刑法", "条款", "判例", "处罚", "规定", "司法解释"),
    "其他": (),
}

_MAX_CONTENT = 4000
_MAX_SUBJECT = 200
_MAX_TOPIC_WORDS = 8

# 关键词抽取停用片段（单字/常用虚词）
_STOP_FRAGMENTS = frozenset(
    "的了是在与和及或也都就要会能可把这从那被让给对为向他她它你我他们它们这那"
    "进行已经通过因为所以但是应该需要一些什么怎么为什么没有可以不是这个那个我们"
)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _utc_naive_now() -> datetime:
    """UTC 当前时间（naive，与库内 created_at 的 UTC 文本格式一致）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _clean_text(text: str) -> str:
    """清洗：剥 HTML 标签 → 实体反转义 → 空白归一 → 去重空行。"""
    out = re.sub(r"<[^>]+>", " ", str(text or ""))
    out = _html.unescape(out)
    out = re.sub(r"\s+", " ", out).strip()
    return out


def _classify(text: str) -> str:
    """ktype 关键词判定：命中关键词最多者胜；无命中 → 其他。"""
    scored: list[tuple[int, str]] = []
    for ktype, kws in _KTYPE_KEYWORDS.items():
        n = sum(text.count(kw) for kw in kws)
        if n:
            scored.append((n, ktype))
    if not scored:
        return "其他"
    scored.sort(key=lambda kv: -kv[0])
    return scored[0][1]


def _extract_keywords(text: str, top: int = _MAX_TOPIC_WORDS) -> list[str]:
    """高频词抽取（规则）：纯净文本 2-4 字片段按频率排序，停用滤除 + 纯数字剥离 + 包含去重。"""
    cleaned = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", text)
    if len(cleaned) < 2:
        return []
    freq: dict[str, int] = {}
    for n in (4, 3, 2):
        if len(cleaned) < n:
            continue
        for i in range(len(cleaned) - n + 1):
            gram = cleaned[i:i + n]
            if gram in freq or any(ch in _STOP_FRAGMENTS for ch in gram):
                continue
            # P2-②：剥离纯数字/数字开头片段（掩码 IOC 残留 02/20/00/13、2手 等），只保留有意义中文/字母词
            if gram.isdigit() or gram[0].isdigit():
                continue
            freq[gram] = cleaned.count(gram)
    ranked = sorted(freq.items(), key=lambda kv: (-kv[1], kv[0]))
    out: list[str] = []
    for gram, _ in ranked:
        if any(gram in g for g in out) or any(g in gram for g in out):
            continue
        out.append(gram)
        if len(out) >= top:
            break
    return out


def _mask_ioc(kind: str, value: str) -> str:
    """IOC 掩码兜底（Desensitize 已掩码的复用其结果；此处兜底原始值）。"""
    v = str(value)
    if len(v) <= 2:
        return v
    if kind in ("phones", "bank_cards"):
        return v[:3] + "****" + v[-4:]
    if kind == "emails":
        head, _, tail = v.partition("@")
        return head[:2] + "****@" + tail
    return v[0] + "****" + v[-2:]


def _clamp_harm(value: float) -> float:
    return round(max(1.0, min(10.0, float(value))), 4)


class KnowledgeDistiller:
    """知识条目蒸馏器：distill（纯函数流水线）+ 库操作 + 检索 + 时效重算。"""

    # ------------------------------------------------------------------ 加权
    @staticmethod
    def _harm_default(ktype: str) -> float:
        """ktype → 默认危害分（§五）。"""
        return KTYPE_HARM.get(ktype, 4.0)

    @staticmethod
    def _decay(days: float) -> float:
        """时效衰减（§五）：<7 天 1.0；7-30 线性至 0.8；30-90 线性至 0.5；>90 天 0.2。"""
        if days < 7:
            return 1.0
        if days < 30:
            return round(1.0 - (days - 7) / 23.0 * 0.2, 4)
        if days < 90:
            return round(0.8 - (days - 30) / 60.0 * 0.3, 4)
        return 0.2

    # ------------------------------------------------------------------ 蒸馏
    def distill(self, item: dict) -> dict:
        """蒸馏流水线（pure）：清洗 → 分类 → 脱敏 → 抽取 → 加权。返回入库字段 dict。

        item 可用键：subject/content（必填）、ktype（可选，缺省自动判定）、harm（可选覆盖）、
        source/tags/enabled/verified（可选）。
        """
        subject = _clean_text(item.get("subject"))
        content = _clean_text(item.get("content"))
        if not subject and content:
            subject = content[:12]
        if not subject:
            raise ApiError("invalid_subject", "subject 不能为空", 422)
        if not content:
            raise ApiError("invalid_content", "content 不能为空", 422)
        subject = subject[:_MAX_SUBJECT]
        content = content[:_MAX_CONTENT]

        ktype = str(item.get("ktype") or "").strip() or _classify(subject + content)
        if ktype not in ALLOWED_KTYPES:
            raise ApiError(
                "invalid_ktype",
                f"ktype 必须为 {'/'.join(sorted(ALLOWED_KTYPES))} 之一",
                422,
            )

        # 脱敏（D7 明文不落库）：content 存掩码版；IOC 从原文提取后存掩码
        red = DesensitizeService().regex_redact(content)
        redacted_content = red["redacted"]
        replaced_by_from = {
            str(r.get("from")): str(r.get("to")) for r in red.get("replaced", [])
        }
        iocs_raw = SocLibService().extract_iocs(content)
        iocs: dict[str, list[str]] = {}
        for kind, values in iocs_raw.items():
            masked = [
                replaced_by_from.get(v) or _mask_ioc(kind, v)
                for v in values
            ]
            if masked:
                iocs[kind] = masked

        # 加权：危害分（显式覆盖或 ktype 默认）+ IOC 奖励 +1，1-10 钳制
        raw_harm = item.get("harm")
        base = self._harm_default(ktype) if raw_harm is None else float(raw_harm)
        harm = _clamp_harm(base)
        if any(iocs.values()):
            harm = min(10.0, round(harm + 1.0, 4))
        decay = 1.0  # 新条目无衰减；存量由 refresh_decay/每日 job 逐日重算
        weighted = round(harm * decay, 4)

        return {
            "subject": subject,
            "content": redacted_content,
            "source": str(item.get("source") or "manual").strip() or "manual",
            "ktype": ktype,
            "tags": list(item.get("tags") or []),
            "keywords": _extract_keywords(redacted_content),
            "iocs": iocs,
            "harm_score": harm,
            "decay_weight": decay,
            "weighted_score": weighted,
            "enabled": 1 if item.get("enabled", 1) else 0,
            "verified": 1 if item.get("verified", 0) else 0,
        }

    # ------------------------------------------------------------------ 入库
    def import_items(self, items: list[dict], conn) -> dict:
        """批量导入 + 蒸馏入库，幂等（subject+ktype 冲突 → 更新）。

        返回 {imported, distilled:[{id, subject, ktype, harm_score, decay_weight, weighted_score}]}；
        每条约写 knowledge_events(action='import') 蒸馏审计。
        """
        distilled: list[dict] = []
        imported = 0
        for item in items:
            d = self.distill(item)
            now = _now()
            existing = conn.execute(
                "SELECT id FROM knowledge_items WHERE subject=? AND ktype=?",
                (d["subject"], d["ktype"]),
            ).fetchone()
            if existing is not None:
                conn.execute(
                    """UPDATE knowledge_items SET content=?, source=?, tags=?, keywords=?,
                       iocs=?, harm_score=?, decay_weight=?, weighted_score=?, updated_at=?,
                       enabled=?, verified=? WHERE id=?""",
                    (d["content"], d["source"], json.dumps(d["tags"], ensure_ascii=False),
                     json.dumps(d["keywords"], ensure_ascii=False),
                     json.dumps(d["iocs"], ensure_ascii=False),
                     d["harm_score"], d["decay_weight"], d["weighted_score"], now,
                     d["enabled"], d["verified"], existing["id"]),
                )
                row_id = int(existing["id"])
            else:
                cur = conn.execute(
                    """INSERT INTO knowledge_items
                       (subject, content, source, ktype, tags, keywords, iocs,
                        harm_score, decay_weight, weighted_score, enabled, verified, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (d["subject"], d["content"], d["source"], d["ktype"],
                     json.dumps(d["tags"], ensure_ascii=False),
                     json.dumps(d["keywords"], ensure_ascii=False),
                     json.dumps(d["iocs"], ensure_ascii=False),
                     d["harm_score"], d["decay_weight"], d["weighted_score"],
                     d["enabled"], d["verified"], now, now),
                )
                row_id = int(cur.lastrowid)
            conn.execute(
                "INSERT INTO knowledge_events (action, payload_json) VALUES ('import', ?)",
                (json.dumps({"knowledge_id": row_id, "subject": d["subject"],
                             "ktype": d["ktype"]}, ensure_ascii=False),),
            )
            distilled.append({
                "id": row_id,
                "subject": d["subject"],
                "ktype": d["ktype"],
                "harm_score": d["harm_score"],
                "decay_weight": d["decay_weight"],
                "weighted_score": d["weighted_score"],
            })
            imported += 1
        conn.commit()
        return {"imported": imported, "distilled": distilled}

    def import_text(self, text: str, source: str = "web", conn=None) -> dict:
        """整段情报文本 → 分段切分 → 逐段蒸馏入库。"""
        if conn is None:
            from .. import db as dbmod
            conn = dbmod.get_conn()
        chunks = self._split_text(text)
        items = [{"subject": c[:12], "content": c, "source": source} for c in chunks]
        return self.import_items(items, conn)

    @staticmethod
    def _split_text(text: str, max_len: int = 200) -> list[str]:
        """文本切分：空行/句末标点分段，超长段按中文标点继续切（≥2 字符的段保留）。"""
        parts = [
            p.strip()
            for p in re.split(r"\n\s*\n|(?<=[。！？!?；;])\s+", text or "")
            if p.strip()
        ]
        chunks: list[str] = []
        for p in parts:
            while len(p) > max_len:
                cut = p[:max_len]
                last_hit = max((cut.rfind(ch) for ch in "。！？!?；;"), default=-1)
                if last_hit <= 0:
                    last_hit = cut.rfind("，")
                if last_hit <= 0:
                    last_hit = max_len
                chunks.append(cut[:last_hit + 1].strip())
                p = p[last_hit + 1:].strip()
            if p:
                chunks.append(p)
        return chunks

    # ------------------------------------------------------------------ 读取
    @staticmethod
    def _row_to_dict(row) -> dict:
        d = dict(row)
        for key in ("tags", "keywords"):
            try:
                d[key] = json.loads(d[key] or "[]")
            except (json.JSONDecodeError, TypeError):
                d[key] = []
        try:
            d["iocs"] = json.loads(d["iocs"] or "{}")
        except (json.JSONDecodeError, TypeError):
            d["iocs"] = {}
        return d

    @staticmethod
    def _get_or_404(conn, knowledge_id: int) -> dict:
        row = conn.execute(
            "SELECT * FROM knowledge_items WHERE id=?", (knowledge_id,)
        ).fetchone()
        if row is None:
            raise ApiError(
                "knowledge_not_found", f"知识条目不存在: id={knowledge_id}", 404
            )
        return KnowledgeDistiller._row_to_dict(row)

    # ------------------------------------------------------------------ 操作
    def adjust_harm(self, knowledge_id: int, delta: float, conn) -> dict:
        """人工调分（1-10 钳制）：重算 weighted_score，写 knowledge_events。"""
        row = self._get_or_404(conn, knowledge_id)
        new_harm = _clamp_harm(float(row["harm_score"]) + float(delta))
        new_weighted = round(new_harm * float(row["decay_weight"]), 4)
        conn.execute(
            "UPDATE knowledge_items SET harm_score=?, weighted_score=?, updated_at=? WHERE id=?",
            (new_harm, new_weighted, _now(), knowledge_id),
        )
        conn.execute(
            "INSERT INTO knowledge_events (action, payload_json) VALUES ('harm_adjust', ?)",
            (json.dumps({"knowledge_id": knowledge_id, "before": row["harm_score"],
                         "after": new_harm, "delta": delta}, ensure_ascii=False),),
        )
        conn.commit()
        return self._get_or_404(conn, knowledge_id)

    def set_verified(self, knowledge_id: int, conn) -> dict:
        row = self._get_or_404(conn, knowledge_id)
        conn.execute(
            "UPDATE knowledge_items SET verified=1, updated_at=? WHERE id=?",
            (_now(), knowledge_id),
        )
        conn.execute(
            "INSERT INTO knowledge_events (action, payload_json) VALUES ('verified', ?)",
            (json.dumps({"knowledge_id": knowledge_id, "subject": row["subject"]},
                        ensure_ascii=False),),
        )
        conn.commit()
        return self._get_or_404(conn, knowledge_id)

    def disable(self, knowledge_id: int, conn) -> dict:
        row = self._get_or_404(conn, knowledge_id)
        conn.execute(
            "UPDATE knowledge_items SET enabled=0, updated_at=? WHERE id=?",
            (_now(), knowledge_id),
        )
        conn.execute(
            "INSERT INTO knowledge_events (action, payload_json) VALUES ('disable', ?)",
            (json.dumps({"knowledge_id": knowledge_id, "subject": row["subject"]},
                        ensure_ascii=False),),
        )
        conn.commit()
        return self._get_or_404(conn, knowledge_id)

    def delete(self, knowledge_id: int, conn) -> dict:
        row = self._get_or_404(conn, knowledge_id)
        conn.execute("DELETE FROM knowledge_items WHERE id=?", (knowledge_id,))
        conn.execute(
            "INSERT INTO knowledge_events (action, payload_json) VALUES ('delete', ?)",
            (json.dumps({"knowledge_id": knowledge_id, "subject": row["subject"]},
                        ensure_ascii=False),),
        )
        conn.commit()
        return {"deleted": True, "id": knowledge_id}

    # ------------------------------------------------------------------ 联动
    def to_pattern(self, knowledge_id: int, category: str, conn,
                   reason: str = "") -> dict:
        """蒸馏成语条（话术库联动，§六.1）：pattern=蒸馏关键词（无则 subject），
        weight=min(10, harm)；写 speech_patterns（source='knowledge'）幂等 + gate_logs。"""
        from .speech_seed import ALLOWED_CATEGORIES

        if category not in ALLOWED_CATEGORIES:
            raise ApiError(
                "invalid_category",
                f"category 必须为 {'/'.join(sorted(ALLOWED_CATEGORIES))} 之一",
                422,
            )
        row = self._get_or_404(conn, knowledge_id)
        keywords = row.get("keywords") or []
        pattern = (keywords[0] if keywords else row["subject"]).strip()
        pattern = re.sub(r"\s+", " ", pattern)[:_MAX_SUBJECT]
        if not pattern:
            raise ApiError("invalid_pattern", "蒸馏关键词为空，无法生成话术词条", 422)
        weight = min(10.0, float(row["harm_score"]))

        dup = conn.execute(
            "SELECT id FROM speech_patterns WHERE pattern=? AND category=?",
            (pattern, category),
        ).fetchone()
        if dup is not None:
            return {
                "knowledge_id": knowledge_id, "pattern": pattern, "category": category,
                "weight": weight, "pattern_id": int(dup["id"]), "inserted": False,
            }
        cur = conn.execute(
            "INSERT INTO speech_patterns (pattern, regex, weight, category, source, enabled) "
            "VALUES (?, NULL, ?, ?, 'knowledge', 1)",
            (pattern, weight, category),
        )
        pattern_id = int(cur.lastrowid)
        write_gate_log(
            conn,
            action="knowledge.to_pattern",
            payload_json=json.dumps(
                {"action": "knowledge.to_pattern", "knowledge_id": knowledge_id,
                 "pattern": pattern, "category": category, "reason": reason or "知识蒸馏成语条"},
                ensure_ascii=False, sort_keys=True,
            ),
            before=json.dumps(
                {"knowledge_id": knowledge_id, "subject": row["subject"],
                 "harm_score": row["harm_score"]}, ensure_ascii=False, sort_keys=True,
            ),
            after=json.dumps(
                {"pattern": pattern, "category": category, "weight": weight,
                 "pattern_id": pattern_id}, ensure_ascii=False, sort_keys=True,
            ),
            reason=reason or "知识蒸馏成语条",
        )
        conn.commit()
        return {
            "knowledge_id": knowledge_id, "pattern": pattern, "category": category,
            "weight": weight, "pattern_id": pattern_id, "inserted": True,
        }

    # ------------------------------------------------------------------ 检索
    def search(self, q: str, conn, ktype: str | None = None,
               min_harm: float | None = None, days: int | None = None) -> list[dict]:
        """检索（§六.4）：q 模糊匹配 subject/content/keywords，按 weighted_score 降序。"""
        conds: list[str] = ["enabled=1"]
        args: list = []
        q = (q or "").strip()
        if q:
            conds.append("(subject LIKE ? OR content LIKE ? OR keywords LIKE ?)")
            args.extend([f"%{q}%", f"%{q}%", f"%{q}%"])
        if ktype:
            conds.append("ktype=?")
            args.append(ktype)
        if min_harm is not None:
            conds.append("harm_score>=?")
            args.append(float(min_harm))
        if days and days > 0:
            cutoff = (_utc_naive_now() - timedelta(days=int(days))).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            conds.append("created_at>=?")
            args.append(cutoff)
        rows = conn.execute(
            f"SELECT * FROM knowledge_items WHERE {' AND '.join(conds)} "
            "ORDER BY weighted_score DESC, id DESC LIMIT 100",
            tuple(args),
        ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    # ------------------------------------------------------------------ 时效重算
    def refresh_decay(self, conn, now: datetime | None = None) -> int:
        """时效衰减每日重算（P18 job）：decay_weight/weighted_score 按 created_at 落库。"""
        now = now or _utc_naive_now()
        rows = conn.execute(
            "SELECT id, harm_score, created_at FROM knowledge_items WHERE enabled=1"
        ).fetchall()
        n = 0
        for r in rows:
            try:
                created = datetime.strptime(r["created_at"], "%Y-%m-%d %H:%M:%S")
            except (ValueError, TypeError):
                created = now
            days = max(0.0, (now - created).total_seconds() / 86400.0)
            decay = self._decay(days)
            weighted = round(float(r["harm_score"]) * decay, 4)
            conn.execute(
                "UPDATE knowledge_items SET decay_weight=?, weighted_score=? WHERE id=?",
                (decay, weighted, r["id"]),
            )
            n += 1
        conn.commit()
        return n