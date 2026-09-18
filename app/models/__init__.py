"""数据模型（对应主方案 §8 全部 24 张表）。

- 全部 DDL 集中于此，CREATE TABLE IF NOT EXISTS 幂等风格（可重入，不丢数据）
- TABLES: 表名 -> 建表 SQL；INDEXES: 索引；TABLE_NAMES: 24 张表名清单
- ensure_schema()（app/db.py）启动时依次执行 ALL_DDL，并种入话术语料

字段约定：id 主键自增；created_at/ts 等以 TEXT 存 SQLite datetime('now')；
JSON 字段一律 TEXT 存 JSON 字符串（sqlite3 无原生 JSON 类型）。
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# 蜜饵（R1）—— 只产草稿不自动发布，发布动作由用户手动完成（D3 HITL）
# ---------------------------------------------------------------------------
TABLES: dict[str, str] = {
    "honey_facts": """
CREATE TABLE IF NOT EXISTS honey_facts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    bait_text       TEXT NOT NULL,                 -- 蜜饵文案
    fingerprint     TEXT NOT NULL UNIQUE,          -- 指纹 UUID（幂等唯一）
    disguise_score  REAL NOT NULL DEFAULT 0,       -- 伪装度 0-100（可解释，见 disguise_reason）
    status          TEXT NOT NULL DEFAULT 'draft', -- draft/active/monitored/hit/retired（状态机，P4）
    target_url      TEXT,                          -- 埋设目标（用户手动部署后填）
    deployed_at     TEXT,                          -- 用户标记 deployed 的时间
    hit_count       INTEGER NOT NULL DEFAULT 0,    -- 踩饵命中次数
    enabled         INTEGER NOT NULL DEFAULT 1,    -- 0=停用（disable，P4）
    retired_reason  TEXT,                          -- hit/user/disable 等退役原因（P4）
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
)
""",

    "speech_patterns": """
CREATE TABLE IF NOT EXISTS speech_patterns (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern     TEXT NOT NULL,                     -- 关键词/正则（LIKE 包含匹配或 regex 优先）
    regex       TEXT,                              -- 可选正则，非空时优先于 pattern
    weight      REAL NOT NULL DEFAULT 1.0,         -- 权重 0-10
    category    TEXT NOT NULL DEFAULT 'other',     -- fake_investment/pig_butchering/job_scam/refund_scam/impersonation/other
    source      TEXT NOT NULL DEFAULT 'seed',      -- seed/manual/import
    enabled     INTEGER NOT NULL DEFAULT 1,        -- 词库开关
    hit_count   INTEGER NOT NULL DEFAULT 0,        -- 命中次数（在线学习统计，P17）
    hit_history TEXT NOT NULL DEFAULT '[]',        -- JSON：命中历史归并（记忆，P5+）
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(pattern, category)                      -- 保证种子幂等入库
)
""",

    "speech_hits": """
CREATE TABLE IF NOT EXISTS speech_hits (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    det_id      INTEGER NOT NULL,                  -- -> detections.id
    pattern_id  INTEGER NOT NULL,                  -- -> speech_patterns.id
    matched_text TEXT NOT NULL,                    -- 命中的原文片段
    score       REAL NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
)
""",

    "accounts": """
CREATE TABLE IF NOT EXISTS accounts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    url_name      TEXT NOT NULL UNIQUE,            -- 知乎 url_token/昵称
    signals       TEXT NOT NULL DEFAULT '{}',      -- JSON：公开信号（activity/content_consistency/...）
    risk_level    TEXT NOT NULL DEFAULT 'green',   -- green/yellow/red
    trap_hit_ids  TEXT NOT NULL DEFAULT '[]',      -- JSON：踩饵记录 id 列表
    updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
)
""",

    "detections": """
CREATE TABLE IF NOT EXISTS detections (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    source           TEXT NOT NULL DEFAULT 'import',  -- zhihu/import/manual
    text_hash        TEXT NOT NULL,                   -- sha256（去重索引）
    content          TEXT,                            -- 原文（存储策略按配置，可脱敏）
    rule_score       REAL NOT NULL DEFAULT 0,         -- 规则引擎分
    ml_score         REAL,                            -- ML 分（可选）
    llm_verdict      TEXT,                            -- normal/suspicious/fraud
    judge_confidence REAL,                            -- 0-1
    grade            TEXT,                            -- L1-L5（P5 分级）
    grade_reason     TEXT NOT NULL DEFAULT '[]',      -- JSON：分级可解释证据链（P5）
    se_factors       TEXT NOT NULL DEFAULT '{}',      -- JSON：社会工程学推理链（队2，se_analysis）
    status           TEXT NOT NULL DEFAULT 'pending', -- pending/processed/reviewed/reported
    created_at       TEXT NOT NULL DEFAULT (datetime('now'))
)
""",

    "evidence_packages": """
CREATE TABLE IF NOT EXISTS evidence_packages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    det_ids     TEXT NOT NULL DEFAULT '[]',       -- JSON：关联检测 id 列表
    screenshots TEXT NOT NULL DEFAULT '[]',       -- JSON：截图文件路径列表
    hash_chain  TEXT NOT NULL DEFAULT '[]',       -- JSON：证据哈希链（R5）
    frozen      INTEGER NOT NULL DEFAULT 0,       -- 0/1：冻结后哈希不可变
    frozen_at   TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
)
""",

    "cases": """
CREATE TABLE IF NOT EXISTS cases (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    det_id           INTEGER,                     -- -> detections.id
    redacted_payload TEXT NOT NULL,               -- 脱敏后载荷（硬性脱敏，D7）
    desensitize_log  TEXT NOT NULL DEFAULT '[]',  -- JSON：脱敏记录 [{from,to},...]
    graph_tags       TEXT NOT NULL DEFAULT '[]',  -- JSON：骗术图谱标签（ECharts）
    published_at     TEXT NOT NULL DEFAULT (datetime('now'))
)
""",

    "events": """
CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    kind    TEXT NOT NULL,                        -- hit/trap_hit/alert/case_published/...
    payload TEXT NOT NULL DEFAULT '{}',           -- JSON
    ts      TEXT NOT NULL DEFAULT (datetime('now'))
)
""",

    "alert_rules": """
CREATE TABLE IF NOT EXISTS alert_rules (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    level      TEXT NOT NULL DEFAULT 'L3',        -- 触发级别：只对 L3+ 推送（P5 闸控）
    channel    TEXT NOT NULL DEFAULT 'webui',     -- webui/email/...
    enabled    INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
)
""",

    "alerts": """
CREATE TABLE IF NOT EXISTS alerts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_id    INTEGER,                           -- -> alert_rules.id
    level      TEXT NOT NULL DEFAULT 'L3',
    channel    TEXT NOT NULL DEFAULT 'webui',
    payload    TEXT NOT NULL DEFAULT '{}',        -- JSON
    unread     INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
)
""",

    "api_keys": """
CREATE TABLE IF NOT EXISTS api_keys (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    key_hash   TEXT NOT NULL UNIQUE,              -- sha256(key)（明文不落库，D6）
    role       TEXT NOT NULL DEFAULT 'agent',     -- admin/agent
    name       TEXT NOT NULL DEFAULT '',
    enabled    INTEGER NOT NULL DEFAULT 1,
    last_used  TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
)
""",

    "zhihu_sessions": """
CREATE TABLE IF NOT EXISTS zhihu_sessions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    channel    TEXT NOT NULL UNIQUE,               -- ch-a/ch-b/ch-c/ch-d（每通道一会话，P7 WebUI 按 channel 查）
    cookie_enc TEXT,                               -- Fernet 加密后 cookie（P2）
    status     TEXT NOT NULL DEFAULT 'inactive',   -- inactive/active/degraded
    health     TEXT NOT NULL DEFAULT 'unknown',    -- ok/degraded/down
    last_sync  TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
)
""",

    # P9：账号桥接配置表（配置账号页面）。platform 每平台一行；
    # config_enc 为 Fernet 双层加密 JSON（外层整段 + 内层敏感字段），明文不落库；
    # status 枚举：unconfigured|configured|testing|error。
    "account_bridges": """
CREATE TABLE IF NOT EXISTS account_bridges (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    platform     TEXT UNIQUE NOT NULL,                -- zhihu/wechat/qq/weibo/douyin/telegram/discord
    display_name TEXT,                                -- 平台展示名（冗余自 PLATFORMS 注册表）
    config_enc   TEXT,                                -- Fernet 加密 JSON（敏感字段双层加密），无则未配置
    status       TEXT NOT NULL DEFAULT 'unconfigured',-- unconfigured/configured/testing/error
    last_sync    TEXT,                                -- 最近一次私信同步/测试时间
    note         TEXT,                                -- 备注（如 account_intel 跳过原因）
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
)
""",

    # P10：采集矩阵表（平台 × 账号 × 策略 编排）。strategy 为 JSON 文本：
    # {collect:'dm|comments|posts|follows', frequency:'minute|hourly|daily',
    #  depth:int, keyword:str, near_dup:0.85}；enabled 1=参与定时轮询。
    "collector_matrix": """
CREATE TABLE IF NOT EXISTS collector_matrix (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    platform     TEXT NOT NULL,                     -- zhihu/weibo/telegram/wechat/qq/rsshub
    account      TEXT NOT NULL DEFAULT '',          -- 目标账号（rsshub 免账号）
    strategy     TEXT NOT NULL DEFAULT '{}',        -- JSON 采集策略
    enabled      INTEGER NOT NULL DEFAULT 1,        -- 1=启用（调度参与）
    status       TEXT NOT NULL DEFAULT 'pending',   -- pending/running/ok/error
    last_run_at  TEXT,                              -- 最近一次执行时间
    last_item_id TEXT,                              -- 最近采集项指纹（sha256，去重用）
    last_error   TEXT,                              -- 最近错误（截断 500）
    note         TEXT,                              -- 备注
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
)
""",

    # 主方案 §8 的列名为 key（SQLite 关键字），此处用 cfg_key 规避引号问题
    "configs": """
CREATE TABLE IF NOT EXISTS configs (
    cfg_key    TEXT PRIMARY KEY,                  -- 阈值/LLM 路由/降级链配置名
    value      TEXT NOT NULL DEFAULT '{}',        -- JSON
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
)
""",

    "gate_logs": """
CREATE TABLE IF NOT EXISTS gate_logs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    action       TEXT NOT NULL,                   -- 敏感操作名（confirm+reason，P3+ 校验）
    payload_hash TEXT NOT NULL DEFAULT '',        -- 哈希链节点：sha256(prev+action+before+after+reason+ts+payload_json)
    prev_hash    TEXT NOT NULL DEFAULT '',        -- 上一行 payload_hash（首行=GENESIS，P1-C6 链式承诺）
    before       TEXT NOT NULL DEFAULT '{}',      -- JSON：操作前状态
    after        TEXT NOT NULL DEFAULT '{}',      -- JSON：操作后状态
    reason       TEXT NOT NULL DEFAULT '',        -- 理由（≥20 字，P3+ 强制）
    ts           TEXT NOT NULL DEFAULT (datetime('now')),
    payload_json TEXT NOT NULL DEFAULT '{}'       -- JSON：审计载荷原文（链式校验可复核）
)
""",

    # 社工库（队5）：本地泄露库记录（明文不落库，D7 最小化）。
    # ioc_value_hash = sha256(明文)；UNIQUE 保证演示种子 INSERT OR IGNORE 幂等。
    # ioc_type: email/phone/qq/wechat/bank_card；source: local/demo/import。
    "leak_records": """
CREATE TABLE IF NOT EXISTS leak_records (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    ioc_type         TEXT NOT NULL,               -- email/phone/qq/wechat/bank_card
    ioc_value_hash   TEXT NOT NULL UNIQUE,        -- sha256(明文)，明文不落库（D7）
    source           TEXT DEFAULT 'local',        -- local/demo/import
    breach_name      TEXT,                        -- 泄露库/事件名称
    note             TEXT,                        -- 备注
    created_at       TEXT NOT NULL DEFAULT (datetime('now'))
)
""",

    "persona_schedules": """
CREATE TABLE IF NOT EXISTS persona_schedules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account TEXT NOT NULL,
    type TEXT NOT NULL,
    interval_minutes INTEGER NOT NULL,
    total INTEGER NOT NULL,
    published INTEGER NOT NULL DEFAULT 0,
    next_run_at TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
)
""",

    "persona_posts": """
CREATE TABLE IF NOT EXISTS persona_posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account TEXT NOT NULL,
    type TEXT NOT NULL,
    content TEXT NOT NULL,
    published_at TEXT NOT NULL DEFAULT (datetime('now')),
    source TEXT NOT NULL DEFAULT 'template'
)
""",

    # P16：实战化应对层——骗子私信/评论应对规则（trigger_keyword substring 匹配，
    # reply_type 选模板池角色：confused_elder/curious_newbie/cautious_prober）
    "persona_replies": """
CREATE TABLE IF NOT EXISTS persona_replies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account TEXT NOT NULL,
    trigger_keyword TEXT NOT NULL,
    reply_type TEXT NOT NULL DEFAULT 'stall',
    content TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
)
""",
    # P18：知识库蒸馏（knowledge_items 结构化情报条目 + knowledge_events 蒸馏审计）
    "knowledge_items": """
CREATE TABLE IF NOT EXISTS knowledge_items (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    subject        TEXT NOT NULL,                     -- 主题/标题
    content        TEXT NOT NULL,                     -- 正文（脱敏后落库，D7）
    source         TEXT NOT NULL DEFAULT 'manual',    -- manual/import/case/collector/web
    ktype          TEXT NOT NULL DEFAULT '其他',       -- 新骗术/案例复盘/线索/平台漏洞/法律/其他
    tags           TEXT NOT NULL DEFAULT '[]',        -- JSON 标签
    keywords       TEXT NOT NULL DEFAULT '[]',        -- JSON 蒸馏关键词
    iocs           TEXT NOT NULL DEFAULT '{}',        -- JSON 提取 IOC（掩码值）
    harm_score     REAL NOT NULL DEFAULT 1.0,         -- 危害分 1-10（基值，可人工调）
    decay_weight   REAL NOT NULL DEFAULT 1.0,         -- 时效权重 0-1（随时间衰减）
    weighted_score REAL NOT NULL DEFAULT 0.0,         -- 综合分 = harm_score × decay_weight
    enabled        INTEGER NOT NULL DEFAULT 1,        -- 0=下架
    verified       INTEGER NOT NULL DEFAULT 0,        -- 人工复核标记
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at     TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(subject, ktype)                            -- 幂等去重（冲突 → 更新）
)
""",
    "knowledge_events": """
CREATE TABLE IF NOT EXISTS knowledge_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    action       TEXT NOT NULL,                       -- import/harm_adjust/verified/disable/delete
    payload_json TEXT NOT NULL DEFAULT '{}',          -- JSON 审计载荷
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
)
""",
    # P19：反钓鱼伪装层（《蜜罐反钓鱼伪装》设计 §三）——多层伪装身份。
    # 全部字段为虚构假资料（name 唯一）；layer=parent 层+1（最外层=1）；
    # fake_* 为用于"泄露"给骗子的假联系信息；expose_strategy 主动/被动/埋饵。
    "cover_identities": """
CREATE TABLE IF NOT EXISTS cover_identities (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL UNIQUE,             -- 伪装名（校验唯一，409）
    persona_type    TEXT NOT NULL DEFAULT '受害者A',   -- 受害者A/转介绍B/朋友C
    layer           INTEGER NOT NULL DEFAULT 1,       -- 伪装层（嵌套，parent 层+1）
    parent_id       INTEGER,                          -- 上层伪装 id（NULL=最外层）
    avatar_desc     TEXT,                             -- 头像描述（虚构）
    bio_desc        TEXT,                             -- 简介（虚构）
    fake_phone      TEXT,                             -- 假手机号（虚拟号）
    fake_wechat     TEXT,                             -- 假微信号
    fake_qq         TEXT,                             -- 假QQ
    fake_email      TEXT,                             -- 假邮箱
    expose_strategy TEXT NOT NULL DEFAULT '主动',      -- 主动/被动/埋饵
    enabled         INTEGER NOT NULL DEFAULT 1,       -- 0=停用
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
)
""",
    # P19：接触事件——骗子拨打/添加伪装假信息时登记。
    # contact_value 落脱敏后值（D7，明文不落库）；layer_at=接触时伪装层；
    # escalated=1 表示同号多次接触已锁定范围（锁定分析见 CoverIdentityService）。
    "contact_events": """
CREATE TABLE IF NOT EXISTS contact_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    identity_id   INTEGER NOT NULL,                   -- -> cover_identities.id
    contact_type  TEXT NOT NULL,                      -- phone_call/wechat_add/qq_add/email
    contact_value TEXT NOT NULL,                      -- 骗子号码/账号（脱敏后，D7）
    trap_id       INTEGER,                            -- 来源蜜饵 id（可空）
    det_id        INTEGER,                            -- 来源检测 id（可空）
    layer_at      INTEGER NOT NULL DEFAULT 1,         -- 接触时伪装层
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    escalated     INTEGER NOT NULL DEFAULT 0          -- 1=已锁定升级（同值>=2次）
)
""",
}

INDEXES: dict[str, str] = {
    "idx_speech_hits_det": "CREATE INDEX IF NOT EXISTS idx_speech_hits_det ON speech_hits(det_id)",
    "idx_detections_hash": "CREATE INDEX IF NOT EXISTS idx_detections_hash ON detections(text_hash)",
    "idx_events_kind_ts": "CREATE INDEX IF NOT EXISTS idx_events_kind_ts ON events(kind, ts)",
    "idx_alerts_unread_level": "CREATE INDEX IF NOT EXISTS idx_alerts_unread_level ON alerts(unread, level)",
    "idx_collector_platform": "CREATE INDEX IF NOT EXISTS idx_collector_matrix_platform ON collector_matrix(platform)",
    "idx_knowledge_ktype": "CREATE INDEX IF NOT EXISTS idx_knowledge_ktype ON knowledge_items(ktype)",
    "idx_knowledge_weighted": "CREATE INDEX IF NOT EXISTS idx_knowledge_weighted ON knowledge_items(weighted_score)",
    "idx_knowledge_created": "CREATE INDEX IF NOT EXISTS idx_knowledge_created ON knowledge_items(created_at)",
    # P19：接触事件检索（按身份/同值锁定/时间）
    "idx_cover_contacts_identity": "CREATE INDEX IF NOT EXISTS idx_cover_contacts_identity ON contact_events(identity_id)",
    "idx_cover_contacts_value": "CREATE INDEX IF NOT EXISTS idx_cover_contacts_value ON contact_events(contact_value)",
    "idx_cover_contacts_escalated": "CREATE INDEX IF NOT EXISTS idx_cover_contacts_escalated ON contact_events(escalated)",
    # R3（P2-2）：cases 同检测唯一约束——R2 存量清洗后 0 重复/0 NULL 前置满足，
    # 与应用层 publish 查重（P1-4）双保险，杜绝任何通道再产生同 det 双案例。
    "idx_cases_det_unique": "CREATE UNIQUE INDEX IF NOT EXISTS idx_cases_det_unique ON cases(det_id)",
}

TABLE_NAMES: list[str] = list(TABLES.keys())
ALL_DDL: list[str] = list(TABLES.values()) + list(INDEXES.values())


def register_bootstrap_key(conn) -> int:
    """把 bootstrap admin key 的哈希登记进 api_keys（role='admin'），幂等。

    仅当 key 已生成时写入；key 以 sha256 哈希落库，明文不落库（D6）。
    返回 0（无 key）/1（已登记或已存在）。
    """
    from ..deps import get_admin_key
    from ..config import get_settings
    import hashlib

    key = get_admin_key(get_settings())
    if not key:
        return 0
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    conn.execute(
        "INSERT OR IGNORE INTO api_keys (key_hash, role, name) VALUES (?, 'admin', 'bootstrap')",
        (digest,),
    )
    return 1


# ---------------------------------------------------------------------------
# 社工库种子（leak_records）：3 条演示数据 + 7 条真实知名泄露事件索引。
# 幂等入库（INSERT OR IGNORE，按 ioc_value_hash UNIQUE 去重；明文不落库，只存 sha256 —— D7 最小化）。
# 合规边界：真实事件仅存公开元数据（事件名/类别/ioc 类型），不引入任何真实个人数据；
# 事件行 ioc_value_hash = sha256(事件英文名小写)，source='real-world'，note 注明公开来源。
# ---------------------------------------------------------------------------
_SEED_LEAK_RECORDS: list[tuple[str, str, str, str, str]] = [
    # ---- 演示种子（source='demo'，供测试 hit/miss 路径）----
    ("email", "victim@example.com", "演示泄露库", "演示数据：示例邮箱泄露记录", "demo"),
    ("phone", "13800138000", "演示泄露库", "演示数据：示例手机号泄露记录", "demo"),
    ("email", "admin@example.com", "撞库事件2024", "演示数据：示例管理员邮箱泄露记录", "demo"),
    # ---- 真实知名泄露事件索引（source='real-world'，只存公开元数据）----
    ("email", "adobe", "Adobe 2013", "公开报道: 2013年Adobe约4.8亿账户数据泄露（邮箱+密码哈希）", "real-world"),
    ("email", "linkedin", "LinkedIn 2012", "公开报道: 2012年LinkedIn约1.64亿账户数据泄露（邮箱+密码）", "real-world"),
    ("email", "yahoo", "Yahoo 2013-2014", "公开报道: 2013-2014年Yahoo约30亿账号数据泄露", "real-world"),
    ("email", "dropbox", "Dropbox 2012", "公开报道: 2012年Dropbox约6800万账号数据泄露", "real-world"),
    ("email", "netease-mail", "网易邮箱 2015", "公开报道: 2015年网易邮箱数亿用户邮箱数据泄露", "real-world"),
    ("email", "weibo", "新浪微博 2020", "公开报道: 2020年新浪微博约5.38亿用户数据泄露", "real-world"),
    ("email", "marriott", "万豪酒店 2018", "公开报道: 2018年万豪酒店约5亿客人数据泄露", "real-world"),
]


def seed_leak_records(conn) -> int:
    """社工库种子（幂等）：INSERT OR IGNORE，按 ioc_value_hash 唯一。返回本次写入行数。

    每条种子自带 source（demo/real-world）；真实事件行只落公开元数据，不引入个人数据。
    """
    import hashlib

    n = 0
    for ioc_type, plaintext, breach, note, source in _SEED_LEAK_RECORDS:
        digest = hashlib.sha256(plaintext.encode("utf-8")).hexdigest()
        cur = conn.execute(
            "INSERT OR IGNORE INTO leak_records (ioc_type, ioc_value_hash, source, breach_name, note) "
            "VALUES (?, ?, ?, ?, ?)",
            (ioc_type, digest, source, breach, note),
        )
        n += cur.rowcount or 0
    conn.commit()
    return n


# ---------------------------------------------------------------------------
# 增量迁移：对已存在的表追加列（SQLite 无 ADD COLUMN IF NOT EXISTS，用 PRAGMA 探测）
# 新增列模式：{表名: [(列名, 列定义), ...]}
# ---------------------------------------------------------------------------
_MIGRATIONS: dict[str, list[tuple[str, str]]] = {
    "honey_facts": [
        ("enabled", "INTEGER NOT NULL DEFAULT 1"),
        ("retired_reason", "TEXT"),
    ],
    "detections": [
        ("grade_reason", "TEXT NOT NULL DEFAULT '[]'"),   # JSON：分级可解释证据链（P5）
        ("platform", "TEXT NOT NULL DEFAULT 'other'"),    # 平台分类：zhihu/weibo/wechat/douyin/other
        ("target_url", "TEXT"),                           # 平台问题点 URL（详情 iframe 跳转）
        ("se_factors", "TEXT NOT NULL DEFAULT '{}'"),     # JSON：社会工程学推理链（队2，se_analysis）
    ],
    # P17：speech_patterns 增补 hit_count（在线学习命中计数，老库迁移补列）
    "speech_patterns": [
        ("hit_count", "INTEGER NOT NULL DEFAULT 0"),
    ],
    # P1-C6：gate_logs 链式承诺（旧库补 prev_hash/payload_json 列；ensure_migrations 随后做链回填）
    "gate_logs": [
        ("prev_hash", "TEXT NOT NULL DEFAULT ''"),
        ("payload_json", "TEXT NOT NULL DEFAULT '{}'"),
    ],
}


def ensure_migrations(conn) -> int:
    """幂等增量迁移：为老库补齐新列。返回执行的 ALTER 语句数（重入安全）。"""
    n = 0
    added_gate_cols = False
    for table, cols in _MIGRATIONS.items():
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for col, decl in cols:
            if col not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
                n += 1
                if table == "gate_logs":
                    added_gate_cols = True
    if added_gate_cols:
        # P1-C6：一次性链回填——旧行独立哈希 → 链式哈希（payload_json 缺失按空串重算）
        n += _migrate_gate_chain(conn)
    elif n:
        conn.commit()
    # 修复历史遗留的链断裂（A4）：列早已补齐的库，若链在某处断裂，做一次性回填，
    # 并用 configs 标记保证只修一次（不削弱运行期的篡改检测能力）。
    n += _repair_gate_chain_once(conn)
    # 平台分类回填：detections.platform 从 source 派生（zhihu→zhihu），幂等
    conn.execute(
        "UPDATE detections SET platform='zhihu' WHERE source='zhihu' AND platform IN ('', 'other')"
    )
    conn.commit()
    return n


def _migrate_gate_chain(conn) -> int:
    """旧 gate_logs 单行哈希 → 链式（幂等一次性；仅当新列刚补齐时调用）。"""
    from ..services.audit_log import GENESIS_HASH, _chain_input

    rows = conn.execute(
        "SELECT id, action, before, after, reason, ts, payload_json FROM gate_logs ORDER BY id"
    ).fetchall()
    if not rows:
        return 0
    prev = GENESIS_HASH
    for r in rows:
        # payload_json 用迁移后实际存储值（新列默认 '{}'），与 verify 重算口径一致
        h = __import__("hashlib").sha256(
            _chain_input(prev, action=r["action"], before=r["before"], after=r["after"],
                         reason=r["reason"], ts=r["ts"],
                         payload_json=str(r["payload_json"] or "")).encode("utf-8")
        ).hexdigest()
        conn.execute(
            "UPDATE gate_logs SET payload_hash=?, prev_hash=? WHERE id=?",
            (h, prev, r["id"]),
        )
        prev = h
    conn.commit()
    return len(rows)


def _repair_gate_chain_once(conn) -> int:
    """一次性修复历史遗留的 gate_logs 链断裂（A4），带迁移标记保证只修一次。

    - 若 configs 表已有标记 `_gate_chain_v2_repaired`，直接跳过（保留运行期篡改检测）。
    - 否则 verify 全链：链完整仅打标记；链断裂则全链回填后打标记。
    - 幂等：修复/打标后链连续，二次调用不再改动。
    """
    from ..services.audit_log import GENESIS_HASH, _chain_input, verify_gate_chain

    mark = conn.execute(
        "SELECT value FROM configs WHERE cfg_key='_gate_chain_v2_repaired'"
    ).fetchone()
    if mark is not None:
        return 0

    v = verify_gate_chain(conn)
    if v["valid"]:
        conn.execute(
            "INSERT OR IGNORE INTO configs(cfg_key, value) VALUES('_gate_chain_v2_repaired','1')"
        )
        conn.commit()
        return 0

    # 链断裂：全链重算回填（保留 action/before/after/reason/ts/payload_json 原值，只修哈希）
    import hashlib

    rows = conn.execute(
        "SELECT id, action, before, after, reason, ts, payload_json FROM gate_logs ORDER BY id"
    ).fetchall()
    prev = GENESIS_HASH
    for r in rows:
        h = hashlib.sha256(
            _chain_input(prev, action=r["action"], before=r["before"], after=r["after"],
                         reason=r["reason"], ts=r["ts"],
                         payload_json=str(r["payload_json"] or "")).encode("utf-8")
        ).hexdigest()
        conn.execute(
            "UPDATE gate_logs SET payload_hash=?, prev_hash=? WHERE id=?",
            (h, prev, r["id"]),
        )
        prev = h
    conn.execute(
        "INSERT OR IGNORE INTO configs(cfg_key, value) VALUES('_gate_chain_v2_repaired','1')"
    )
    conn.commit()
    return len(rows)