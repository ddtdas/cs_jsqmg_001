"""证据包（R5，P6）：组装 → sha256 哈希链 → 冻结不可变 → 导出 / 举报模板。

对应主方案 §4.1 evidence 职责、§8 evidence_packages 表、§9 P6 DoD、§11：
- 哈希链：每份证据内容 sha256，链式 hash = sha256(prev_hash + content_sha256)；
  内容来自 DB（detections/speech_hits）与截图路径，verify() 重算比对 → 篡改即失败
- freeze：冻结后 frozen=1 + frozen_at；冻结前内容任何变更 → verify 失败（存证核心）
- 举报模板：96110 / 平台 / 辟谣 三类，仅个人署名 + 处置引导（D7 不处置、不冒充官方）
- 模板内嵌内容一律先过正则脱敏（D7 强制）

P2-C5 局限说明（外部信任锚点）：哈希链与内容同库共存，verify 只防"误改/意外篡改"，
不防"持库者整体重算并重写链"（intact=True 的已知局限）；正式存证建议外置链根——
导出时附独立校验文件（如 HMAC(secret, chain_root)）或对接 RFC3161 可信时间戳服务
作为外部锚点；链根外置前，本哈希链语义为"防误改"，勿用于对抗持库者的司法举证。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from ..utils import ApiError
from .desensitize import DesensitizeService


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class EvidenceService:
    # ------------------------------------------------------------------ 组装
    def _detection_items(self, conn, det_ids: list[int], *, tolerate_missing: bool = False) -> list[dict]:
        """组检测原文条目。

        P2-C 修复：tolerate_missing=True 时（verify/export 路径），源 detection 行
        已被删除则不 404 崩溃，改返回标注 source_deleted=True 的占位条目
        （原内容不可重建——哈希链只存摘要，缺失即链断裂由 verify 反映）；
        build 路径保持严格（tolerate_missing=False，缺行仍 404，防止误绑不存在的检测）。
        """
        items: list[dict] = []
        for det_id in det_ids:
            det = conn.execute(
                """
                SELECT id, source, content, rule_score, grade, llm_verdict,
                       judge_confidence, created_at, se_factors
                FROM detections WHERE id=?
                """,
                (det_id,),
            ).fetchone()
            if det is None:
                if not tolerate_missing:
                    raise ApiError("detection_not_found", f"检测记录不存在: id={det_id}", 404)
                items.append({
                    "type": "detection",
                    "det_id": det_id,
                    "source_deleted": True,
                    "source": "deleted",
                    "content": None,
                    "rule_score": None,
                    "grade": None,
                    "llm_verdict": None,
                    "judge_confidence": None,
                    "created_at": None,
                    "hits": [],
                    "se_factors": {},
                })
                continue
            # C14：se_factors 为 JSON 文本（推理链输出）→ 解析为 dict（无/坏 JSON → {}）
            raw = det["se_factors"]
            try:
                se_factors = json.loads(raw) if raw else {}
            except (json.JSONDecodeError, TypeError):
                se_factors = {}
            hits = conn.execute(
                """
                SELECT sh.matched_text, sh.score, sp.pattern, sp.category
                FROM speech_hits sh JOIN speech_patterns sp ON sp.id = sh.pattern_id
                WHERE sh.det_id = ? ORDER BY sh.score DESC
                """,
                (det_id,),
            ).fetchall()
            items.append({
                "type": "detection",
                "det_id": det_id,
                "source": det["source"],
                "content": det["content"],
                "rule_score": det["rule_score"],
                "grade": det["grade"],
                "llm_verdict": det["llm_verdict"],
                "judge_confidence": det["judge_confidence"],
                "created_at": det["created_at"],
                "hits": [dict(h) for h in hits],
                "se_factors": se_factors,
            })
        return items

    @staticmethod
    def build_chain(items: list[dict]) -> list[dict]:
        """哈希链：每节点 hash = sha256(prev_hash + content_sha256)；prev 链式串联。"""
        chain: list[dict] = []
        prev = ""
        for i, item in enumerate(items):
            content = json.dumps(item, sort_keys=True, ensure_ascii=False)
            content_sha = _sha256(content)
            node_hash = _sha256(prev + content_sha)
            chain.append({
                "index": i,
                "type": item.get("type", "item"),
                "prev_hash": prev,
                "content_sha256": content_sha,
                "hash": node_hash,
            })
            prev = node_hash
        return chain

    def build(self, conn, det_ids: list[int], screenshots: list[str] | None = None) -> dict:
        """组装证据包：检测明细 + 截图路径占位 → 哈希链 → 入库（frozen=0）。

        P1-3（R1 严格验收）：同一检测不可重复建包——建包前对既有包做 json_each
        精确比对，任一 det_id 已存在于某证据包即拒绝（409 evidence_exists）。
        原自动路径用 LIKE 子串去重会漏建/误判，此处为服务层去重约束（防御式），
        手动/API/MCP 重复建包同样被拦截，杜绝证据包重复。
        顺序约定：先 _detection_items 严格校验（源缺失 → 404 detection_not_found，
        P2-C 语义优先），再做重复建包检查（409 evidence_exists）。
        """
        if not det_ids:
            raise ApiError("empty_evidence", "至少需要一个检测记录", 400)
        screenshots = screenshots or []
        items = self._detection_items(conn, det_ids)
        for det_id in det_ids:
            # P2-1（R2）：json_valid 门控（CASE 短路）——历史脏数据（det_ids 非合法
            # JSON）行不再让 json_each 抛 malformed JSON 导致 build() 500，直接跳过。
            dup = conn.execute(
                "SELECT id FROM evidence_packages WHERE CASE WHEN json_valid(det_ids) THEN "
                "EXISTS (SELECT 1 FROM json_each(det_ids) WHERE CAST(value AS TEXT) = ?) "
                "ELSE 0 END LIMIT 1",
                (str(det_id),),
            ).fetchone()
            if dup:
                raise ApiError(
                    "evidence_exists",
                    f"检测 #{det_id} 已存在于证据包 #{dup['id']}（同一检测不可重复建包）",
                    409,
                )
        items += [{"type": "screenshot", "path": p} for p in screenshots]
        chain = self.build_chain(items)

        cur = conn.execute(
            "INSERT INTO evidence_packages (det_ids, screenshots, hash_chain, frozen) VALUES (?, ?, ?, 0)",
            (json.dumps(det_ids, ensure_ascii=False),
             json.dumps(screenshots, ensure_ascii=False),
             json.dumps(chain, ensure_ascii=False)),
        )
        pkg_id = int(cur.lastrowid)
        conn.execute(
            "INSERT INTO events (kind, payload) VALUES ('evidence_built', ?)",
            (json.dumps({"pkg_id": pkg_id, "det_ids": det_ids}, ensure_ascii=False),),
        )
        conn.commit()
        return {
            "pkg_id": pkg_id,
            "det_ids": det_ids,
            "screenshots": screenshots,
            "hash_chain": chain,
            "frozen": 0,
            "created_at": _now(),
        }

    # ------------------------------------------------------------------ 校验
    def verify(self, conn, pkg_id: int) -> dict:
        """重算哈希链与存储链比对：返回 intact（冻结后内容变更 → False）。

        P2-C：源 detection 缺失时不 404 崩溃——先统计缺失 id，内容不可重建则
        链校验失败并标注 source_deleted=True + missing_det_ids（含明细）。
        """
        pkg = conn.execute("SELECT * FROM evidence_packages WHERE id=?", (pkg_id,)).fetchone()
        if pkg is None:
            raise ApiError("evidence_not_found", f"证据包不存在: id={pkg_id}", 404)
        stored = json.loads(pkg["hash_chain"] or "[]")
        det_ids = json.loads(pkg["det_ids"] or "[]")
        missing = [
            i for i in det_ids
            if conn.execute("SELECT 1 FROM detections WHERE id=?", (i,)).fetchone() is None
        ]
        items = self._detection_items(conn, det_ids, tolerate_missing=True)
        items += [{"type": "screenshot", "path": p} for p in json.loads(pkg["screenshots"] or "[]")]
        recomputed = self.build_chain(items)
        intact = stored == recomputed
        if missing:
            detail = f"SOURCE_DELETED：源检测记录已删除（id={missing}），内容不可重建，链校验失败"
        elif intact:
            detail = "OK"
        else:
            detail = "TAMPERED：内容与哈希链不一致"
        return {
            "pkg_id": pkg_id,
            "intact": intact,
            "frozen": bool(pkg["frozen"]),
            "frozen_at": pkg["frozen_at"],
            "nodes": len(stored),
            "source_deleted": bool(missing),
            "missing_det_ids": missing,
            "detail": detail,
        }

    def freeze(self, conn, pkg_id: int) -> dict:
        """冻结：哈希不可变（冻结后任何内容变更 → verify 失败）。"""
        pkg = conn.execute("SELECT * FROM evidence_packages WHERE id=?", (pkg_id,)).fetchone()
        if pkg is None:
            raise ApiError("evidence_not_found", f"证据包不存在: id={pkg_id}", 404)
        if pkg["frozen"]:
            raise ApiError("already_frozen", "证据包已冻结", 409)
        conn.execute(
            "UPDATE evidence_packages SET frozen=1, frozen_at=? WHERE id=?",
            (_now(), pkg_id),
        )
        conn.execute(
            "INSERT INTO events (kind, payload) VALUES ('evidence_frozen', ?)",
            (json.dumps({"pkg_id": pkg_id}, ensure_ascii=False),),
        )
        conn.commit()
        return self.verify(conn, pkg_id)

    # ------------------------------------------------------------------ 导出
    def export(self, conn, pkg_id: int, fmt: str = "json") -> dict:
        """导出证据包（json 结构化 / text 可读文本）。

        P2-C：源 detection 缺失时（tolerate_missing=True）不 404——
        json/text 均输出 source_deleted 占位与 verify.source_deleted 标注。
        """
        pkg = conn.execute("SELECT * FROM evidence_packages WHERE id=?", (pkg_id,)).fetchone()
        if pkg is None:
            raise ApiError("evidence_not_found", f"证据包不存在: id={pkg_id}", 404)
        ver = self.verify(conn, pkg_id)
        det_ids = json.loads(pkg["det_ids"] or "[]")
        payload = {
            "pkg_id": pkg_id,
            "det_ids": det_ids,
            "screenshots": json.loads(pkg["screenshots"] or "[]"),
            "hash_chain": json.loads(pkg["hash_chain"] or "[]"),
            "frozen": bool(pkg["frozen"]),
            "frozen_at": pkg["frozen_at"],
            "created_at": pkg["created_at"],
            "verified": ver,
            "items": self._detection_items(conn, det_ids, tolerate_missing=True),
        }
        if fmt == "text":
            lines = [
                f"证据包 #{pkg_id}（frozen={'是' if pkg['frozen'] else '否'}）",
                f"创建时间: {pkg['created_at']}  完整性: {'OK' if ver['intact'] else ('SOURCE_DELETED' if ver.get('source_deleted') else 'TAMPERED')}",
            ]
            for it in payload["items"]:
                if it.get("source_deleted"):
                    lines.append(f"- 检测#{it['det_id']} [源记录已删除] 内容不可重建（source_deleted）")
                    continue
                lines.append(f"- 检测#{it['det_id']} [{it['source']}] grade={it['grade']} rule_score={it['rule_score']}")
                for h in it["hits"]:
                    lines.append(f"    * 命中: {h['pattern']}（{h['category']}）score={h['score']}")
            for node in payload["hash_chain"]:
                lines.append(f"- 链节点[{node['index']}] {node['type']}: {node['hash'][:16]}...")
            return {"fmt": "text", "content": "\n".join(lines), "verified": ver}
        return {"fmt": "json", "content": payload, "verified": ver}

    # ------------------------------------------------------------------ 举报模板
    def report_template(self, conn, pkg_id: int, kind: str = "96110") -> dict:
        """生成举报/辟谣模板（仅个人署名 + 处置引导；不含处置措辞与官方冒充，D7）。"""
        if kind not in ("96110", "platform", "pibyao"):
            raise ApiError("invalid_template_kind", "kind 必须为 96110/platform/pibyao", 400)
        pkg = conn.execute("SELECT * FROM evidence_packages WHERE id=?", (pkg_id,)).fetchone()
        if pkg is None:
            raise ApiError("evidence_not_found", f"证据包不存在: id={pkg_id}", 404)
        det_ids = json.loads(pkg["det_ids"] or "[]")
        det = conn.execute("SELECT * FROM detections WHERE id=?", (det_ids[0],)).fetchone() if det_ids else None
        if det is None:
            raise ApiError("detection_not_found", "证据包无关联检测记录", 404)

        hits = conn.execute(
            """
            SELECT sp.pattern, sp.category FROM speech_hits sh
            JOIN speech_patterns sp ON sp.id = sh.pattern_id
            WHERE sh.det_id = ? ORDER BY sh.score DESC LIMIT 5
            """,
            (det["id"],),
        ).fetchall()
        hit_summary = "、".join(f"{h['pattern']}({h['category']})" for h in hits) or "（规则未命中）"
        # 模板内嵌内容一律脱敏（D7 强制）
        redacted = DesensitizeService().regex_redact(det["content"] or "")["redacted"]

        base = {
            "ts": _now(),
            "grade": det["grade"] or "未分级",
            "hit_summary": hit_summary,
            "content": redacted,
            "pkg_id": pkg_id,
        }
        if kind == "96110":
            text = (
                "【反诈举报指引 · 供 96110 / 国家反诈中心 APP 提交】\n"
                "本人为普通个人用户，现将疑似电信网络诈骗话术线索提交供反诈部门参考：\n"
                f"- 线索时间：{base['ts']}\n"
                f"- 风险分级（个人工具评估）：{base['grade']}\n"
                f"- 话术命中：{base['hit_summary']}\n"
                f"- 线索内容（已脱敏）：{base['content']}\n"
                "本人仅作个人举报与线索转述，不构成对任何人的指控；"
                "处置请交由 96110 与属地反诈部门依法进行。\n"
                "签名：普通用户（个人）"
            )
        elif kind == "platform":
            text = (
                "【平台举报指引 · 供站内举报入口提交】\n"
                "本人为平台普通用户，发现疑似诈骗内容，特向平台举报，请平台依据社区规则核查：\n"
                f"- 发现时间：{base['ts']}\n"
                f"- 内容摘要（已脱敏）：{base['content']}\n"
                "本人为个人举报，不冒充任何机构；核查与处置由平台依法依规进行。\n"
                "签名：普通用户（个人）"
            )
        else:  # pibyao
            text = (
                "【辟谣澄清指引 · 供个人公开发布澄清时参考】\n"
                "本人为普通个人用户，就相关疑似诈骗话术作如下个人澄清声明：\n"
                f"- 声明时间：{base['ts']}\n"
                f"- 相关线索分级（个人工具评估）：{base['grade']}\n"
                "- 提示：请勿轻信来源不明的投资、兼职、退款等信息，转账前多方核实。\n"
                "- 本声明不引用、不点名任何具体人员；处置以执法部门与平台为准。\n"
                "签名：普通用户（个人）"
            )
        return {"kind": kind, "text": text, "signature": "普通用户（个人）"}