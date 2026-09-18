"""拟真账号服务（P11）：为金丝雀蜜罐生成拟真社交账号动态。

- PERSONA_TEMPLATES: 中文文案模板池（life/work/study/social/hobby 五类，每类 6-8 条），
  模板内嵌 {time}{place}{weather}{mood} 占位符，运行时随机填充变量池。
- PersonaSimService: 计划管理 + 到期发布：
  * generate_work: 随机选模板并填充变量，产出单条拟真动态
  * schedule:      新建发布计划（persona_schedules，next_run_at=now）
  * publish_due:   轮询到期计划逐条发布（persona_posts），单条异常不影响其它
  * publish_now:   手动立即发布一条并推进计划
  * list_schedules / list_posts / set_enabled: 查询与启停
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta

from ..utils import ApiError

# 时间轴格式：与 SQLite datetime('now') 同形（YYYY-MM-DD HH:MM:SS，TEXT 比较即时间序）
_FMT = "%Y-%m-%d %H:%M:%S"

# ---------------------------------------------------------------------------
# 变量池（随机填充模板占位符）
# ---------------------------------------------------------------------------
_VAR_POOLS: dict[str, list[str]] = {
    "time": ["清晨六点", "早上八点", "上午十点", "中午十二点", "下午三点",
             "傍晚六点", "晚上八点", "深夜十一点"],
    "place": ["公司", "家里", "楼下公园", "图书馆", "咖啡馆", "地铁站",
              "小区门口", "商场", "河边", "学校"],
    "weather": ["晴空万里", "细雨蒙蒙", "多云转晴", "微风习习", "凉意渐起",
                "闷热难耐", "雪后初霁", "天朗气清"],
    "mood": ["悠闲", "放松", "有点疲惫", "元气满满", "平静", "雀跃", "犯困", "专注"],
}

# ---------------------------------------------------------------------------
# 拟真文案模板池：5 类 × 6-8 条，每条含 {time}{place}{weather}{mood} 占位符
# ---------------------------------------------------------------------------
PERSONA_TEMPLATES: dict[str, list[str]] = {
    "life": [
        "{time}在{place}晒了会儿太阳，{weather}，心情{mood}，顺手把攒了半年的旧杂志整理了一遍。",
        "{time}去{place}买菜，{weather}，回家路上{place}边的小摊飘着香味，{mood}地又多买了一袋水果。",
        "{time}躺在{place}的沙发上，{weather}，整个人{mood}，什么也不想干，就发发呆。",
        "{time}在{place}做了一顿大餐，{weather}，吃饱后{mood}地瘫着，连碗都不想洗。",
        "{time}路过{place}，{weather}，突然{mood}，决定给自己买束花带回家。",
        "{time}在{place}修好了那盏坏掉的小台灯，{weather}，{mood}地很有成就感。",
        "{time}在{place}大扫除，{weather}，虽然{mood}但家里清爽多了。",
        "{time}在{place}煮了碗热汤面，{weather}，{mood}地喝完，整个人都暖了。",
    ],
    "work": [
        "{time}在{place}开会，{weather}，大家{mood}地讨论，方案总算有了眉目。",
        "{time}在{place}加班赶报表，{weather}，虽然{mood}但进度喜人。",
        "{time}在{place}写周报，{weather}，一边写一边{mood}地想：这周过得真快。",
        "{time}在{place}对接客户，{weather}，谈得{mood}，感觉合同有戏。",
        "{time}在{place}整理项目文档，{weather}，{mood}地收尾，明天可以交付了。",
        "{time}在{place}参加培训，{weather}，听得{mood}，笔记记了好几页。",
        "{time}在{place}摸鱼刷手机，{weather}，{mood}地等着下班。",
        "{time}在{place}处理积压邮件，{weather}，一封封回完，{mood}地松了口气。",
    ],
    "study": [
        "{time}在{place}背单词，{weather}，{mood}地坚持打卡第三十天。",
        "{time}在{place}看书，{weather}，读到精彩处{mood}得拍了下大腿。",
        "{time}在{place}上网课，{weather}，笔记记得{mood}，知识点终于串起来了。",
        "{time}在{place}写论文，{weather}，卡壳时{mood}，出去走了圈回来思路就通了。",
        "{time}在{place}复习错题，{weather}，{mood}地发现错题本快写满了。",
        "{time}在{place}练字，{weather}，一笔一画{mood}地写，心静了不少。",
        "{time}在{place}准备考试，{weather}，模拟卷做下来{mood}，正确率比上周高了一截。",
    ],
    "social": [
        "{time}和几个朋友在{place}聚餐，{weather}，聊得{mood}，差点忘了时间。",
        "{time}在{place}偶遇老同学，{weather}，寒暄几句{mood}地加了微信。",
        "{time}在{place}参加聚会，{weather}，认识了不少新朋友，{mood}地交换了联系方式。",
        "{time}在{place}给朋友过生日，{weather}，吹蜡烛时大家{mood}地起哄。",
        "{time}在{place}陪爸妈散步，{weather}，听他们唠叨家常，{mood}地觉得这样挺好。",
        "{time}在{place}帮邻居搬东西，{weather}，忙完{mood}地收到一顿感谢饭。",
        "{time}在{place}约了老朋友喝茶，{weather}，多年不见仍{mood}，话匣子一开就收不住。",
        "{time}在{place}当志愿者，{weather}，帮到别人{mood}，累也值得。",
    ],
    "hobby": [
        "{time}在{place}拍了一组照片，{weather}，光线好得让人{mood}。",
        "{time}在{place}跑步，{weather}，跑到五公里{mood}，酣畅淋漓。",
        "{time}在{place}画画，{weather}，涂涂抹抹{mood}地画了一下午。",
        "{time}在{place}弹吉他，{weather}，新曲子练得{mood}，能完整弹下来了。",
        "{time}在{place}养的绿植冒了新芽，{weather}，看着{mood}，成就感满满。",
        "{time}在{place}打羽毛球，{weather}，球馆里{mood}地打了两个小时。",
        "{time}在{place}拼模型，{weather}，零件一个个装好，{mood}地拍了张完成照。",
        "{time}在{place}钓鱼，{weather}，半天没口也{mood}，图的就是这份安静。",
    ],
}

# ---------------------------------------------------------------------------
# 实战应对模板池（P16）：扮演易骗角色应对骗子（困惑老人/好奇小白/谨慎试探），
# reply_type 对应关系：confused_elder / curious_newbie / cautious_prober。
# respond 命中规则后从对应角色池随机取一条，与规则 content 拼接成实战回复
# （仅生成建议话术，真实发送由用户 HITL 手动完成——合规边界）。
# ---------------------------------------------------------------------------
REPLY_TEMPLATES: dict[str, list[str]] = {
    "confused_elder": [  # 困惑老人：装糊涂拖延，诱导骗子重复/暴露更多细节
        "哎哟小伙子，你说的这些我一句没听懂，我儿子又不在家，你能不能再讲细一点？",
        "我今年都六十多了，手机上那些东西我按不明白，你说的是点哪个？",
        "听你这么说我有点慌，可我家里的钱都在卡里，动一下我得先问问老伴儿……",
        "你说稳赚不赔？我以前让骗子骗过一次，这回我可不敢随便转钱，你到底是哪家公司的？",
    ],
    "curious_newbie": [  # 好奇小白：问东问西、装感兴趣，诱导骗子暴露操作手法
        "真的假的？我前几天刚听人说过这个，具体怎么弄？要下什么软件吗？",
        "我有点心动，但怕麻烦，你一步步教我，先要填什么信息？",
        "这个佣金/收益是怎么算的？能先发我个截图看看别人拿到钱的样子吗？",
        "我以前没弄过这些，你能发个操作流程给我吗？我按你说的来。",
    ],
    "cautious_prober": [  # 谨慎试探：将信将疑，套取骗子账号/收款方式/身份信息
        "我怎么知道你是真是假？你把公司名字、工号或者执照发我核实一下？",
        "转账的话是转到个人账户还是对公账户？能先给我个合同看看吗？",
        "我有个做律师的朋友，我先问问他再决定，你先把你的联系方式留给我。",
        "你让我下载的这个软件在应用商店搜得到吗？网址是正规的那种吗？",
    ],
}

# ---------------------------------------------------------------------------
# 内置实战演练剧本（P16）：4 类对抗剧本（杀猪盘/刷单/冒充公检法/投资理财）。
# 每类含 场景说明 / 典型骗子来信 / 建议应对话术 / 风险警告 / IOC 提示 / 推理。
# ---------------------------------------------------------------------------
DRILL_SCRIPTS: list[dict] = [
    {
        "scenario": "杀猪盘",
        "description": "以婚恋/交友名义建立感情信任，诱导向虚假平台投资充值，前期小额返利，后期无法提现。",
        "typical_incoming": "亲爱的，我在这边做外汇好几年了，稳赚不赔，你也注册一个，我带你操作。",
        "suggested_reply": "谢谢你的好意，但我对投资一窍不通，而且我听说网上有很多骗局。你能先把平台名字、营业执照和你的真实身份发我吗？我核实一下再考虑。",
        "warning": "警惕'稳赚不赔/带你赚钱'话术；任何让你先充值后返利的平台都是骗局；不要向陌生账户转账。",
        "iocs_hint": "平台域名 / 收款银行卡号 / 手机号 / 微信号",
        "reasoning": "杀猪盘靠感情铺垫降低戒心：不拒绝但要求身份与平台资质核验，可诱骗对方留下账户与联系方式作为 IOC。",
    },
    {
        "scenario": "刷单",
        "description": "以'刷单返佣/做任务赚钱'为饵，先小额返利建立信任，再诱导垫付大额资金后失联。",
        "typical_incoming": "在家就能赚钱，刷一单返 8 块，多刷多返，加微信 132xxx 领任务。",
        "suggested_reply": "这个怎么保证返钱？我以前被套路过，你先说清楚钱转到哪个账户，我考虑一下。",
        "warning": "所有先垫付后返佣的'刷单/做任务'都是诈骗；不要扫描不明二维码、不要下载不明 APP。",
        "iocs_hint": "任务群号 / 收款二维码 / 微信号 / 手机号",
        "reasoning": "刷单盘用'小利诱饵'开场：顺势追问结算账户与返佣规则，可套取收款二维码与群号等 IOC。",
    },
    {
        "scenario": "冒充公检法",
        "description": "冒充公安/检察院/法院，以'涉嫌洗钱/通缉'恐吓，要求转'安全账户'或索要验证码。",
        "typical_incoming": "你好，这里是XX市公安局，你名下银行卡涉嫌洗钱案，请配合调查，把钱转入安全账户。",
        "suggested_reply": "警官您好，我有点慌，但我记一下您的警号和单位，我先让家人陪我去派出所核实一下，可以吗？",
        "warning": "公检法不会电话办案、不会要求转账'安全账户'；接到此类电话直接挂断并拨打 96110 核实。",
        "iocs_hint": "冒充警号 / 所谓'安全账户'卡号 / 来电号码",
        "reasoning": "冒充公检法靠恐吓施压：假装配合但要求对方留警号/单位，拖延并诱导骗子暴露身份线索。",
    },
    {
        "scenario": "投资理财",
        "description": "以'高收益理财/内幕消息/虚拟币'为饵，诱导下载虚假 APP 投资，前期返利后跑路。",
        "typical_incoming": "老师带单，稳赚不赔，下载这个 APP 注册就能领 1888 体验金，抓紧上车。",
        "suggested_reply": "这个 APP 我在应用商店怎么搜不到？收益这么高有点吓人，你先发我公司资质和过往案例看看。",
        "warning": "高收益必然高风险；虚假理财 APP 通常不在应用商店上架，仅通过链接下载；提现困难立即停止并报警。",
        "iocs_hint": "APP 下载链接 / 充值收款账户 / 邀请码 / 微信号",
        "reasoning": "投资盘用'高收益+限时'制造冲动：质疑 APP 正规性与资质，可套取下载链接与充值账户等 IOC。",
    },
]


class PersonaSimService:
    """拟真账号服务：模板生成 + 发帖计划调度（persona_schedules / persona_posts）。"""

    # ------------------------------------------------------------------
    # 生成
    # ------------------------------------------------------------------
    def generate_work(self, type: str | None = None) -> dict:
        """随机生成一条拟真动态。

        type 为 None 或不在模板池时随机选类；返回 {type, content, source:'template'}。
        """
        if type not in PERSONA_TEMPLATES:
            type = random.choice(list(PERSONA_TEMPLATES.keys()))
        template = random.choice(PERSONA_TEMPLATES[type])
        content = template.format(
            time=random.choice(_VAR_POOLS["time"]),
            place=random.choice(_VAR_POOLS["place"]),
            weather=random.choice(_VAR_POOLS["weather"]),
            mood=random.choice(_VAR_POOLS["mood"]),
        )
        return {"type": type, "content": content, "source": "template"}

    # ------------------------------------------------------------------
    # 计划
    # ------------------------------------------------------------------
    def schedule(self, account: str, type: str, interval_minutes: int, total: int, conn) -> dict:
        """新建发布计划：INSERT persona_schedules（next_run_at=now），返回计划 dict。"""
        now = datetime.now().strftime(_FMT)
        cur = conn.execute(
            "INSERT INTO persona_schedules "
            "(account, type, interval_minutes, total, published, next_run_at, enabled) "
            "VALUES (?, ?, ?, ?, 0, ?, 1)",
            (account, type, interval_minutes, total, now),
        )
        conn.commit()
        return {
            "id": cur.lastrowid,
            "account": account,
            "type": type,
            "interval_minutes": interval_minutes,
            "total": total,
            "published": 0,
            "next_run_at": now,
            "enabled": 1,
        }

    def publish_due(self, conn) -> int:
        """发布所有到期计划（enabled=1 且 next_run_at<=now 且 published<total）。

        逐条 generate_work + INSERT persona_posts + published+1 + next_run_at=now+interval；
        单条异常回滚并跳过，不影响其它计划。返回本次成功发布数。
        """
        now = datetime.now().strftime(_FMT)
        due = conn.execute(
            "SELECT * FROM persona_schedules "
            "WHERE enabled=1 AND next_run_at<=? AND published<total",
            (now,),
        ).fetchall()
        published = 0
        for row in due:
            try:
                work = self.generate_work(row["type"])
                conn.execute(
                    "INSERT INTO persona_posts (account, type, content, published_at, source) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (row["account"], work["type"], work["content"], now, work["source"]),
                )
                next_run = (datetime.now() + timedelta(minutes=row["interval_minutes"])).strftime(_FMT)
                conn.execute(
                    "UPDATE persona_schedules SET published=published+1, next_run_at=? WHERE id=?",
                    (next_run, row["id"]),
                )
                conn.commit()
                published += 1
            except Exception:
                conn.rollback()
                continue
        return published

    def publish_now(self, schedule_id: int, conn) -> dict:
        """手动立即发布一条并推进计划，返回 {发布记录 + 计划最新状态}。

        P2：published 达 total 上限不再发布，抛 ApiError('persona_limit_reached', 409)。
        """
        row = conn.execute(
            "SELECT * FROM persona_schedules WHERE id=?", (schedule_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"persona schedule not found: {schedule_id}")
        if row["published"] >= row["total"]:
            raise ApiError(
                "persona_limit_reached",
                f"发布计划已达上限: id={schedule_id} "
                f"(published={row['published']}/{row['total']})",
                409,
            )
        now = datetime.now().strftime(_FMT)
        work = self.generate_work(row["type"])
        cur = conn.execute(
            "INSERT INTO persona_posts (account, type, content, published_at, source) "
            "VALUES (?, ?, ?, ?, ?)",
            (row["account"], work["type"], work["content"], now, work["source"]),
        )
        next_run = (datetime.now() + timedelta(minutes=row["interval_minutes"])).strftime(_FMT)
        conn.execute(
            "UPDATE persona_schedules SET published=published+1, next_run_at=? WHERE id=?",
            (next_run, schedule_id),
        )
        conn.commit()
        return {
            "post_id": cur.lastrowid,
            "account": row["account"],
            "type": work["type"],
            "content": work["content"],
            "published_at": now,
            "source": work["source"],
            "schedule_id": schedule_id,
            "published": row["published"] + 1,
            "next_run_at": next_run,
        }

    # ------------------------------------------------------------------
    # 查询 / 启停
    # ------------------------------------------------------------------
    def list_schedules(self, conn) -> list[dict]:
        """返回全部发布计划（按 id 升序）。"""
        rows = conn.execute("SELECT * FROM persona_schedules ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    def list_posts(self, account: str, conn) -> list[dict]:
        """返回指定账号已发布动态（按 id 倒序，最新在前）。"""
        rows = conn.execute(
            "SELECT * FROM persona_posts WHERE account=? ORDER BY id DESC", (account,)
        ).fetchall()
        return [dict(r) for r in rows]

    def set_enabled(self, schedule_id: int, enabled: int, conn) -> dict:
        """启停计划（enabled: 0/1），返回更新后的计划 dict；不存在则抛 ValueError。"""
        cur = conn.execute(
            "UPDATE persona_schedules SET enabled=? WHERE id=?", (1 if enabled else 0, schedule_id)
        )
        if cur.rowcount == 0:
            conn.rollback()
            raise ValueError(f"persona schedule not found: {schedule_id}")
        conn.commit()
        row = conn.execute(
            "SELECT * FROM persona_schedules WHERE id=?", (schedule_id,)
        ).fetchone()
        return dict(row)

    # ------------------------------------------------------------------
    # P16：实战应对层 —— 应对规则 CRUD + respond 匹配引擎 + 实战演练
    # ------------------------------------------------------------------
    def add_reply(self, account: str, trigger_keyword: str, reply_type: str,
                  content: str, conn) -> dict:
        """新增应对规则（persona_replies，enabled=1），返回 {reply_id}。"""
        cur = conn.execute(
            "INSERT INTO persona_replies "
            "(account, trigger_keyword, reply_type, content, enabled) "
            "VALUES (?, ?, ?, ?, 1)",
            (account, trigger_keyword, reply_type, content),
        )
        conn.commit()
        return {"reply_id": cur.lastrowid}

    def list_replies(self, conn) -> list[dict]:
        """全部应对规则（id 升序）。"""
        rows = conn.execute("SELECT * FROM persona_replies ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    def delete_reply(self, reply_id: int, conn) -> bool:
        """删除应对规则，返回是否删除成功（不存在返回 False）。"""
        cur = conn.execute("DELETE FROM persona_replies WHERE id=?", (reply_id,))
        conn.commit()
        return cur.rowcount > 0

    def set_reply_enabled(self, reply_id: int, enabled: int, conn) -> dict:
        """启停应对规则（enabled: 0/1），返回更新后的规则 dict；不存在抛 ValueError。"""
        cur = conn.execute(
            "UPDATE persona_replies SET enabled=? WHERE id=?",
            (1 if enabled else 0, reply_id),
        )
        if cur.rowcount == 0:
            conn.rollback()
            raise ValueError(f"persona reply not found: {reply_id}")
        conn.commit()
        row = conn.execute(
            "SELECT * FROM persona_replies WHERE id=?", (reply_id,)
        ).fetchone()
        return dict(row)

    def respond(self, account: str, incoming_text: str, conn) -> dict:
        """实战应对：匹配启用规则生成回复 + 提取 IOC。

        - 匹配：trigger_keyword 在 incoming_text 中 substring 命中（多条命中取
          最长 trigger_keyword，更特异；长度相同取 id 小者）；
        - 命中：从 REPLY_TEMPLATES[reply_type] 角色池随机取一条 + 规则 content
          拼接成回复，并提取 incoming_text 的 IOC；
        - 未命中：防御式返回 {matched:false, reply:'', reasoning:'未命中应对规则'}。
        """
        from .soc_lib import SocLibService

        text = (incoming_text or "").strip()
        iocs = SocLibService().extract_iocs(text)
        rules = conn.execute(
            "SELECT * FROM persona_replies WHERE account=? AND enabled=1 "
            "ORDER BY length(trigger_keyword) DESC, id ASC",
            (account,),
        ).fetchall()
        hit = None
        for r in rules:
            if r["trigger_keyword"] and r["trigger_keyword"] in text:
                hit = r
                break
        if hit is None:
            return {
                "matched": False,
                "reply": "",
                "reasoning": "未命中应对规则",
                "extracted_iocs": iocs,
            }
        pool = REPLY_TEMPLATES.get(hit["reply_type"]) or REPLY_TEMPLATES["cautious_prober"]
        role_line = random.choice(pool)
        reply = f"{hit['content']}\n{role_line}"
        labels = {"confused_elder": "困惑老人", "curious_newbie": "好奇小白",
                  "cautious_prober": "谨慎试探"}
        label = labels.get(hit["reply_type"], hit["reply_type"])
        return {
            "matched": True,
            "reply": reply,
            "reasoning": (
                f"命中应对规则(id={hit['id']}, trigger='{hit['trigger_keyword']}', "
                f"reply_type='{hit['reply_type']}'/{label})：以{label}人设拖延并套取信息，"
                f"已提取 {sum(1 for v in iocs.values() if v)} 类 IOC"
            ),
            "extracted_iocs": iocs,
        }

    def drills(self) -> dict:
        """内置 4 类实战演练剧本（静态数据）：返回
        {drills:[{scenario, description, typical_incoming, suggested_reply,
        warning, iocs_hint, reasoning}]}。"""
        return {"drills": [dict(s) for s in DRILL_SCRIPTS]}

    def drill(self, scenario: str, incoming_text: str | None = None, conn=None) -> dict:
        """实战演练：按 scenario 返回剧本 suggested_reply/warning/reasoning；
        若提供 incoming_text 则额外 extract_iocs 并补充推理；未知场景抛 ValueError。"""
        from .soc_lib import SocLibService

        scripts = {s["scenario"]: s for s in DRILL_SCRIPTS}
        if scenario not in scripts:
            raise ValueError(f"未知演练场景: {scenario}")
        s = scripts[scenario]
        result = {
            "scenario": s["scenario"],
            "suggested_reply": s["suggested_reply"],
            "warning": s["warning"],
            "reasoning": s["reasoning"],
        }
        if incoming_text and incoming_text.strip():
            iocs = SocLibService().extract_iocs(incoming_text)
            result["incoming_text"] = incoming_text.strip()
            result["extracted_iocs"] = iocs
            n = sum(1 for v in iocs.values() if v)
            result["reasoning"] = f"{s['reasoning']} 已对输入文本提取 {n} 类 IOC（{iocs}）。"
        return result