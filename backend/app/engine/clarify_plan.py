"""推断"生成出来的 Agent 该先问用户什么"。

为什么需要这一层：用户给的是"帮我规划成都 3 天，预算 3000"，缺出发地和日期。
直接跑，模型会替你猜返程时间、酒店位置 —— 猜错了不报错，只是结果不能用。
所以生成物里要带一份"缺口清单 + 从原话里读已有值的规则"。

这里全是确定性规则（0 token）：领域画像给槽位，正则负责判断"用户已经说了"。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.schemas.requirement import RequirementSpec


@dataclass(frozen=True)
class SlotSpec:
    """一个需要向用户确认的信息槽。

    patterns 是"用户已经说了"的判定规则：命中就不再问。
    """

    id: str
    label: str
    question: str
    why: str
    patterns: tuple[str, ...] = ()
    options: tuple[str, ...] = ()
    required: bool = True


@dataclass(frozen=True)
class DomainSlots:
    key: str
    keywords: tuple[str, ...]
    slots: tuple[SlotSpec, ...]
    #: 用来生成回归测试和 notebook 示例：一段"只说了部分信息"的真实说法
    sample: str
    sample_has: tuple[str, ...] = field(default_factory=tuple)
    sample_missing: tuple[str, ...] = field(default_factory=tuple)


_CN = "[\u4e00-\u9fa5]"

DOMAINS: tuple[DomainSlots, ...] = (
    DomainSlots(
        key="travel",
        keywords=("旅行", "旅游", "行程", "出行", "攻略", "自驾", "差旅", "玩几天"),
        sample="帮我规划成都3天旅行，预算3000元，喜欢吃货和熊猫",
        sample_has=("days", "budget"),
        sample_missing=("origin", "dates"),
        slots=(
            SlotSpec(
                id="origin",
                label="出发地",
                question="你从哪座城市出发？",
                why="出发地决定大交通时长和首日到达时间，缺了只能按“你已经在目的地”排日程",
                patterns=(
                    rf"(?:从|由)\s*({_CN}{{2,8}})\s*(?:出发|飞|坐|开车|走)",
                    rf"出发地[：:\s]*({_CN}{{2,8}})",
                ),
            ),
            SlotSpec(
                id="dates",
                label="出行日期",
                question="大概什么时候去、什么时候回？",
                why="票价、营业时间、是否需要预约都跟日期绑定，日期不定预算只能给区间",
                patterns=(
                    r"\d{1,2}\s*月\s*\d{1,2}\s*[日号]",
                    r"\d{4}[-/]\d{1,2}[-/]\d{1,2}",
                    r"(明天|后天|下周[一二三四五六日天]?|本周[一二三四五六日天]|周末"
                    r"|五一|十一|国庆|春节|暑假|寒假|清明|端午|中秋)",
                ),
                options=("这周末", "下周末", "还没定，先给参考方案"),
            ),
            SlotSpec(
                id="days",
                label="天数",
                question="计划玩几天？",
                why="天数决定能排几个点位，也直接决定住宿晚数与预算",
                patterns=(r"(\d+)\s*天", r"(\d+)\s*日游"),
                options=("2 天", "3 天", "5 天"),
            ),
            SlotSpec(
                id="budget",
                label="预算",
                question="总预算（含大交通和住宿）大概多少？",
                why="预算决定住哪个片区、吃哪一档，是先算钱再排行程，还是先排再砍",
                patterns=(
                    rf"(?:预算|花费|人均|大概|不超过)\s*(\d+(?:\.\d+)?)\s*(?:元|块|k|K|万)?",
                    r"(\d+(?:\.\d+)?)\s*元",
                ),
                options=("2000 以内", "2000–4000", "4000 以上", "不限，给性价比方案"),
            ),
            SlotSpec(
                id="companions",
                label="同行人",
                question="几个人去？有老人或小孩吗？",
                why="带小孩/老人会改变步行强度、住宿要求和交通方式",
                patterns=(
                    r"(\d+)\s*(?:个)?人",
                    r"(一个人|独自|自己一个人|和家人|带孩子|带娃|情侣|朋友|父母|老人)",
                ),
                required=False,
            ),
            SlotSpec(
                id="preferences",
                label="偏好",
                question="有什么偏好或必须避开的？（吃、玩、节奏）",
                why="偏好决定推荐的是网红点还是本地日常，不问我只能给通用清单",
                patterns=(
                    rf"(?:喜欢|偏好|爱吃|想吃|忌口|不喜欢|不要|必去|想看)({_CN}{{0,16}})",
                ),
                required=False,
            ),
        ),
    ),
    DomainSlots(
        key="report",
        keywords=("周报", "日报", "月报", "报表", "统计", "汇总", "数据分析", "指标"),
        sample="帮我把上周的销售数据整理成周报，发给部门经理",
        sample_has=("period",),
        sample_missing=("data_file",),
        slots=(
            SlotSpec(
                id="data_file",
                label="数据文件",
                question="数据从哪来？文件路径或表名是什么？",
                why="没有真实表头，清洗规则和异常判定都是空谈",
                patterns=(
                    rf"([\w\-{_CN}]+\.(?:xlsx|xls|csv|json))",
                    r"(data/\S+)",
                    r"(?:表名|数据表|数据库)[：:\s]*(\S+)",
                ),
                options=("我放 data/ 目录里", "从数据库读，我给你连接信息"),
            ),
            SlotSpec(
                id="period",
                label="统计区间",
                question="统计哪一段时间的数据？",
                why="区间决定环比对象；不说清楚，同比环比都可能算错",
                patterns=(
                    r"(上周|本周|上月|本月|上季度|本季度|今年|去年)",
                    r"(近\s*\d+\s*(?:天|周|月))",
                    r"\d{4}\s*年\s*\d{1,2}\s*月",
                ),
                options=("上周", "本月", "近 30 天"),
            ),
            SlotSpec(
                id="audience",
                label="报告对象",
                question="报告发给谁？要什么粒度？",
                why="给老板看要结论和建议，给执行看要明细，写法完全不同",
                patterns=(rf"(?:发给|发送给|汇报给|抄送)({_CN}{{2,10}})",),
                required=False,
            ),
            SlotSpec(
                id="dimensions",
                label="分析口径",
                question="按什么维度拆？区域、产品线还是渠道？",
                why="维度决定表的形状，事后加维度基本等于重做",
                patterns=(rf"(?:按|分)({_CN}{{2,8}})(?:维度|拆分|统计|汇总)",),
                required=False,
            ),
        ),
    ),
    DomainSlots(
        key="support",
        keywords=("客服", "工单", "售后", "售前", "问答", "FAQ", "咨询", "投诉"),
        sample="做个客服助手，读取客户问题，检索客服手册回答，答不了就转人工",
        sample_has=("kb_docs", "handoff"),
        sample_missing=(),
        slots=(
            SlotSpec(
                id="kb_docs",
                label="业务语料",
                question="客服手册/退换货政策这些文档在哪？",
                why="回答质量的上限就是你给的资料；没资料只能靠模型编",
                patterns=(
                    rf"([\w\-{_CN}]+\.(?:md|txt|docx|pdf))",
                    r"(knowledge/|手册|政策|FAQ|话术|知识库)",
                ),
                options=("我放 knowledge/ 目录", "在飞书/企微文档里，我导出"),
            ),
            SlotSpec(
                id="handoff",
                label="转人工条件",
                question="什么情况下必须转人工？",
                why="转人工规则写不清楚，Agent 会在不该答的时候硬答",
                patterns=(
                    r"(转人工|人工介入|转客服|升级|工单|退款|投诉|赔偿)",
                ),
                options=("拿不准就转", "涉及退款/赔偿才转", "客户明确要求才转"),
            ),
            SlotSpec(
                id="channel",
                label="服务渠道",
                question="在哪个渠道接待客户？",
                why="渠道决定回复长度和消息格式（企微/网页/小程序不一样）",
                patterns=(r"(企微|企业微信|微信|网页|小程序|APP|邮件|钉钉|抖音)",),
                required=False,
            ),
            SlotSpec(
                id="tone",
                label="回复口径",
                question="回复语气和称呼有要求吗？",
                why="官方口径和个人化话术差别很大，事后统一比一开始就说清楚贵",
                patterns=(r"(语气|口径|称呼|正式|亲切|简洁|不要承诺)",),
                required=False,
            ),
        ),
    ),
    DomainSlots(
        key="review",
        keywords=("审核", "审查", "合规", "合同", "风控", "尽调", "校对"),
        sample="帮我审合同，重点看付款条款和违约责任",
        sample_has=(),
        sample_missing=("standard_docs",),
        slots=(
            SlotSpec(
                id="standard_docs",
                label="审核标准",
                question="按什么标准审？有制度文件或检查清单吗？",
                why="没有标准，“有风险”就是模型主观判断，法务没法用",
                patterns=(
                    rf"([\w\-{_CN}]+\.(?:md|txt|docx|pdf))",
                    # 只认"按某个制度/清单/规范审"，不认"看付款条款"——
                    # 后者只说明关注点，没说明按什么标准判定。
                    rf"(?:按|根据|按照|依据)({_CN}{{2,12}})(?:制度|规范|清单|标准|指引|法规)",
                ),
                options=("我放 knowledge/ 目录", "按公司通用合规要求"),
            ),
            SlotSpec(
                id="sample_doc",
                label="待审文件",
                question="这次要审的文件在哪？",
                why="先拿一份真实文件跑通，再批量，排错快得多",
                patterns=(rf"([\w\-{_CN}]+\.(?:pdf|docx|doc|txt))", r"(data/\S+)"),
                required=False,
            ),
            SlotSpec(
                id="threshold",
                label="风险分级",
                question="风险要怎么分级？哪些必须人工复核？",
                why="不分级的话，输出全是“建议关注”，等于没结论",
                patterns=(r"(高|中|低)\s*风险", r"(必须人工|人工复核|驳回|通过)"),
                required=False,
            ),
            SlotSpec(
                id="output_format",
                label="输出形式",
                question="结果是逐条批注，还是一份问题清单？",
                why="批注要能落回原文位置，清单只要结论，实现方式不同",
                patterns=(r"(批注|清单|表格|报告|逐条)",),
                required=False,
            ),
        ),
    ),
    DomainSlots(
        key="recruit",
        keywords=("招聘", "简历", "面试", "候选人", "筛选", "HR"),
        sample="帮我筛选简历，找出符合后端开发岗位的候选人",
        sample_has=(),
        sample_missing=("jd",),
        slots=(
            SlotSpec(
                id="jd",
                label="岗位要求",
                question="岗位 JD 或硬性要求是什么？",
                why="没有 JD，筛选标准只能靠猜，筛出来的简历没法用",
                patterns=(
                    rf"([\w\-{_CN}]+\.(?:md|txt|docx|pdf))",
                    r"(JD|岗位要求|任职要求|硬性条件)",
                ),
            ),
            SlotSpec(
                id="criteria",
                label="筛选维度",
                question="最看重哪几点？学历、年限、项目还是稳定性？",
                why="维度不同结论会翻，先定权重再筛，避免变成“看着还行”",
                patterns=(r"(学历|年限|经验|项目|稳定性|技能|证书|权重)",),
                required=False,
            ),
            SlotSpec(
                id="channel",
                label="简历来源",
                question="简历从哪来？文件夹、邮箱还是招聘系统？",
                why="来源决定解析方式（PDF/邮件/接口）",
                patterns=(r"(邮箱|邮件|招聘系统|BOSS|智联|猎聘|文件夹|data/)",),
                required=False,
            ),
        ),
    ),
)

#: 生成物里最多问几个问题 —— 一次问太多，用户会直接关掉
MAX_QUESTIONS = 4


def pick_domain(requirement: RequirementSpec) -> DomainSlots | None:
    """按需求文本命中最贴近的一个领域画像。"""
    corpus = " ".join(
        [
            requirement.agent_name,
            requirement.goal,
            *requirement.features,
            *requirement.inputs,
            *requirement.outputs,
        ]
    )
    best: tuple[int, DomainSlots] | None = None
    for domain in DOMAINS:
        hits = sum(1 for kw in domain.keywords if kw in corpus)
        if hits and (best is None or hits > best[0]):
            best = (hits, domain)
    return best[1] if best else None


def plan_slots(requirement: RequirementSpec) -> tuple[list[SlotSpec], DomainSlots | None]:
    """只返回"这次任务缺什么"的槽位。

    注意：需求里的 clarifying_questions **不进运行时**。那些问题问的是
    "渠道用对话界面还是 API"这种设计取舍 —— 在构建时该问，在用户每次
    跑任务时再问一遍就是噪音，答了也不改变这一次的结果。
    它们改由 design_questions() 输出到 README / 交付清单里。
    """
    domain = pick_domain(requirement)
    return (list(domain.slots) if domain else []), domain


def design_questions(requirement: RequirementSpec) -> list[tuple[str, str]]:
    """构建期没答、但会影响实现方式的问题：(问题, 为什么)。"""
    return [
        (q.question, q.why)
        for q in requirement.clarifying_questions
        if getattr(q, "blocking", False)
    ]


def sample_input(requirement: RequirementSpec, domain: DomainSlots | None) -> str:
    """notebook 与回归测试都用这一段，保证"演示的输入"和"测过的输入"是同一个。"""
    if domain is not None:
        return domain.sample
    if requirement.inputs:
        return f"用这条真实输入试跑：{requirement.inputs[0]}"
    return "把这里换成你的真实输入"
