"""账号速查（R3，P5）：公开信号评分 → 绿黄红风险 + 可解释证据链 + 账号簇共现。

对应主方案 §4.1 account_intel 职责与 §9 P5 DoD：
- 只用公开/自有信号（D7）：注册时长、内容一致性、被举报记录（若有）、踩饵记录关联
- 输入信号走白名单过滤（PUBLIC_SIGNAL_KEYS），私有字段一律丢弃，不人肉
- 风险：score≥10 红 / ≥4 黄 / 其余绿；证据链 [{signal, weight, note}] 可解释
- 账号簇共现（可选）：多个账号踩中同一批蜜饵 → 提示疑似团伙
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from ..utils import ApiError

# 公开信号白名单（D7：只读公开/自有信息；其余字段在入库前被丢弃）
PUBLIC_SIGNAL_KEYS = frozenset({
    "registered_at",          # 注册时间 ISO8601
    "activity_level",         # 活跃度 0-1（公开动态统计）
    "content_consistency",    # 内容一致性 0-1
    "reported_count",         # 被举报次数（官方/平台公示，若有）
    "trap_hit_ids",           # 关联蜜饵踩饵记录 id 列表（自有数据）
    "bio",                    # 公开简介
    "follower_count",         # 公开粉丝数
    "answer_count",           # 公开回答数
})

RED_MIN = 10.0
YELLOW_MIN = 4.0


def _parse_days(iso_text: str) -> int | None:
    try:
        dt = datetime.fromisoformat(str(iso_text).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0, (datetime.now(timezone.utc) - dt).days)
    except ValueError:
        return None


class AccountIntelService:
    def check(self, conn, url_name: str, signals: dict | None = None,
              persist: bool = True) -> dict:
        """账号速查：合并公开信号 → 评分 → 绿黄红 + 证据链 + 共现账号。

        R4-F3：persist=False（匿名调用，无有效 admin key）→ 只做只读评估，
        不 upsert 画像、不写库（防匿名灌入账号库）；已有档案仍返回其 account_id。
        persist=True（已认证/服务级默认）→ 维持原 upsert 行为（证据链沉淀）。
        返回新增字段 persisted：本次是否落库。
        """
        url_name = (url_name or "").strip()
        if not url_name:
            raise ApiError("invalid_url_name", "url_name 不能为空", 400)

        incoming = {k: v for k, v in (signals or {}).items() if k in PUBLIC_SIGNAL_KEYS}
        stored = conn.execute("SELECT * FROM accounts WHERE url_name=?", (url_name,)).fetchone()
        merged: dict = {}
        if stored:
            try:
                merged = json.loads(stored["signals"] or "{}")
            except json.JSONDecodeError:
                merged = {}
        merged.update(incoming)
        merged["checked_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        trap_hit_ids = [int(x) for x in merged.get("trap_hit_ids", []) or [] if str(x).isdigit()]
        risk, score, evidence = self._score(merged, trap_hit_ids)
        co_occurring = self._co_occurring(conn, url_name, trap_hit_ids)

        # upsert 画像（R4-F3：persist=False 时跳过写库，只读评估）
        account_id = stored["id"] if stored else None
        if persist:
            if stored:
                conn.execute(
                    "UPDATE accounts SET signals=?, risk_level=?, trap_hit_ids=?, updated_at=? WHERE id=?",
                    (json.dumps(merged, ensure_ascii=False), risk,
                     json.dumps(trap_hit_ids, ensure_ascii=False),
                     datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"), stored["id"]),
                )
                account_id = stored["id"]
            else:
                cur = conn.execute(
                    "INSERT INTO accounts (url_name, signals, risk_level, trap_hit_ids) VALUES (?, ?, ?, ?)",
                    (url_name, json.dumps(merged, ensure_ascii=False), risk,
                     json.dumps(trap_hit_ids, ensure_ascii=False)),
                )
                account_id = int(cur.lastrowid)
            conn.commit()

        return {
            "account_id": account_id,
            "url_name": url_name,
            "risk_level": risk,
            "score": score,
            "evidence": evidence,
            "co_occurring_accounts": co_occurring,
            "signals": merged,
            "persisted": persist,
        }

    # ------------------------------------------------------------------ 评分
    def _score(self, signals: dict, trap_hit_ids: list[int]) -> tuple[str, float, list[dict]]:
        chain: list[dict] = []
        score = 0.0

        reg = signals.get("registered_at")
        if reg:
            days = _parse_days(str(reg))
            if days is not None and days <= 30:
                score += 4.0
                chain.append({"signal": "account_age_young", "weight": 4.0, "note": f"注册仅 {days} 天（新号）"})
            elif days is not None and days >= 365:
                chain.append({"signal": "account_age_old", "weight": -1.0, "note": f"注册 {days} 天（老号，降权）"})
                score += -1.0

        cc = signals.get("content_consistency")
        if isinstance(cc, (int, float)) and cc < 0.3:
            score += 3.0
            chain.append({"signal": "content_consistency_low", "weight": 3.0, "note": f"内容一致性偏低（{cc:.2f}）"})

        rc = signals.get("reported_count")
        if isinstance(rc, (int, float)) and rc > 0:
            w = min(8.0, 4.0 * float(rc))
            score += w
            chain.append({"signal": "reported", "weight": w, "note": f"被举报记录 {rc} 条"})

        if trap_hit_ids:
            score += 10.0
            chain.append({"signal": "trap_hit_evidence", "weight": 10.0,
                          "note": f"关联 {len(trap_hit_ids)} 条蜜饵踩饵记录（实锤线索）"})

        score = round(max(score, 0.0), 2)
        if not chain:
            chain.append({"signal": "no_signal", "weight": 0.0, "note": "暂无公开信号，默认绿色（信息不足不作恶评）"})

        risk = "red" if score >= RED_MIN else ("yellow" if score >= YELLOW_MIN else "green")
        return risk, score, chain

    # ------------------------------------------------------------------ 账号簇共现
    def _co_occurring(self, conn, url_name: str, trap_hit_ids: list[int]) -> list[str]:
        """共现：其他账号与当前账号踩中同一批蜜饵 → 疑似团伙。"""
        if not trap_hit_ids:
            return []
        mine = set(trap_hit_ids)
        rows = conn.execute(
            "SELECT url_name, trap_hit_ids FROM accounts WHERE url_name != ?", (url_name,)
        ).fetchall()
        co: list[str] = []
        for r in rows:
            try:
                theirs = {int(x) for x in json.loads(r["trap_hit_ids"] or "[]")}
            except (json.JSONDecodeError, ValueError):
                continue
            if mine & theirs:
                co.append(r["url_name"])
        return co

    # ------------------------------------------------------------------ 查询
    def get_account(self, conn, url_name: str) -> dict:
        row = conn.execute("SELECT * FROM accounts WHERE url_name=?", (url_name,)).fetchone()
        if row is None:
            raise ApiError("account_not_found", f"账号不存在: {url_name}", 404)
        d = dict(row)
        try:
            d["signals"] = json.loads(d["signals"] or "{}")
            d["trap_hit_ids"] = json.loads(d["trap_hit_ids"] or "[]")
        except json.JSONDecodeError:
            pass
        return d

    def timeline(self, conn, account_id: int) -> list[dict]:
        row = conn.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
        if row is None:
            raise ApiError("account_not_found", f"账号不存在: id={account_id}", 404)
        items: list[dict] = [{
            "ts": row["updated_at"],
            "kind": "account_updated",
            "note": "账号画像更新",
        }]
        try:
            trap_ids = json.loads(row["trap_hit_ids"] or "[]")
        except json.JSONDecodeError:
            trap_ids = []
        for tid in trap_ids:
            t = conn.execute("SELECT * FROM honey_facts WHERE id=?", (int(tid),)).fetchone()
            if t:
                items.append({
                    "ts": t["created_at"],
                    "kind": "trap_hit",
                    "note": f"蜜饵#{t['id']} 踩饵命中（hit_count={t['hit_count']}, status={t['status']}）",
                })
        items.sort(key=lambda x: str(x["ts"]), reverse=True)
        return items