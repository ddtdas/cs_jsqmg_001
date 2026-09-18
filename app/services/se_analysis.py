"""社会工程学推理链规则层（确定性，不依赖 LLM，D5 降级可用）。

对应设计文档《社会工程学推理链与全面测试》§2.2：
在既有话术规则层（R1，speech_engine.rule_match）基础上，新增**确定性规则层**，
把原始文本映射为可解释的社会工程学证据链：

    attack_vector    攻击向量（pretexting/phishing/baiting/quid_pro_quo/tailgating）
    psych_techniques 心理技巧（Cialdini 六原则：urgency/authority/scarcity/
                     social_proof/reciprocity/commitment）
    seadm_stage      SEADM 攻击阶段（relationship/exploitation/disclosure/
                     execution/completion）
    scam_lifecycle   诈骗话术生命周期（break_ice/persona/groom/small_bait/
                     big_invest/withdraw_block/second_harvest/exit）
    evidence         证据清单 [{factor, matched, text}]（每个命中的因子一条）
    confidence       命中因子数 / 全部因子数（上限 1，可解释信号覆盖度）
    divergence       发散推理 {iocs, playbook, suggested_actions}（供看板
                     「话术套路」列；playbook 由 attack_vector+psych_techniques+
                     seadm_stage/scam_lifecycle 组合规则推理，确定性无 LLM）

纯正则 + 关键词匹配，无 LLM 调用：LLM 不可用（D5 降级）时本层照常产出，
作为五级分级的证据链（grade_reason）之外的独立可解释维度。
"""

from __future__ import annotations

import re
from typing import Any

# ---------------------------------------------------------------------------
# 关键词表（含中文）。参考设计文档 §2.2 分类与 §三 案例分析样本。
# ---------------------------------------------------------------------------

# Cialdini 心理技巧（六原则）
# R4-F1：urgency 移除过宽泛词『今天/现在』（负例"今天天气不错"误报来源），
# 细化为『今天就/现在马上』等紧邻催促表达；social_proof 补常见话术。
PSYCH_KEYWORDS: dict[str, list[str]] = {
    "urgency": ["马上", "立即", "限时", "最后", "来不及", "别错过", "尽快", "今天就", "现在马上", "立刻"],
    "authority": ["公安", "检察院", "法院", "银行", "客服", "官方", "导师", "专家", "内部", "政府"],
    "scarcity": ["名额", "仅此", "最后几个", "限量", "内部渠道", "专属"],
    "social_proof": ["很多人", "大家都", "都在做", "已赚", "成功案例", "别人都", "很多客户都", "都在赚"],
    "reciprocity": ["返利", "垫付", "佣金", "红包", "福利", "优惠", "免费赠送"],
    "commitment": ["稳赚", "保证", "包赚", "保本", "必涨", "包赔"],
}

# 攻击向量（按设计文档 §1.2；phishing=链接/二维码诱导，tailgating=借他人信任链）
# R4-F1：phishing 补『扫码下载』；exploitation（SEADM）补『入金/投』（防 unknown 漏判）
VECTOR_KEYWORDS: dict[str, list[str]] = {
    "pretexting": ["公安", "检察院", "法院", "银联", "客服", "警官", "检察官", "清查", "安全账户"],
    "phishing": ["点链接", "点击链接", "二维码", "下载app", "下载APP", "输入网址", "链接", "扫码下载"],
    "baiting": ["高回报", "兼职", "刷单", "免费", "福利", "投资理财", "内幕消息", "稳赚不赔"],
    "quid_pro_quo": ["垫付", "返利", "佣金", "先交", "手续费", "保证金", "解冻费"],
    "tailgating": ["经人介绍", "熟人介绍", "朋友推荐", "同事推荐", "群里认识", "他是我朋友", "我认识的人"],
}

# SEADM 攻击阶段（推进顺序：关系→利用→披露→执行→完成）
SEADM_KEYWORDS: dict[str, list[str]] = {
    "relationship": ["在吗", "你好", "打扰", "辛苦", "关心", "嘘寒问暖", "哥哥姐姐", "弟弟妹妹"],
    "exploitation": ["需要你", "帮忙", "垫付", "操作", "点链接", "扫码", "下载", "入金", "投"],
    "disclosure": ["银行卡", "卡号", "验证码", "密码", "身份证", "转账", "收款码"],
    "execution": ["汇款", "转账", "扫码支付", "点击确认", "提交"],
    "completion": ["成功", "到账", "提现", "收益", "恭喜"],
}

# 诈骗话术生命周期（推进顺序：破冰→人设→甜头→小额试水→大额投入→提现受阻→二次收割→跑路）
LIFECYCLE_KEYWORDS: dict[str, list[str]] = {
    "break_ice": ["在吗", "你好", "打扰一下", "看到你"],
    "persona": ["导师", "成功", "高管", "内幕", "操盘手", "分析师"],
    "groom": ["小额", "试水", "先转", "返利", "试试", "体验"],
    "small_bait": ["首单", "小额投入", "先投一点", "小额资金", "少投", "试投"],
    "big_invest": ["加仓", "梭哈", "全部", "重仓", "多投", "几万", "几十万"],
    "withdraw_block": ["提现失败", "冻结", "解冻", "保证金", "卡单", "风控", "需再充"],
    "second_harvest": ["再交", "补充", "验证金", "续费"],
    "exit": ["删号", "失联", "拉黑", "无法联系", "解散"],
}

PSYCH_ORDER: list[str] = list(PSYCH_KEYWORDS.keys())
VECTOR_ORDER: list[str] = list(VECTOR_KEYWORDS.keys())
SEADM_ORDER: list[str] = list(SEADM_KEYWORDS.keys())
LIFECYCLE_ORDER: list[str] = list(LIFECYCLE_KEYWORDS.keys())

# 全部可命中因子（confidence 分母；命中因子数=evidence 条数）
ALL_FACTORS: list[str] = PSYCH_ORDER + VECTOR_ORDER + SEADM_ORDER + LIFECYCLE_ORDER

_SENTENCE_SPLIT = re.compile(r"[。！？!?；;\n]")

# ---------------------------------------------------------------------------
# 发散推理（divergence）：话术套路剧本 + 建议动作（确定性规则，无 LLM）。
# 供看板「话术套路」列（BoardView.vue 读 se_factors.divergence.playbook）。
# ---------------------------------------------------------------------------

# 空 IOC 结构（extract_iocs 的五个字段，防御式降级时原样返回）
_EMPTY_IOCS: dict[str, list[str]] = {
    "phones": [],
    "emails": [],
    "qq": [],
    "wechat": [],
    "bank_cards": [],
}

# 剧本 → 建议动作（每条 2-3 项；供研判/处置下钻）
_PLAYBOOK_ACTIONS: dict[str, list[str]] = {
    "冒充公检法剧本": [
        "查该文本 IOCs 是否在社工库泄露",
        "对比历史同类冒充公检法案例",
        "核查自称单位电话/属地真实性",
    ],
    "刷单返利剧本": [
        "查该文本 IOCs 是否在社工库泄露",
        "供应链反查关联刷单平台域名",
        "对比历史同类刷单返利案例",
    ],
    "中奖/退款二次收割剧本": [
        "查该文本 IOCs 是否在社工库泄露",
        "对比历史同类二次收割案例",
        "核查中奖/退款官方渠道真实性",
    ],
    "投资理财剧本": [
        "查该文本 IOCs 是否在社工库泄露",
        "供应链反查关联理财平台域名",
        "对比历史同类投资理财案例",
    ],
    "综合诈骗剧本": [
        "查该文本 IOCs 是否在社工库泄露",
        "对比历史同类诈骗案例",
    ],
}

# 防御式导入：soc_lib 不可用（依赖缺失/导入异常）时 IOC 提取降级为空结构，绝不抛出
try:  # pragma: no cover
    from app.services.soc_lib import SocLibService as _SocLibService
except Exception:  # pragma: no cover
    _SocLibService = None

_SOCLIB_INSTANCE: Any = None  # 惰性单例（SocLibService 无自定义 __init__，纯正则方法）


def _extract_iocs(text: str) -> dict[str, list[str]]:
    """防御式 IOC 提取：SocLibService 不可用/实例化/提取任何异常 → 全空结构。"""
    global _SOCLIB_INSTANCE
    if _SocLibService is None:  # pragma: no cover
        return dict(_EMPTY_IOCS)
    try:
        if _SOCLIB_INSTANCE is None:
            _SOCLIB_INSTANCE = _SocLibService()
        out = _SOCLIB_INSTANCE.extract_iocs(text)
        return {k: list(out.get(k, [])) for k in _EMPTY_IOCS}
    except Exception:  # pragma: no cover
        return dict(_EMPTY_IOCS)


def _infer_playbook(vector: str, psych: list[str], seadm_stage: str,
                    lifecycle: str) -> str:
    """按 attack_vector + psych_techniques + seadm_stage/scam_lifecycle 组合推理剧本。

    规则（确定性优先级）：
      pretexting + authority(+disclosure)       → 冒充公检法剧本
      baiting   + reciprocity + small_bait/groom→ 刷单返利剧本
      quid_pro_quo + urgency + second_harvest   → 中奖/退款二次收割剧本
      baiting   + social_proof + big_invest     → 投资理财剧本
      其它                                       → 综合诈骗剧本
    （冒充公检法仅要求权威冒充+前置取证即可归类；disclosure（索要卡号/验证码）
     是典型后期信号，命中时证据更强，非硬性条件。）
    """
    if vector == "pretexting" and "authority" in psych:
        return "冒充公检法剧本"
    if vector == "baiting" and "reciprocity" in psych and lifecycle in ("small_bait", "groom"):
        return "刷单返利剧本"
    if vector == "quid_pro_quo" and "urgency" in psych and lifecycle == "second_harvest":
        return "中奖/退款二次收割剧本"
    if vector == "baiting" and "social_proof" in psych and lifecycle == "big_invest":
        return "投资理财剧本"
    return "综合诈骗剧本"


_EMPTY_RESULT: dict[str, Any] = {
    "attack_vector": "unknown",
    "psych_techniques": [],
    "seadm_stage": "unknown",
    "scam_lifecycle": "unknown",
    "evidence": [],
    "confidence": 0.0,
}


def _empty_result() -> dict[str, Any]:
    """全新空结果（嵌套结构不复用共享引用，避免调用方误改污染）。"""
    return {
        **_EMPTY_RESULT,
        "divergence": {
            "iocs": dict(_EMPTY_IOCS),
            "playbook": "",
            "suggested_actions": [],
        },
    }


def _snippet(text: str, pos: int, max_len: int = 200) -> str:
    """提取关键词所在的原句片段（从最近句子边界截取，超长截断）。"""
    start = pos
    while start > 0 and text[start - 1] not in "。！？!?；;\n":
        start -= 1
    end = pos
    while end < len(text) and text[end] not in "。！？!?；;\n":
        end += 1
    seg = text[start:end].strip()
    if len(seg) > max_len:
        seg = seg[:max_len] + "…"
    return seg or text[pos:pos + max_len]


class SEAnalyzer:
    """社会工程学规则分析器：analyze(text) -> 结构化证据链（纯确定性）。"""

    def __init__(self) -> None:
        self._vector_counts: dict[str, int] = {v: 0 for v in VECTOR_ORDER}

    # ------------------------------------------------------------------ 分析
    def analyze(self, text: str | None) -> dict[str, Any]:
        """输入原始文本 → 社会工程学证据链 JSON（结构见模块 docstring）。

        空文本/空串 → unknown 全空 + confidence 0，不抛异常。
        """
        text = (text or "").strip()
        if not text:
            return _empty_result()

        # factor -> {"matched": [kw...], "text": snippet}
        evidence_map: dict[str, dict[str, Any]] = {}

        def _scan(group: dict[str, list[str]], factors: list[str]) -> None:
            for factor in factors:
                first_pos = None
                matched: list[str] = []
                for kw in group[factor]:
                    idx = text.find(kw)
                    if idx >= 0:
                        matched.append(kw)
                        if first_pos is None or idx < first_pos:
                            first_pos = idx
                if matched:
                    matched.sort(key=lambda k: text.find(k))
                    evidence_map[factor] = {
                        "matched": matched,
                        "text": _snippet(text, first_pos or 0),
                    }

        _scan(PSYCH_KEYWORDS, PSYCH_ORDER)
        _scan(VECTOR_KEYWORDS, VECTOR_ORDER)
        _scan(SEADM_KEYWORDS, SEADM_ORDER)
        _scan(LIFECYCLE_KEYWORDS, LIFECYCLE_ORDER)

        # attack_vector：命中关键词数量最多的向量；平局按枚举顺序优先；无命中 unknown
        vector = "unknown"
        best = 0
        for v in VECTOR_ORDER:
            n = len(evidence_map.get(v, {}).get("matched", []))
            if n > best:
                best, vector = n, v

        # seadm_stage / scam_lifecycle：取推进顺序最靠后的命中阶段
        seadm = next((s for s in reversed(SEADM_ORDER) if s in evidence_map), "unknown")
        lifecycle = next((l for l in reversed(LIFECYCLE_ORDER) if l in evidence_map), "unknown")

        psych = [p for p in PSYCH_ORDER if p in evidence_map]
        evidence = [
            {
                "factor": factor,
                "matched": "|".join(evidence_map[factor]["matched"]),
                "text": evidence_map[factor]["text"],
            }
            for factor in ALL_FACTORS
            if factor in evidence_map
        ]

        confidence = round(min(1.0, len(evidence) / len(ALL_FACTORS)), 3)
        playbook = _infer_playbook(vector, psych, seadm, lifecycle)
        return {
            "attack_vector": vector,
            "psych_techniques": psych,
            "seadm_stage": seadm,
            "scam_lifecycle": lifecycle,
            "evidence": evidence,
            "confidence": confidence,
            "divergence": {
                "iocs": _extract_iocs(text),
                "playbook": playbook,
                "suggested_actions": list(_PLAYBOOK_ACTIONS.get(playbook, [])),
            },
        }

    # ------------------------------------------------------------------ 便捷
    @staticmethod
    def empty() -> dict[str, Any]:
        """空结果（供 API 无数据时的统一兜底）。"""
        return _empty_result()
