"""种子话术词库（≥20 条，幂等入库）。

对应主方案 §8 speech_patterns 表与 §9 P1 DoD。每条含：
  pattern  关键词（匹配引擎按子串包含匹配）或 regex 的语义名称
  regex    可选正则，非空时优先于 pattern
  weight   0-10（权重，规则引擎 P3 使用）
  category 分类：fake_investment / pig_butchering / job_scam /
            refund_scam / impersonation / other
  source   'seed'（词库来源标记）

幂等策略：speech_patterns 上 UNIQUE(pattern, category)，seed_speech_patterns()
用 INSERT OR IGNORE 重复执行不产生重复行。
"""

from __future__ import annotations

SEED_PATTERNS: list[dict] = [
    # ---- 虚假投资理财 / 加密货币（fake_investment）----
    {"pattern": "稳赚不赔", "regex": r"稳赚不赔|稳赚|包赚|保赚", "weight": 9.0, "category": "fake_investment"},
    {"pattern": "投资理财高回报", "regex": r"高回报|高收益|翻倍收益", "weight": 7.0, "category": "fake_investment"},
    {"pattern": "内幕消息", "weight": 8.0, "category": "fake_investment"},
    {"pattern": "导师带你", "weight": 8.0, "category": "fake_investment"},
    {"pattern": "包赔", "weight": 9.0, "category": "fake_investment"},
    {"pattern": "保本高息", "weight": 8.0, "category": "fake_investment"},
    {"pattern": "原始股", "weight": 7.0, "category": "fake_investment"},
    {"pattern": "打新中签", "weight": 7.0, "category": "fake_investment"},
    {"pattern": "虚拟货币翻倍", "regex": r"虚拟货币|数字货币|USDT", "weight": 7.0, "category": "fake_investment"},
    {"pattern": "加密货币提现手续费", "weight": 8.0, "category": "fake_investment"},
    {"pattern": "提现需缴税", "weight": 8.0, "category": "fake_investment"},
    {"pattern": "保证收益", "weight": 7.0, "category": "fake_investment"},
    # ---- 修复 R4-F1（队3/队4 案例缺口）：典型诈骗话术补齐 ----
    # 投资理财类：收益承诺 / 扫码下载 APP / 注册领红包 / 导师带单 / 跟单
    {"pattern": "收益30%以上", "regex": r"收益\s*30%\s*以上|年化\s*30%|收益率\s*30%", "weight": 7.0, "category": "fake_investment"},
    {"pattern": "扫码下载APP", "regex": r"扫码下载|下载.{0,6}(APP|App|app|软件|客户端)", "weight": 7.0, "category": "fake_investment"},
    {"pattern": "注册领红包", "weight": 6.0, "category": "fake_investment"},
    {"pattern": "导师带单", "weight": 7.0, "category": "fake_investment"},
    {"pattern": "跟单", "weight": 5.0, "category": "fake_investment"},
    # 中奖/缴纳类：中奖 / 手续费 / 税款 / 公证费 / 需先缴纳（解冻费/保证金已有）
    {"pattern": "中奖", "weight": 6.0, "category": "other"},
    {"pattern": "手续费", "weight": 6.0, "category": "fake_investment"},
    {"pattern": "税款", "weight": 7.0, "category": "fake_investment"},
    {"pattern": "公证费", "weight": 7.0, "category": "other"},
    {"pattern": "需先缴纳", "weight": 7.0, "category": "job_scam"},
    # ---- 杀猪盘 / 网恋诱导（pig_butchering）----
    {"pattern": "杀猪盘", "weight": 10.0, "category": "pig_butchering"},
    {"pattern": "网恋借钱", "weight": 9.0, "category": "pig_butchering"},
    {"pattern": "带你赚钱", "weight": 8.0, "category": "pig_butchering"},
    {"pattern": "异国网友", "weight": 6.0, "category": "pig_butchering"},
    {"pattern": "家人急需用钱", "weight": 7.0, "category": "pig_butchering"},
    {"pattern": "网恋奔现先转账", "regex": r"奔现.*(转账|红包|打钱)|见面.*(转账|红包|打钱)", "weight": 8.0, "category": "pig_butchering"},
    # ---- 兼职刷单 / 垫付（job_scam）----
    {"pattern": "刷单返利", "regex": r"刷单|刷信誉|做任务返利", "weight": 9.0, "category": "job_scam"},
    {"pattern": "垫付", "weight": 7.0, "category": "job_scam"},
    {"pattern": "解冻费", "weight": 8.0, "category": "job_scam"},
    {"pattern": "保证金", "weight": 6.0, "category": "job_scam"},
    {"pattern": "解锁任务", "weight": 7.0, "category": "job_scam"},
    {"pattern": "日结佣金", "weight": 7.0, "category": "job_scam"},
    {"pattern": "足不出户", "weight": 7.0, "category": "job_scam"},
    {"pattern": "打字员", "weight": 6.0, "category": "job_scam"},
    # ---- 退款 / 注销校园贷（refund_scam）----
    {"pattern": "客服主动退款", "regex": r"客服.*(主动退款|理赔|退款链接)", "weight": 8.0, "category": "refund_scam"},
    {"pattern": "注销校园贷", "regex": r"注销(校园贷|白条|网贷)|校园贷", "weight": 8.0, "category": "refund_scam"},
    {"pattern": "双倍退款", "weight": 7.0, "category": "refund_scam"},
    {"pattern": "理赔链接", "weight": 7.0, "category": "refund_scam"},
    # 冒充客服退款类：退款 / 理赔 / 备用金 / 屏幕共享 / 银联冻结（R4-F1）
    {"pattern": "退款", "weight": 5.0, "category": "refund_scam"},
    {"pattern": "理赔", "weight": 6.0, "category": "refund_scam"},
    {"pattern": "备用金", "weight": 7.0, "category": "refund_scam"},
    {"pattern": "屏幕共享", "weight": 8.0, "category": "refund_scam"},
    {"pattern": "银联冻结", "regex": r"银联.{0,6}(冻结|锁定|风控|异常)", "weight": 7.0, "category": "impersonation"},
    # ---- 冒充公检法 / 客服 / 领导（impersonation）----
    {"pattern": "冒充公检法", "regex": r"公检法|检察院|公安局.*(冻结|传唤)", "weight": 9.0, "category": "impersonation"},
    {"pattern": "安全账户", "weight": 8.0, "category": "impersonation"},
    {"pattern": "涉嫌洗钱", "weight": 8.0, "category": "impersonation"},
    {"pattern": "通缉令", "weight": 7.0, "category": "impersonation"},
    {"pattern": "配合调查", "weight": 6.0, "category": "impersonation"},
    {"pattern": "冒充客服", "weight": 8.0, "category": "impersonation"},
    {"pattern": "冒充领导", "weight": 8.0, "category": "impersonation"},
    # ---- 其他引流 / 中奖（other）----
    {"pattern": "点击链接领取", "weight": 6.0, "category": "other"},
    {"pattern": "中奖兑换", "weight": 6.0, "category": "other"},
    {"pattern": "内部渠道", "weight": 6.0, "category": "other"},
    {"pattern": "内测资格", "weight": 5.0, "category": "other"},
    # 钓鱼链接 / 索取敏感信息（R4-F1/F5：URL+银行卡密组合，队1 P2-6 漏检）
    {"pattern": "钓鱼链接", "weight": 8.0, "category": "other"},
    {"pattern": "点击链接", "weight": 6.0, "category": "other"},
    {"pattern": "可疑URL", "regex": r"https?://[^\s，。！？；;、]+|www\.[A-Za-z0-9][A-Za-z0-9.-]*\.(com|cn|net|org|top|vip|xyz|cc|icu|info|io|me|online|site|club)", "weight": 6.0, "category": "other"},
    {"pattern": "输入卡号", "regex": r"输入.{0,6}卡号", "weight": 7.0, "category": "other"},
    {"pattern": "填写验证码", "weight": 7.0, "category": "other"},
]

ALLOWED_CATEGORIES: frozenset[str] = frozenset({
    "fake_investment", "pig_butchering", "job_scam", "refund_scam", "impersonation", "other",
})

_INSERT_SQL = (
    "INSERT OR IGNORE INTO speech_patterns (pattern, regex, weight, category, source, enabled) "
    "VALUES (?, ?, ?, ?, 'seed', 1)"
)


def seed_speech_patterns(conn) -> int:
    """幂等入库种子话术；返回本次实际新增行数（重复执行为 0）。"""
    before = conn.total_changes
    for s in SEED_PATTERNS:
        conn.execute(
            _INSERT_SQL,
            (s["pattern"], s.get("regex"), s["weight"], s["category"]),
        )
    conn.commit()
    return conn.total_changes - before
