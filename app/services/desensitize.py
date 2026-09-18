"""双层脱敏（R6，P6）：正则层 + LLM 复核层。

对应主方案 §4.1 desensitize 职责与 §11 合规（脱敏强制，D7）：
  1) 正则层：手机号/身份证/中文姓名（上下文启发式）/邮箱/链接/微信号 抹除，
     输出 replaced 列表 [{from, to}]（审计可追溯）；
  2) LLM 复核层：LLM 可用时复核残余敏感信息（user 文本先 fence，D4）；
     不可用/失败 → 正则结果直接用于入库（降级链，D5）；
  3) 输出 {redacted, replaced, risk}；
  4) 强制校验：scan_sensitive()/assert_clean() —— 任何入库 payload 必须先过本层
     （cases.publish 前置校验，代码层强制，非口头约定）。
"""

from __future__ import annotations

import re
import unicodedata

from .llm_provider import DegradedError, get_provider

# ---------------------------------------------------------------------------
# 正则规则（顺序敏感：先长后短/先具体后宽泛）
# ---------------------------------------------------------------------------
_PATTERNS: list[tuple[str, str]] = [
    # 手机号（中国大陆 1[3-9]xxxxxxxxx，前后非数字）
    ("phone", r"(?<!\d)1[3-9]\d{9}(?!\d)"),
    # 身份证（18 位，前 6 地区 + 8 生日 + 3 顺序 + 校验位）
    ("id_card", r"(?<!\d)[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx](?!\d)"),
    # 邮箱
    ("email", r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    # 链接（截止到中文/常见中文标点；要求含字母数字，避免命中掩码占位）
    ("url", r"https?://[^\s，。；！？、（）()\"'<>]*[A-Za-z0-9][^\s，。；！？、（）()\"'<>]*"),
    # 微信号（带标识符上下文，避免误伤普通单词）
    ("wechat", r"微信号[:：]?\s*[A-Za-z][A-Za-z0-9_-]{5,19}"),
    # 裸 wxid（R2 P2-2 顺手补：C4 裸 wxid 放行观察项 → 补 regex）
    ("wechat_bare", r"wxid_[A-Za-z0-9_-]{5,20}"),
    # QQ 号（带上下文前缀；纯数字不加规则，避免误伤其他数字串）
    ("qq", r"QQ[:：]?\s*[1-9]\d{4,10}"),
    # 中文姓名（上下文启发式：我叫/姓名/本人/我是 + 2-4 个汉字）
    ("name", r"(?:我叫|姓名[:：]|本人|我是|对方叫)([\u4e00-\u9fff]{2,4})"),
]

# ---------------------------------------------------------------------------
# 变体归一化层（P1-A6/C7 + R2 P1-C1/C2/C4）：全角数字 / 零宽字符 / 字母混淆 /
# 中文数字 / 拆词分隔符 / +86 前缀 / emoji keycap / 圈号与上标数字 / 全 Unicode
# 数字系统（Arabic-Indic、天城、孟加拉等）/ 罗马数字 / 希腊-西里尔混淆 /
# 双重 JSON 转义（\\uXXXX）/ 银行卡号。
# 原理：对文本做字符级归一化（保留原文位置映射），在归一化文本上跑变体正则，
# 再把命中跨度映射回原文做掩码 / 报告。
# ---------------------------------------------------------------------------

# 拆词分隔符：归一化时删除（138 1234 5678 / 138-1234-5678 / 138.1234.5678）
_VARIANT_DELETE: frozenset[str] = frozenset(
    " \t\r\n\u00a0\u3000-_./\\|·•–—‒‑,，;；"
)
# 零宽字符：归一化时删除（\u200b 等插缝绕过）
_VARIANT_ZERO_WIDTH: frozenset[str] = frozenset("\u200b\u200c\u200d\u2060\ufeff\u00ad")

# 按 Unicode 类别整类删除（R2 字形盲区）：
#   So  emoji/杂项符号（含 keycap 的基符以外的符号）
#   Sk  修饰符号（^ ` ´ 等）
#   Mn/Me  组合标记（含 keycap 组合符 U+20E3、变体选择符 U+FE0F、各类变音符号）
#   Cf  格式字符（ZWJ/ZWNJ/LRM/RLM/BOM 等）
#   Zs  各类空格分隔符（NBSP/EN SP/表意空格等）
#   Sm/Pd/Po  数学/连接号/标点（“138，1234，5678”等标点插缝绕过）
_VARIANT_DELETE_CATEGORIES: frozenset[str] = frozenset(
    {"So", "Sk", "Mn", "Me", "Cf", "Zs", "Sm", "Pd", "Po"}
)

# 字母混淆 → 数字（I/l/1、O/0、S/5、B/8、G/9，含全角字母 + 希腊/西里尔字形）
_CONFUSABLE_TO_DIGIT: dict[str, str] = {
    "l": "1", "L": "1", "I": "1", "i": "1",
    "ｌ": "1", "Ｌ": "1", "Ｉ": "1", "ｉ": "1",
    "o": "0", "O": "0", "ｏ": "0", "Ｏ": "0",
    "s": "5", "S": "5", "ｓ": "5", "Ｓ": "5",
    "b": "8", "B": "8", "ｂ": "8", "Ｂ": "8",
    "g": "9", "G": "9", "ｇ": "9", "Ｇ": "9",
    # 希腊（R2：④a 希腊 I/O 混淆）
    "Ο": "0", "ο": "0", "Ι": "1", "ι": "1",
    # 西里尔（R2 ④b + W3 补修：О→0、І→1、З→3、Е/Ё→3、Б→6、В→8、С→5、Ч→4）
    "О": "0", "о": "0", "І": "1", "і": "1", "ı": "1",
    "З": "3", "з": "3",           # Ze 像 3（1З8ОО… 混淆）
    "Е": "3", "е": "3", "Ё": "3", "ё": "3",  # Ye/Yo 像 3
    "Б": "6", "б": "6",           # Be 像 6
    "В": "8", "в": "8",           # Ve 像 8
    "С": "5", "с": "5",           # Es 像 5
    "Ч": "4", "ч": "4",           # Che 像 4
}

# 中文数字（小写 + 大写）→ 数字
_CN_DIGIT_CHARS: dict[str, str] = {
    "零": "0", "〇": "0", "一": "1", "壹": "1",
    "二": "2", "贰": "2", "两": "2",
    "三": "3", "叁": "3", "四": "4", "肆": "4",
    "五": "5", "伍": "5", "六": "6", "陆": "6",
    "七": "7", "柒": "7", "八": "8", "捌": "8",
    "九": "9", "玖": "9",
}


def _build_variant_map() -> dict[str, str | None]:
    m: dict[str, str | None] = {}
    # 全角数字 → ASCII 数字；全角字母 → ASCII 字母
    for i in range(10):
        m[chr(0xFF10 + i)] = str(i)
    for i in range(26):
        m[chr(0xFF21 + i)] = chr(ord("A") + i)
        m[chr(0xFF41 + i)] = chr(ord("a") + i)
    for ch in _VARIANT_ZERO_WIDTH:
        m[ch] = None
    for ch in _VARIANT_DELETE:
        m[ch] = None
    m.update(_CONFUSABLE_TO_DIGIT)
    m.update(_CN_DIGIT_CHARS)
    return m


_VARIANT_MAP: dict[str, str | None] = _build_variant_map()

# 双重 JSON 转义（P1-8/C3）：\\u0031... 字面量 → 解码后再归一化
_U_ESCAPE_RE = re.compile(r"\\u([0-9a-fA-F]{4})")


def _map_variant_char(ch: str) -> str | None:
    """单字符变体映射：显式表 → Unicode 类别删除 → digit() → numeric()。"""
    if ch in _VARIANT_MAP:
        return _VARIANT_MAP[ch]
    cat = unicodedata.category(ch)
    if cat in _VARIANT_DELETE_CATEGORIES:
        return None
    try:
        d = unicodedata.digit(ch)
        if 0 <= d <= 9:
            return str(d)
    except (ValueError, TypeError):
        pass
    try:
        n = unicodedata.numeric(ch)
        if n == int(n) and 0 <= int(n) <= 9:
            return str(int(n))
    except (ValueError, TypeError):
        pass
    return ch


def _build_variant_text(text: str) -> tuple[str, list[int]]:
    """字符级变体归一化：返回 (归一化文本, pos[i]=原文下标)。

    同时识别字面 \\uXXXX 转义序列（6 字符 → 1 字符，位置指向序列起点，
    掩码覆盖整段原文）。
    """
    out: list[str] = []
    pos: list[int] = []
    i = 0
    n = len(text)
    while i < n:
        m = _U_ESCAPE_RE.match(text, i)
        if m:
            ch = chr(int(m.group(1), 16))
            c = _map_variant_char(ch)
            if c is not None:
                out.append(c)
                pos.append(i)
            i = m.end()
            continue
        ch = text[i]
        c = _map_variant_char(ch)
        if c is not None:
            out.append(c)
            pos.append(i)
        i += 1
    return "".join(out), pos


def _luhn_valid(digits: str) -> bool:
    """Luhn 校验（银行卡号）：16-19 位数字且校验位通过才算银行卡命中，降低误伤。"""
    if len(digits) < 16 or not digits.isdigit():
        return False
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = ord(ch) - 48
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


# 变体正则（运行在归一化文本上：分隔符/零宽/符号已删，全角/混淆/中文数字/各书写系统
# 数字/罗马数字已转 ASCII；银行卡命中另做 Luhn 校验）
_VARIANT_PATTERNS: list[tuple[str, str]] = [
    # 手机号：可选 +86/86 前缀；11 位 1[3-9] 段
    ("phone", r"(?<!\d)(?:\+?86)?1[3-9]\d{9}(?!\d)"),
    # 身份证：18 位（6 地区 + 8 生日 + 3 顺序 + 校验位）
    ("id_card", r"(?<!\d)[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx](?!\d)"),
    # 银行卡（P1-9/C4）：16-19 位数字，命中后 Luhn 校验（防长数字串误伤）
    ("bank_card", r"(?<!\d)\d{16,19}(?!\d)"),
]


def _variant_find(text: str) -> list[tuple[str, str, int, int]]:
    """变体复查：返回 [(kind, 原文子串, 原文start, 原文end)]，跨度已去重叠。"""
    norm, pos = _build_variant_text(text)
    if not norm:
        return []
    raw: list[tuple[int, int, str, str]] = []
    for kind, pattern in _VARIANT_PATTERNS:
        for m in re.finditer(pattern, norm):
            s, e = m.start(), m.end()
            os_, oe = pos[s], pos[e - 1] + 1
            if kind == "bank_card" and not _luhn_valid(norm[s:e]):
                continue  # Luhn 不通过 → 普通长数字串，非银行卡
            raw.append((os_, oe, kind, text[os_:oe]))
    if not raw:
        return []
    raw.sort(key=lambda h: (h[0], h[1]))
    merged: list[tuple[str, str, int, int]] = []
    last_end = -1
    for s, e, kind, orig in raw:
        if s < last_end:
            continue  # 重叠命中只保留先出现的（跨度更长/更靠前）
        merged.append((kind, orig, s, e))
        last_end = e
    return merged

LLM_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "sensitive": {"type": "array", "items": {"type": "string"}},
        "risk": {"type": "string", "enum": ["low", "medium", "high"]},
    },
    "required": ["sensitive", "risk"],
}

LLM_SYSTEM = (
    "你是隐私脱敏复核器。文本已经过规则层脱敏，请找出其中残留的敏感信息"
    "（手机号、身份证号、银行卡号、住址、真实姓名、微信号、QQ 号等）。"
    "只输出 JSON：{\"sensitive\": [\"残留敏感串\", ...], \"risk\": \"low|medium|high\"}。"
    "没有残留时 sensitive 返回空数组。"
)


def _digits(value: str) -> str:
    return re.sub(r"\D", "", value)


def _mask(kind: str, value: str) -> str:
    """按类型保留少量可辨识字符，其余抹除。

    手机/身份证先抽取数字再掩码（变体跨度可能含 +86/分隔符/零宽等字符）。
    """
    if kind == "phone":
        digits = _digits(value)
        if digits.startswith("86") and len(digits) >= 13:
            digits = digits[2:]  # 去掉国家码，只掩国内 11 位
        d = digits[-11:]
        if len(d) == 11:
            return d[:3] + "*" * 4 + d[-4:]
        return "*" * len(value)
    if kind == "id_card":
        digits = _digits(value)
        if len(digits) >= 15:
            return digits[:3] + "*" * (len(digits) - 7) + digits[-4:]
        return "*" * len(value)
    if kind == "bank_card":
        digits = _digits(value)
        if len(digits) >= 16:
            return digits[:6] + "*" * (len(digits) - 10) + digits[-4:]
        return "*" * len(value)
    if kind == "email":
        local, _, domain = value.partition("@")
        return (local[:2] + "*" * max(1, len(local) - 2) + "@" + domain) if local else "*"
    if kind == "url":
        return "https://***"
    if kind == "wechat":
        return "微信号:***"
    if kind == "wechat_bare":
        return "wxid:***"
    if kind == "qq":
        return "QQ:***"
    if kind == "name":
        return value[0] + "*" * (len(value) - 1)
    return "*" * len(value)


class DesensitizeService:
    """双层脱敏。regex 层同步；LLM 层异步（不可用自动降级）。"""

    # ------------------------------------------------------------------ 正则层
    def regex_redact(self, text: str) -> dict:
        """正则层：抹除已知敏感模式 + 变体复查，返回 {redacted, replaced, risk, layer}。"""
        redacted = text
        replaced: list[dict] = []
        for kind, pattern in _PATTERNS:
            def _sub(m: re.Match, _kind: str = kind) -> str:
                if _kind == "name":
                    # 只掩名字本身，保留"我叫/本人"等前缀
                    name = m.group(1)
                    masked_name = _mask("name", name)
                    replaced.append({"from": name, "to": masked_name})
                    return m.group(0)[:m.start(1) - m.start(0)] + masked_name
                orig = m.group(0)
                masked = _mask(_kind, orig)
                if masked != orig:
                    replaced.append({"from": orig, "to": masked})
                return masked
            redacted = re.sub(pattern, _sub, redacted)

        # P1-A6/C7 + R2：变体复查（全角/零宽/混淆/中文数字/分隔符/+86/emoji/各书写系统/
        # 罗马/希腊-西里尔/\\uXXXX 转义/银行卡）。从右往左替换，避免掩码长度变化破坏
        # 后续跨度下标；掩码基于原文跨度的归一化子串（转义语法中的数字不参与抽号）。
        for kind, orig, s, e in reversed(_variant_find(redacted)):
            norm_orig, _ = _build_variant_text(orig)
            masked = _mask(kind, norm_orig)
            if masked != orig:
                replaced.append({"from": orig, "to": masked})
            redacted = redacted[:s] + masked + redacted[e:]
        return {
            "redacted": redacted,
            "replaced": replaced,
            "risk": "low",
            "layer": "regex",
        }

    # ------------------------------------------------------------------ LLM 层
    async def llm_redact(self, text: str) -> dict:
        """LLM 复核残余敏感信息；不可用/失败 → 原样返回（降级由调用方衔接）。"""
        provider = get_provider()
        try:
            out = await provider.complete(system=LLM_SYSTEM, user=text, schema=LLM_SCHEMA)
            sensitive: list[str] = out.get("sensitive") or []
            redacted = text
            replaced: list[dict] = []
            for s in sensitive:
                s = str(s).strip()
                if not s or s not in redacted:
                    continue
                redacted = redacted.replace(s, _mask("unknown", s))
                replaced.append({"from": s, "to": _mask("unknown", s)})
            return {
                "redacted": redacted,
                "replaced": replaced,
                "risk": "low" if not sensitive else str(out.get("risk", "medium")),
                "layer": "llm",
            }
        except DegradedError:
            return {"redacted": text, "replaced": [], "risk": "low", "layer": "degraded"}

    # ------------------------------------------------------------------ 主入口
    async def desensitize(self, text: str) -> dict:
        """双层脱敏：正则层必跑，LLM 层复核（降级自动）。"""
        r1 = self.regex_redact(text)
        r2 = await self.llm_redact(r1["redacted"])
        replaced = r1["replaced"] + r2["replaced"]
        risk = "high" if r2["risk"] == "high" else r1["risk"]
        return {
            "redacted": r2["redacted"],
            "replaced": replaced,
            "risk": risk,
            "layer": "regex+llm" if r2["layer"] == "llm" else "regex",
        }

    # ------------------------------------------------------------------ 强制校验
    @staticmethod
    def scan_sensitive(text: str) -> list[dict]:
        """扫描残留敏感信息（不修改）：返回命中列表（含变体命中）。空列表 = 干净。"""
        base: list[dict] = []
        for kind, pattern in _PATTERNS:
            for m in re.finditer(pattern, text):
                base.append({"type": kind, "value": m.group(0), "start": m.start(), "end": m.end()})
        variant = [
            {"type": kind, "value": orig, "start": s, "end": e, "variant": True}
            for kind, orig, s, e in _variant_find(text)
        ]
        out: list[dict] = []
        for b in base:
            if any(b["start"] >= v["start"] and b["end"] <= v["end"] for v in variant):
                continue  # 基础命中已被变体跨度覆盖（同一号码），避免重复报告
            out.append({"type": b["type"], "value": b["value"]})
        out.extend({"type": v["type"], "value": v["value"], "variant": True} for v in variant)
        return out

    @staticmethod
    def assert_clean(text: str) -> None:
        """强制校验：含敏感信息抛 ApiError（cases.publish 入库前调用，代码层强制）。"""
        from ..utils import ApiError

        hits = DesensitizeService.scan_sensitive(text)
        if hits:
            kinds = sorted({h["type"] for h in hits})
            raise ApiError(
                "sensitive_data",
                f"脱敏未通过，拒绝入库：检测到 {kinds}",
                422,
            )