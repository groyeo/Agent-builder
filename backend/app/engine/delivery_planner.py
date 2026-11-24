"""交付策划器：从设计产物推导出"跑起来还差什么"。

输入是需求/架构/组件（都已经结构化），输出是一份可执行的清单：
  1. 要准备哪些外部资源（Key、语料、数据文件、账号）；
  2. 每项去哪注册、怎么拿、填到哪、没配会怎样；
  3. 从零到跑通的分步命令。

全程不调用模型：注册流程是事实，让模型复述只会得到看起来对的错误 URL。
匹配靠关键词 + 组件类型（rag → 需要语料，mcp → 需要 Server 地址）。
"""

from __future__ import annotations

from app.engine.clarify_plan import pick_domain
from app.engine.service_catalog import CATALOG, ServiceEntry
from app.schemas.architecture import ArchitectureSpec
from app.schemas.components import ComponentPlan
from app.schemas.delivery import DeliveryPlan, ResourceItem, StartupStep
from app.schemas.requirement import RequirementSpec

KNOWLEDGE_KEYWORDS = (
    "知识库",
    "手册",
    "SOP",
    "sop",
    "制度",
    "FAQ",
    "faq",
    "话术",
    "产品文档",
    "资料",
    "规范",
    "条款",
    "判例",
    "常见问题",
)
DATA_KEYWORDS = (
    "Excel",
    "excel",
    "CSV",
    "csv",
    "PDF",
    "pdf",
    "上传",
    "表格",
    "简历",
    "合同",
    "发票",
    "报表",
    "明细",
    "图片",
    "附件",
)


def _corpus(
    requirement: RequirementSpec, architecture: ArchitectureSpec, plan: ComponentPlan
) -> str:
    """把所有可读文本拼一起做关键词匹配 —— 宁可多命中一条清单，也不要漏。"""
    parts: list[str] = [
        requirement.agent_name,
        requirement.goal,
        *requirement.features,
        *requirement.inputs,
        *requirement.outputs,
        *requirement.constraints,
        *requirement.quality_requirements,
        *requirement.external_dependencies,
        *requirement.acceptance_criteria,
    ]
    parts += [a.name + " " + a.responsibility for a in architecture.agents]
    parts += [f"{c.name} {c.purpose} {c.depends_on}" for c in plan.components]
    parts += [f"{c.name} {c.purpose}" for c in architecture.required_components]
    return "\n".join(parts)


def _declared_deps(requirement: RequirementSpec) -> str:
    """需求里显式声明的外部依赖：命中它们说明用户已经知道要用这个服务，算必需。"""
    return "\n".join(requirement.external_dependencies)


def _match(entry: ServiceEntry, corpus: str) -> tuple[bool, str]:
    """返回（是否命中, 命中的那个关键词），用于在 why 里写清依据。"""
    for kw in entry.keywords:
        if kw in corpus:
            return True, kw
    return False, ""


def _service_item(entry: ServiceEntry, keyword: str, required: bool) -> ResourceItem:
    return ResourceItem(
        key=entry.key,
        name=entry.name,
        kind=entry.kind,  # type: ignore[arg-type]
        required=required,
        why=f"需求/组件里出现了「{keyword}」，需要对接真实服务才能跑出结果；"
        f"否则对应工具只能停在桩实现。",
        env_vars=list(entry.env_vars),
        target=".env（项目根目录，从 .env.example 复制）",
        signup_url=entry.signup_url,
        steps=list(entry.steps),
        cost_hint=entry.cost,
        fallback=entry.fallback,
        example=entry.example,
        env_defaults=dict(entry.env_defaults),
    )


def _knowledge_item(
    requirement: RequirementSpec, plan: ComponentPlan, required: bool
) -> ResourceItem:
    rag = [c.name for c in plan.components if c.type == "rag"]
    why = (
        f"检索类组件 {', '.join(rag)} 需要真实语料才能回答得准"
        if rag
        else "Agent 的业务知识不在代码里，必须由你提供文档，否则只能靠模型自由发挥"
    )
    return ResourceItem(
        key="knowledge_base",
        name="知识库语料（你所在业务的手册/制度/FAQ）",
        kind="knowledge_doc",
        required=required,
        why=why,
        env_vars=[],
        target="knowledge/ 目录（项目里已建好，放 .md / .txt 文件，一个主题一份）",
        signup_url=None,
        steps=[
            "在项目根目录打开 knowledge/ 目录",
            "把业务文档转成纯文本（.md 或 .txt）：Word 直接用「另存为纯文本」，PDF 复制正文即可",
            "按主题拆分，一个文件讲一件事（如 退换货政策.md、产品报价.md），单文件建议不超过 3000 字",
            "文件名用中文没问题，但不要有空格和斜杠",
            "重新跑 python main.py，检索会自动读取该目录（可用 python -c \"from agent.rag import list_docs; print(list_docs())\" 确认已加载）",
        ],
        cost_hint="免费。文档质量直接决定回答质量 —— 这是最值得花时间的一项。",
        fallback="knowledge/ 为空时，检索返回空上下文，Agent 会明确说「知识库为空」而不是瞎编。",
        example="knowledge/退换货政策.md",
    )


def _data_item(requirement: RequirementSpec) -> ResourceItem:
    return ResourceItem(
        key="input_data",
        name="运行时输入的数据文件",
        kind="data_file",
        required=True,
        why="这个 Agent 的输入是文件，不是对话文本；没有样例文件就无法验证它是否真的能跑通。",
        env_vars=[],
        target="data/ 目录（推荐），或用命令行参数传绝对路径",
        signup_url=None,
        steps=[
            "准备 1~3 个真实样例文件（脱敏即可），放到 data/ 目录",
            "文件名写清场景，如 data/2026-10_销售明细.xlsx",
            "先跑通样例，再换全量数据 —— 排错时数据越小越快",
            "注意：不要把含个人信息/密钥的文件提交到 git（.gitignore 已忽略 data/）",
        ],
        cost_hint="免费。",
        fallback="data/ 为空时，main.py 会提示你先放文件。",
        example="data/样例.xlsx",
    )


def _startup_steps(
    requirement: RequirementSpec, resources: list[ResourceItem]
) -> list[StartupStep]:
    has_kb = any(r.kind == "knowledge_doc" for r in resources)
    has_data = any(r.kind == "data_file" for r in resources)
    steps = [
        StartupStep(
            order=1,
            title="一键准备（生成 .env、装依赖、自检）",
            command="python start.py --check",
            detail="Windows 也可以直接双击 run.bat。它会把 .env 从模板复制出来、装好"
            "requirements.txt、再逐项报缺 —— 不用记五条命令。",
        ),
        StartupStep(
            order=2,
            title="按上面的「必需项」把 .env 填完",
            command="python start.py --check",
            detail="每项去哪注册、变量名是什么见本文件前半部分。填一点就重跑一次上面的命令，"
            "它会告诉你还缺哪个；不用一次填完再试。",
        ),
    ]
    if has_kb:
        steps.append(
            StartupStep(
                order=len(steps) + 1,
                title="放业务知识库",
                command=None,
                detail="把手册/制度/FAQ 转成 .md 或 .txt 放进 knowledge/，一个主题一个文件。",
            )
        )
    if has_data:
        steps.append(
            StartupStep(
                order=len(steps) + 1,
                title="放样例数据",
                command=None,
                detail="把 1~3 个脱敏样例文件放进 data/，先用小数据跑通再换全量。",
            )
        )
    steps += [
        StartupStep(
            order=len(steps) + 1,
            title="跑自带测试（不需要联网，验证脚手架没被改坏）",
            command="pytest -q",
            detail="测试用假模型/假工具，全部应通过。这一步失败说明代码被改坏了，先别接真实服务。",
        ),
        StartupStep(
            order=len(steps) + 1,
            title="打开网页版（点开就能用）",
            command="python start.py --web",
            detail="会起一个只监听本机的网页并自动打开浏览器，直接在页面对话，不用记命令。"
            "Windows 双击 run.bat 等价。端口被占用会自动往后找；"
            "想换端口加 --port 9000。",
        ),
        StartupStep(
            order=len(steps) + 2,
            title="试跑一次真实任务（缺信息会先反问你）",
            command='python start.py "把你的真实输入放这里"',
            detail="它会先检查这句话缺什么：已经说过的跳过，缺的最多问 4 个，答完才生成。"
            "不想被问就加 --no-ask，缺的信息会列出来而不是悄悄猜。",
        ),
        StartupStep(
            order=len(steps) + 3,
            title="在 Jupyter 里交互调试",
            command="jupyter lab notebook.ipynb",
            detail="notebook.ipynb 已接好 handle()，运行第二个单元格就会弹输入框问你。"
            "没装就 pip install jupyterlab。",
        ),
        StartupStep(
            order=len(steps) + 4,
            title="把桩实现接成真实调用",
            command=None,
            detail="在 agent/tools.py 里找到 raise NotImplementedError 的函数，"
            "按 DELIVERY.md 对应条目调真实 API；每接一个就补一条测试。",
        ),
    ]
    return [
        s.model_copy(update={"order": i + 1}) for i, s in enumerate(steps)
    ]


#: 领域 → 一定会用到、但用户原话不一定提到的服务
DOMAIN_SERVICES: dict[str, tuple[tuple[str, str, bool], ...]] = {
    "travel": (
        (
            "amap",
            "行程要算距离、营业时间和换乘：没有地图 Key，只能给「建议去某地」，"
            "给不出「几点出发、怎么走、要多久」，返程赶不赶得上也无从判断。",
            False,
        ),
    ),
    "report": (),
    "support": (),
    "review": (),
    "recruit": (),
}


def plan_delivery(
    requirement: RequirementSpec,
    architecture: ArchitectureSpec,
    plan: ComponentPlan,
) -> DeliveryPlan:
    corpus = _corpus(requirement, architecture, plan)
    declared = _declared_deps(requirement)
    has_mcp = any(c.type == "mcp" for c in plan.components)
    has_rag = any(c.type == "rag" for c in plan.components)

    resources: list[ResourceItem] = []

    # 1) 模型：恒定必需
    llm = CATALOG[0]
    resources.append(
        ResourceItem(
            key=llm.key,
            name=llm.name,
            kind="llm",
            required=True,
            why="所有节点都要调用模型；没有 Key 时程序启动即失败。",
            env_vars=list(llm.env_vars),
            target=".env 的 OPENAI_BASE_URL / OPENAI_API_KEY / OPENAI_MODEL",
            signup_url=llm.signup_url,
            steps=list(llm.steps),
            cost_hint=llm.cost,
            fallback=llm.fallback,
            example=llm.example,
            env_defaults=dict(llm.env_defaults),
        )
    )

    # 2) 第三方服务
    for entry in CATALOG[1:]:
        if entry.key == "mcp_server":
            if not has_mcp:
                continue
            resources.append(_service_item(entry, "MCP 组件", True))
            continue
        hit, keyword = _match(entry, corpus)
        if not hit:
            continue
        # 目录里标了 optional 的（邮件、对象存储、向量库…）默认可降级；
        # 但需求里显式声明为外部依赖的，说明业务上离不开它，升为必需。
        required = (not entry.optional) or (entry.key in declared)
        resources.append(_service_item(entry, keyword, required))

    # 3) 领域隐含的服务：用户只说"推荐交通方式"，但旅行 Agent 没有地图就只能给
    # 文字建议。这类"你没想到但一定会缺"的东西，清单要主动提醒。
    domain = pick_domain(requirement)
    if domain is not None:
        for key, why, required in DOMAIN_SERVICES.get(domain.key, ()):
            if any(r.key == key for r in resources):
                continue
            entry = next((e for e in CATALOG if e.key == key), None)
            if entry is None:  # pragma: no cover - 目录被改坏时才会发生
                continue
            resources.append(
                ResourceItem(
                    key=entry.key,
                    name=entry.name,
                    kind=entry.kind,  # type: ignore[arg-type]
                    required=required,
                    why=why,
                    env_vars=list(entry.env_vars),
                    target=".env 的 " + " / ".join(entry.env_vars),
                    signup_url=entry.signup_url,
                    steps=list(entry.steps),
                    cost_hint=entry.cost,
                    fallback=entry.fallback,
                    example=entry.example,
                    env_defaults=dict(entry.env_defaults),
                )
            )

    # 4) 知识库 / 数据文件
    if has_rag or any(kw in corpus for kw in KNOWLEDGE_KEYWORDS):
        resources.append(_knowledge_item(requirement, plan, required=has_rag))
    if any(kw in corpus for kw in DATA_KEYWORDS):
        resources.append(_data_item(requirement))

    # 4) 清单顺序：必需在前，同类保持目录顺序
    resources.sort(key=lambda r: (not r.required, r.kind != "llm"))

    blockers = [
        f"{r.name}（{' / '.join(r.env_vars) if r.env_vars else r.target}）"
        for r in resources
        if r.required
    ]
    needs_env = [r for r in resources if r.env_vars]
    summary = (
        f"要把 {requirement.agent_name} 跑起来，你需要准备 {len(resources)} 项东西："
        f"{len([r for r in resources if r.required])} 项必需、"
        f"{len(resources) - len([r for r in resources if r.required])} 项可后补。"
        f"其中 {len(needs_env)} 项要填进 .env，其余是目录与文件。"
    )

    return DeliveryPlan(
        summary=summary,
        resources=resources,
        startup_steps=_startup_steps(requirement, resources),
        blockers=blockers,
    )
