"""蜜饵引擎：完整生命周期 + SimHash 踩饵检测（R1）。

对应主方案 §4.1 trap_engine、§5.3（HITL 部署）、§9 P4 DoD、§11（命中即退役）：

  状态机：draft → active → monitored → hit → retired（非法迁移抛 TrapError）
    - draft    草稿（生成即此态；永不自动发布，D3 HITL）
    - active   用户手动发布后 deploy 标记（记录 deployed_at / target_url）
    - monitored 进入监控（monitor 端点；check-hit 也接受 active/monitored）
    - hit      踩饵命中（实锤，D2）→ 立即 hit → retired（命中即退役防反查）
    - retired  终态（retired_reason: hit/user/disable）

  踩饵检测（无 embedding 依赖的降级链）：
    1) 归一化精确/包含匹配（去标点空白+小写；长文本含 bait 片段即命中）
    2) 64-bit SimHash 预筛（汉明距离 ≤ SIMHASH_THRESHOLD）
    3) 字符二元组 Jaccard 重叠（改写 5-10 字/加标点仍召回）
    三者任一命中 = 实锤踩饵（D2 确定性优先）。

  伪装度评分（可解释）：
    - LLM 可用 → LLM 校验（schema 钳制 0-100，DegradedError 自动降级启发式，D5）
    - 否则启发式：基础 100，话术词库命中/敏感词扣分，长度/自然语气加分，
      返回 reasons 理由清单（可解释）。

  合规（D7）：只生成草稿 + 埋设指引；发布动作必须由用户手动执行后显式调 deploy。
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections import Counter

from ..utils import ApiError
from .config_reader import cfg_float
from .llm_provider import DegradedError, get_provider

# ---------------------------------------------------------------------------
# 状态机
# ---------------------------------------------------------------------------
TRANSITIONS: dict[str, set[str]] = {
    "draft": {"active", "retired"},
    "active": {"monitored", "retired"},
    "monitored": {"hit", "retired"},
    "hit": {"retired"},
    "retired": set(),
}
VALID_STATUSES = set(TRANSITIONS)

# SimHash 预筛汉明距离阈值；归一化重叠阈值（改写召回）
SIMHASH_THRESHOLD = 8
OVERLAP_THRESHOLD = 0.6

# P1-B3：候选文本归一化后的有效字符数下限。空/空白/纯符号/纯 emoji 归一化为空串，
# 若不做下限守卫，substring 包含判断（'' in nb 恒真）会把任意蜜饵误判命中并退役。
MIN_CANDIDATE_LEN = 4

# 离线模板变体池（LLM 不可用时的生成降级，对齐 Mellivora TemplateProvider）
TRAP_TEMPLATES: list[dict] = [
    {"id": "t_invest_flow", "name": "投资理财引流",
     "text": "最近我在研究一个投资机会，收益挺不错的，你有兴趣的话可以一起聊聊"},
    {"id": "t_parttime_job", "name": "兼职刷单引流",
     "text": "问一下，你有没有兴趣做一个简单的兼职，时间自由，当天结算"},
    {"id": "t_crypto_airdrop", "name": "空投福利引流",
     "text": "看到你动态了，这边有个新活动能领福利，想了解的话可以回复我"},
    {"id": "t_question_link", "name": "话题提问引流",
     "text": "打扰了，想请教一个问题，看到你似乎懂这个，方便的话能聊聊吗"},
]

# 蜜饵生成 LLM schema（§6.2）
BAIT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "bait_text": {"type": "string"},
        "note": {"type": "string"},
    },
    "required": ["bait_text"],
}
BAIT_SYSTEM = (
    "你是反诈侦查工具的蜜饵文案生成器。请生成一段自然、有吸引力的引导文案（蜜饵），"
    "用于引诱诈骗分子主动暴露骗术。要求：口语化、不出现任何诈骗敏感词、长度 20-60 字。"
    '只输出 JSON：{"bait_text": "...", "note": "埋设建议"}。'
)
DISGUISE_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "disguise_score": {"type": "integer", "minimum": 0, "maximum": 100},
        "reason": {"type": "string"},
    },
    "required": ["disguise_score", "reason"],
}
DISGUISE_SYSTEM = (
    "你是蜜饵伪装度评审。判断一段文案看起来有多自然（越像正常日常交流伪装度越高，"
    "越像诈骗话术越低）。只输出 JSON："
    '{"disguise_score": 0到100的整数, "reason": "一句话理由"}。'
)

# 自然语气词 / 金钱敏感词（启发式伪装度）
_NATURAL_WORDS = ("您好", "你好", "请问", "谢谢", "方便", "麻烦", "感觉", "一起", "打扰")
_SENSITIVE_WORDS = ("转账", "银行卡", "验证码", "密码", "支付宝", "微信收款", "手续费", "保证金")


class TrapError(ApiError):
    """蜜饵业务异常：非法状态迁移 / 不存在 / 已退役 / 已停用等。"""

    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        super().__init__(code=code, message=message, status_code=status_code)


def _now() -> str:
    from datetime import datetime

    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# SimHash / 归一化 / 相似度
# ---------------------------------------------------------------------------
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_WORD_RE = re.compile(r"[A-Za-z0-9]+")


def _tokenize(text: str) -> list[str]:
    """分词：拉丁词（小写）+ 中文字符二元组（中文无空格，bigram 抗改写）。"""
    tokens = [m.group(0).lower() for m in _WORD_RE.finditer(text)]
    chars = "".join(_CJK_RE.findall(text))
    tokens.extend(chars[i:i + 2] for i in range(len(chars) - 1))
    return tokens or ["<empty>"]


def _normalize(text: str) -> str:
    """字符归一化：小写 + 仅保留 CJK/字母/数字（去标点空白，标点改动不破匹配）。"""
    return re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]", "", text.lower())


def simhash(text: str) -> int:
    """64-bit simhash：md5 取前 8 字节做位加权累计（权重 = token 频次封顶 3）。"""
    weights = Counter(_tokenize(text))
    v = [0] * 64
    for token, w in weights.items():
        h = int.from_bytes(hashlib.md5(token.encode("utf-8")).digest()[:8], "big")
        for i in range(64):
            v[i] += w if (h >> i) & 1 else -w
    out = 0
    for i in range(64):
        if v[i] > 0:
            out |= 1 << i
    return out


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def _bigram_overlap(a: str, b: str) -> float:
    """归一化文本的字符二元组 Jaccard（改写 5-10 字/加标点仍高重叠）。"""
    if not a or not b:
        return 0.0
    ga = {a[i:i + 2] for i in range(len(a) - 1)}
    gb = {b[i:i + 2] for i in range(len(b) - 1)}
    if not ga or not gb:
        return 1.0 if a == b else 0.0
    return len(ga & gb) / len(ga | gb)


def match_similarity(bait: str, candidate: str,
                     *, simhash_threshold: int = SIMHASH_THRESHOLD,
                     overlap_threshold: float = OVERLAP_THRESHOLD) -> dict:
    """踩饵匹配（实锤判定）：返回命中与否 + 各通道证据。

    P2-1：阈值可经 configs 表热更新覆盖（trap.simhash_threshold / trap.overlap_threshold）。
    P1-B3：候选归一化后有效字符过少（<MIN_CANDIDATE_LEN，即空/空白/纯符号/纯 emoji）
    信息量不足，直接判不命中 —— 修掉 substring 包含判断对空归一化候选恒真的误命中。
    """
    nb, nc = _normalize(bait), _normalize(candidate)
    if len(nc) < MIN_CANDIDATE_LEN:
        return {
            "hit": False,
            "matched_by": [],
            "exact": False,
            "substring": False,
            "hamming": hamming(simhash(bait), simhash(candidate)),
            "simhash_hit": False,
            "overlap": 0.0,
            "overlap_hit": False,
            "reason": "candidate_too_short",
        }
    exact = nb == nc
    substring = bool(nc) and len(nb) >= 8 and (nb in nc or nc in nb)
    d = hamming(simhash(bait), simhash(candidate))
    ov = round(_bigram_overlap(nb, nc), 3)
    simhash_hit = d <= simhash_threshold
    overlap_hit = ov >= overlap_threshold
    matched_by = []
    if exact:
        matched_by.append("exact")
    if substring:
        matched_by.append("substring")
    if simhash_hit:
        matched_by.append("simhash")
    if overlap_hit:
        matched_by.append("overlap")
    return {
        "hit": bool(matched_by),
        "matched_by": matched_by,
        "exact": exact,
        "substring": substring,
        "hamming": d,
        "simhash_hit": simhash_hit,
        "overlap": ov,
        "overlap_hit": overlap_hit,
    }


def find_near_duplicate(existing: list[tuple[int, str]], candidate: str,
                        *, hamming_max: int = SIMHASH_THRESHOLD,
                        overlap_min: float = OVERLAP_THRESHOLD) -> int | None:
    """近重复检测（P3-9）：候选文本与 existing[(id, text)] 中任一文本"改写重复"即返回其 id。

    复用踩饵检测的 SimHash（汉明距离 ≤ hamming_max）与字符二元组重叠（≥ overlap_min），
    用于话术/导入入库前的近重复去重——同一骗术改写（text_hash 不同）不重复入库。
    无近重复返回 None。
    """
    if not existing or not (candidate or "").strip():
        return None
    n_cand = _normalize(candidate)
    if not n_cand:
        return None
    cand_hash = simhash(candidate)
    for eid, text in existing:
        if not (text or "").strip():
            continue
        n_t = _normalize(text)
        if not n_t:
            continue
        if n_cand == n_t:
            return int(eid)
        if hamming(cand_hash, simhash(text)) <= hamming_max:
            return int(eid)
        if _bigram_overlap(n_cand, n_t) >= overlap_min:
            return int(eid)
    return None


# ---------------------------------------------------------------------------
# 蜜饵引擎
# ---------------------------------------------------------------------------
class TrapEngine:
    # ------------------------------------------------------------ 伪装度
    def _disguise_heuristic(self, conn, bait_text: str) -> dict:
        """启发式伪装度：基础 100，诈骗词/敏感词扣分，长度/自然语气加分，理由可解释。"""
        score = 100.0
        reasons: list[str] = []
        lower = bait_text.lower()

        for row in conn.execute(
            "SELECT pattern, regex, weight FROM speech_patterns WHERE enabled=1"
        ).fetchall():
            if row["regex"]:
                m = re.search(row["regex"], bait_text)
            else:
                m = row["pattern"].lower() in lower
            if m:
                pen = min(float(row["weight"]) * 2.0, 20.0)
                score -= pen
                reasons.append(f"含疑似诈骗词'{row['pattern']}'(-{pen:g})")

        n = len(bait_text)
        if 10 <= n <= 80:
            score += 10
            reasons.append("长度适中(+10)")
        elif n < 10:
            score -= 15
            reasons.append("过短易显刻意(-15)")
        else:
            score -= 10
            reasons.append("过长易显刻意(-10)")

        for w in _NATURAL_WORDS:
            if w in bait_text:
                score += 3
                reasons.append(f"含自然语气词'{w}'(+3)")
                break

        for w in _SENSITIVE_WORDS:
            if w in bait_text:
                score -= 12
                reasons.append(f"含金钱敏感词'{w}'(-12)")

        score = max(0.0, min(100.0, score))
        return {
            "disguise_score": round(score, 1),
            "reasons": reasons[:8] or ["无明显扣分项，文本自然"],
            "method": "heuristic",
        }

    async def _disguise(self, conn, bait_text: str) -> dict:
        """LLM 校验伪装度；不可用/失败自动降级启发式（D5）。"""
        provider = get_provider()
        try:
            out = await provider.complete(
                system=DISGUISE_SYSTEM,
                user=f"请评审以下文案的伪装度：\n{bait_text}",
                schema=DISGUISE_SCHEMA,
            )
            return {
                "disguise_score": round(float(out["disguise_score"]), 1),
                "reasons": [out.get("reason", "LLM 评审")],
                "method": "llm",
            }
        except DegradedError:
            return self._disguise_heuristic(conn, bait_text)

    # ------------------------------------------------------------ 生成（HITL）
    async def _generate_bait(self, conn, template_id: str | None) -> tuple[str, str]:
        """生成蜜饵文案：LLM 优先，不可用降级离线模板池。返回 (文案, 生成方式)。"""
        provider = get_provider()
        try:
            out = await provider.complete(
                system=BAIT_SYSTEM,
                user="请生成一条用于评论区埋设的蜜饵文案（口语化、无敏感词）。",
                schema=BAIT_SCHEMA,
            )
            text = str(out["bait_text"]).strip()
            if text:
                return text, "llm"
        except DegradedError:
            pass
        tpl = next((t for t in TRAP_TEMPLATES if t["id"] == template_id), None)
        if tpl is None:
            # P3-6：确定性模板选择（跨进程/重启可复现）。
            # Python 内置 hash() 受 PYTHONHASHSEED 随机化影响，这里用 sha256 前 4 字节取模。
            seed = template_id or "fallback"
            idx = int.from_bytes(hashlib.sha256(seed.encode("utf-8")).digest()[:4], "big") % len(TRAP_TEMPLATES)
            tpl = TRAP_TEMPLATES[idx]
        return tpl["text"], f"template:{tpl['id']}"

    def _insert_draft(self, conn, *, bait_text: str, disguise: dict,
                      generation: str, note: str | None) -> int:
        fingerprint = str(uuid.uuid4())
        cur = conn.execute(
            "INSERT INTO honey_facts (bait_text, fingerprint, disguise_score, status) "
            "VALUES (?, ?, ?, 'draft')",
            (bait_text, fingerprint, disguise["disguise_score"]),
        )
        trap_id = int(cur.lastrowid)
        self._log_event(conn, "trap_draft_created", {
            "trap_id": trap_id,
            "generation": generation,
            "note": note,
        })
        conn.commit()
        return trap_id

    async def generate_draft(self, conn, *, template_id: str | None = None,
                             platform: str = "评论区", note: str | None = None) -> dict:
        """生成蜜饵草稿（HITL：只产草稿 + 埋设指引，永不自动发布，D3）。"""
        bait_text, generation = await self._generate_bait(conn, template_id)
        disguise = await self._disguise(conn, bait_text)
        trap_id = self._insert_draft(conn, bait_text=bait_text, disguise=disguise,
                                     generation=generation, note=note)
        d = self.get(conn, trap_id)
        d.update({
            "disguise_reason": disguise["reasons"],
            "disguise_method": disguise["method"],
            "generation": generation,
            "fingerprint_note": (
                f"模板[{generation}]埋设建议：发布到{platform}后，"
                f"调用 POST /api/v1/traps/{trap_id}/deploy 标记部署（HITL：发布由用户手动完成）。"
            ),
            "hitl_hint": "系统只生成文案与指纹，绝不自动发布（D3）。",
        })
        return d

    def create_draft(self, conn, *, bait_text: str, note: str | None = None) -> dict:
        """直接创建蜜饵草稿（用户自拟文案；伪装度用启发式）。"""
        disguise = self._disguise_heuristic(conn, bait_text)
        trap_id = self._insert_draft(conn, bait_text=bait_text, disguise=disguise,
                                     generation="manual", note=note)
        d = self.get(conn, trap_id)
        d["disguise_reason"] = disguise["reasons"]
        d["disguise_method"] = disguise["method"]
        d["generation"] = "manual"
        return d

    # ------------------------------------------------------------ 生命周期
    def _transition(self, row, target: str) -> None:
        """状态机校验：非法迁移抛 TrapError。"""
        current = row["status"]
        if current not in TRANSITIONS or target not in TRANSITIONS[current]:
            raise TrapError(
                "invalid_transition",
                f"非法状态迁移: {current} → {target}（允许: {sorted(TRANSITIONS.get(current, set()))}）",
                400,
            )

    def _log_event(self, conn, kind: str, payload: dict) -> None:
        conn.execute(
            "INSERT INTO events (kind, payload) VALUES (?, ?)",
            (kind, json.dumps(payload, ensure_ascii=False)),
        )

    def _get_row(self, conn, trap_id: int):
        row = conn.execute("SELECT * FROM honey_facts WHERE id=?", (trap_id,)).fetchone()
        if row is None:
            raise TrapError("trap_not_found", f"蜜饵不存在: id={trap_id}", 404)
        return row

    def get(self, conn, trap_id: int) -> dict:
        row = self._get_row(conn, trap_id)
        return dict(row)

    def deploy(self, conn, trap_id: int, *, target_url: str | None = None) -> dict:
        """标记 deployed（draft→active）：用户手动发布后显式调用（D3 HITL）。"""
        row = self._get_row(conn, trap_id)
        self._transition(row, "active")
        conn.execute(
            "UPDATE honey_facts SET status='active', deployed_at=?, target_url=COALESCE(?, target_url) WHERE id=?",
            (_now(), target_url, trap_id),
        )
        self._log_event(conn, "trap_deployed", {"trap_id": trap_id, "target_url": target_url})
        conn.commit()
        return self.get(conn, trap_id)

    def monitor(self, conn, trap_id: int) -> dict:
        """进入监控（active→monitored）。"""
        row = self._get_row(conn, trap_id)
        self._transition(row, "monitored")
        conn.execute("UPDATE honey_facts SET status='monitored' WHERE id=?", (trap_id,))
        self._log_event(conn, "trap_monitored", {"trap_id": trap_id})
        conn.commit()
        return self.get(conn, trap_id)

    def disable(self, conn, trap_id: int) -> dict:
        """停用（enabled=0；仅 active/monitored 可停用，退役/草稿不可）。"""
        row = self._get_row(conn, trap_id)
        if row["status"] not in ("active", "monitored"):
            raise TrapError("invalid_transition",
                            f"仅 active/monitored 可停用，当前: {row['status']}", 400)
        conn.execute("UPDATE honey_facts SET enabled=0 WHERE id=?", (trap_id,))
        self._log_event(conn, "trap_disabled", {"trap_id": trap_id})
        conn.commit()
        return self.get(conn, trap_id)

    def retire(self, conn, trap_id: int, *, reason: str = "user") -> dict:
        """退役（draft/active/monitored/hit → retired）。命中即退役由 check_hit 自动触发。"""
        row = self._get_row(conn, trap_id)
        self._transition(row, "retired")
        conn.execute(
            "UPDATE honey_facts SET status='retired', retired_reason=? WHERE id=?",
            (reason, trap_id),
        )
        self._log_event(conn, "trap_retired", {"trap_id": trap_id, "reason": reason})
        conn.commit()
        return self.get(conn, trap_id)

    # ------------------------------------------------------------ 踩饵检测
    async def check_hit(self, conn, trap_id: int, text: str) -> dict:
        """输入候选文本检测踩饵；命中 = 实锤（D2）→ 状态机 hit→retired 防反查。"""
        row = self._get_row(conn, trap_id)
        if row["status"] == "retired":
            raise TrapError("trap_retired", f"蜜饵已退役（{row['retired_reason']}），不再监测", 409)
        if not row["enabled"]:
            raise TrapError("trap_disabled", "蜜饵已停用，请先启用", 409)
        if row["status"] not in ("active", "monitored"):
            raise TrapError("invalid_transition",
                            f"未部署的蜜饵不可检测踩饵，当前状态: {row['status']}", 400)

        # P2-1：阈值支持 configs 表热更新覆盖
        sim = match_similarity(
            row["bait_text"], text,
            simhash_threshold=int(cfg_float(conn, "trap.simhash_threshold", SIMHASH_THRESHOLD)),
            overlap_threshold=cfg_float(conn, "trap.overlap_threshold", OVERLAP_THRESHOLD),
        )
        if not sim["hit"]:
            return {
                "trap_id": trap_id,
                "hit": False,
                "status": row["status"],
                "hit_count": row["hit_count"],
                "similarity": sim,
                "evidence": [],
            }

        # 实锤命中：hit（记事件/累加）→ 立即 retired（命中即退役防反查）
        # 状态机只允许 monitored→hit；active 检测时先隐式进入监控（active→monitored→hit）
        detected_from = row["status"]
        if detected_from == "active":
            self._transition(row, "monitored")
            self._log_event(conn, "trap_monitored", {"trap_id": trap_id, "implicit": True})
        conn.execute(
            "UPDATE honey_facts SET status='hit', hit_count=hit_count+1 WHERE id=?",
            (trap_id,),
        )
        self._log_event(conn, "trap_hit", {
            "trap_id": trap_id,
            "text_hash": hashlib.sha256(text.encode("utf-8")).hexdigest()[:16],
            "similarity": {k: v for k, v in sim.items() if k != "hit"},
            "status_before": detected_from,
        })
        self._transition({"status": "hit"}, "retired")
        conn.execute(
            "UPDATE honey_facts SET status='retired', retired_reason='hit' WHERE id=?",
            (trap_id,),
        )
        self._log_event(conn, "trap_retired", {"trap_id": trap_id, "reason": "hit"})
        conn.commit()

        return {
            "trap_id": trap_id,
            "hit": True,
            "status": "retired",
            "transition": f"{detected_from}→hit→retired"
                          if detected_from == "monitored"
                          else "active→monitored→hit→retired",
            "hit_count": row["hit_count"] + 1,
            "retired_reason": "hit",
            "similarity": sim,
            "evidence": [
                f"匹配通道: {', '.join(sim['matched_by'])}",
                f"SimHash 汉明距离: {sim['hamming']}（阈值 {SIMHASH_THRESHOLD}）",
                f"字符二元组重叠度: {sim['overlap']}（阈值 {OVERLAP_THRESHOLD}）",
            ],
        }

    # ------------------------------------------------------------ 列表
    def list_traps(self, conn, *, status: str | None = None, limit: int = 100) -> list[dict]:
        limit = max(1, min(limit, 500))
        sql = "SELECT * FROM honey_facts"
        args: tuple = ()
        if status:
            if status not in VALID_STATUSES:
                raise TrapError("invalid_status", f"非法状态: {status}", 400)
            sql += " WHERE status=?"
            args = (status,)
        sql += " ORDER BY id DESC LIMIT ?"
        return [dict(r) for r in conn.execute(sql, args + (limit,)).fetchall()]
