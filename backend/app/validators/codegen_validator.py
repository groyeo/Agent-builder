"""生成代码的机器校验。

原则：**生成完就自检**，而不是让用户踩坑。这里不评价代码好坏（那是人的事），
只检查那些"一定跑不起来"的硬问题：语法错误、YAML 坏掉、节点没落地、
组件被吞掉、路径重复。
"""

from __future__ import annotations

import ast
import json

import yaml

from app.schemas.architecture import ArchitectureSpec, ValidationIssue, ValidationReport
from app.schemas.codegen import CodegenResult
from app.schemas.components import ComponentPlan


def validate(
    result: CodegenResult,
    architecture: ArchitectureSpec,
    plan: ComponentPlan,
) -> ValidationReport:
    issues: list[ValidationIssue] = []
    by_path = {f.path: f for f in result.files}

    # G001 路径唯一
    if len(by_path) != len(result.files):
        issues.append(
            ValidationIssue(
                level="error",
                code="G001",
                message="生成结果存在重复文件路径，打包会互相覆盖",
            )
        )

    # G002 Python 文件必须能过 AST 解析（比"看起来像代码"更可靠）
    for f in result.files:
        if f.kind != "python":
            continue
        try:
            ast.parse(f.content, filename=f.path)
        except SyntaxError as exc:
            issues.append(
                ValidationIssue(
                    level="error",
                    code="G002",
                    message=f"{f.path} 语法错误：第 {exc.lineno} 行 {exc.msg}",
                    target=f.path,
                )
            )

    # G003 YAML 必须能解析
    for f in result.files:
        if f.kind != "yaml":
            continue
        try:
            yaml.safe_load(f.content)
        except yaml.YAMLError as exc:
            issues.append(
                ValidationIssue(
                    level="error",
                    code="G003",
                    message=f"{f.path} 不是合法 YAML：{str(exc)[:120]}",
                    target=f.path,
                )
            )

    # G004 每个架构节点都要有提示词
    prompts = by_path.get("agent/prompts.py")
    if prompts is None:
        issues.append(
            ValidationIssue(
                level="error",
                code="G004",
                message="缺少 agent/prompts.py，节点无法获得行为约束",
            )
        )
    else:
        missing = [a.id for a in architecture.agents if a.id not in prompts.content]
        if missing:
            issues.append(
                ValidationIssue(
                    level="error",
                    code="G004",
                    message=f"以下节点没有生成提示词：{', '.join(missing)}",
                    target="agent/prompts.py",
                )
            )

    # G005 每个架构节点都要有编排步骤函数
    router = by_path.get("agent/router.py")
    if router is None:
        issues.append(
            ValidationIssue(
                level="error",
                code="G005",
                message="缺少 agent/router.py，架构无法执行",
            )
        )
    else:
        missing_steps = [
            a.id for a in architecture.agents if f"def _step_{a.id}(" not in router.content
        ]
        if missing_steps:
            issues.append(
                ValidationIssue(
                    level="error",
                    code="G005",
                    message=f"编排里缺少节点步骤：{', '.join(missing_steps)}",
                    target="agent/router.py",
                )
            )

    # G006 tool / mcp / rag / memory 组件必须有落地文件
    required_files = {
        "tool": "agent/tools.py",
        "mcp": "agent/mcp_client.py",
        "rag": "agent/rag.py",
        "memory": "agent/memory.py",
        "harness": "agent/harness.py",
        "skill": "agent/skills.py",
    }
    for comp in plan.components:
        target = required_files.get(comp.type)
        if target and target not in by_path:
            issues.append(
                ValidationIssue(
                    level="error",
                    code="G006",
                    message=f"组件 {comp.name}（{comp.type}）没有对应文件 {target}",
                    target=comp.name,
                )
            )
            continue
        if target and comp.type == "tool" and comp.name not in by_path[target].content:
            issues.append(
                ValidationIssue(
                    level="error",
                    code="G006",
                    message=f"工具 {comp.name} 未写入 {target}",
                    target=comp.name,
                )
            )

    # G007 有测试文件，否则"可测试"是空话
    if not any(f.path.startswith("tests/") for f in result.files):
        issues.append(
            ValidationIssue(
                level="warning",
                code="G007",
                message="没有生成任何测试文件，组件设计里的 test_requirements 无处落地",
            )
        )

    # G008 不能有空文件
    for f in result.files:
        if not f.content.strip():
            issues.append(
                ValidationIssue(
                    level="error",
                    code="G008",
                    message=f"{f.path} 是空文件",
                    target=f.path,
                )
            )

    # G009 README 要能说清怎么跑（否则交付物不完整）
    # 现在推荐用 `python start.py --check`（内部会 pip install），因此两者都算数
    readme = by_path.get("README.md")
    install_hints = ("pip install", "start.py --check", "requirements.txt")
    if readme is not None and not any(h in readme.content for h in install_hints):
        issues.append(
            ValidationIssue(
                level="warning",
                code="G009",
                message="README 缺少安装步骤，接手的人不知道从哪开始",
                target="README.md",
            )
        )

    # G010 config.yaml 里的节点要与架构一致
    config = by_path.get("config.yaml")
    if config is not None:
        try:
            data = yaml.safe_load(config.content) or {}
            declared = {n.get("id") for n in data.get("nodes", []) if isinstance(n, dict)}
        except yaml.YAMLError:
            declared = set()
        expected = {a.id for a in architecture.agents}
        if declared and declared != expected:
            issues.append(
                ValidationIssue(
                    level="warning",
                    code="G010",
                    message=f"config.yaml 的节点与架构不一致，多出 {declared - expected or '无'}，缺少 {expected - declared or '无'}",
                    target="config.yaml",
                )
            )

    # G011 单命令启动：start.py 缺失等于让用户回到"敲五条命令"的年代
    start = by_path.get("start.py")
    if start is None:
        issues.append(
            ValidationIssue(
                level="error",
                code="G011",
                message="缺少 start.py：用户得手动补 .env、装依赖、自检，跑起来门槛太高",
                target="start.py",
            )
        )
    else:
        for flag in ("--check", "ensure_deps", "ensure_env"):
            if flag not in start.content:
                issues.append(
                    ValidationIssue(
                        level="error",
                        code="G011",
                        message=f"start.py 缺少 {flag}：一键启动会漏掉一步",
                        target="start.py",
                    )
                )
        for path in ("run.bat", "run.sh"):
            if path not in by_path:
                issues.append(
                    ValidationIssue(
                        level="warning",
                        code="G011",
                        message=f"缺少 {path}：非技术用户没法双击启动",
                        target="start.py",
                    )
                )

    # G012 先提问再生成：clarify 是入口契约，缺了就只能靠模型猜用户信息
    clarify = by_path.get("agent/clarify.py")
    if clarify is None:
        issues.append(
            ValidationIssue(
                level="error",
                code="G012",
                message="缺少 agent/clarify.py：信息不全时会直接开始生成，不会反问用户",
                target="agent/clarify.py",
            )
        )
    else:
        for marker in ("def collect(", "def pending(", "SLOTS"):
            if marker not in clarify.content:
                issues.append(
                    ValidationIssue(
                        level="error",
                        code="G012",
                        message=f"agent/clarify.py 缺少 {marker}，提问流程无法被调用",
                        target="agent/clarify.py",
                    )
                )
        # 提问必须能在提示词里生效：渲染后的输入要真的带上确认结果
        router_src = by_path.get("agent/router.py")
        if router_src is not None and "collect(" not in router_src.content:
            issues.append(
                ValidationIssue(
                    level="error",
                    code="G012",
                    message="router.handle 没有接 clarify：缺口问了也传不到提示词",
                    target="agent/router.py",
                )
            )

    # G013 notebook 必须是合法 JSON，否则 Jupyter 打不开
    notebook = by_path.get("notebook.ipynb")
    if notebook is not None:
        try:
            json.loads(notebook.content)
        except json.JSONDecodeError as exc:
            issues.append(
                ValidationIssue(
                    level="error",
                    code="G013",
                    message=f"notebook.ipynb 不是合法 JSON：{exc.msg}",
                    target="notebook.ipynb",
                )
            )

    # G014 每个目标 Agent 都要有一个"点开就能用"的网页
    #      只有命令行入口，等于把不写代码的人挡在门外
    server_py = by_path.get("server.py")
    if server_py is None:
        issues.append(
            ValidationIssue(
                level="error",
                code="G014",
                message="缺少 server.py：目标 Agent 没有网页入口，只能命令行使用",
                target="server.py",
            )
        )
    elif "/api/chat" not in server_py.content:
        issues.append(
            ValidationIssue(
                level="error",
                code="G014",
                message="server.py 没有 /api/chat 接口，网页发不出消息",
                target="server.py",
            )
        )
    if "web/index.html" not in by_path:
        issues.append(
            ValidationIssue(
                level="error",
                code="G014",
                message="缺少 web/index.html：网页入口没有页面",
                target="web/index.html",
            )
        )

    # ---- 交付清单（D 系列）：清单跑不通比代码跑不通更让人卡住 ----

    # D001 清单与自检脚本必须存在，否则用户不知道要准备什么
    for path in ("DELIVERY.md", "scripts/check_env.py"):
        if path not in by_path:
            issues.append(
                ValidationIssue(
                    level="error",
                    code="D001",
                    message=f"缺少 {path}：用户拿不到「需要准备什么」的说明",
                    target=path,
                )
            )

    delivery = result.delivery
    if delivery is None:
        issues.append(
            ValidationIssue(
                level="error",
                code="D004",
                message="生成结果没有交付清单（DeliveryPlan），项目管理页无法展示缺什么",
            )
        )
    else:
        # D002 清单里要求的变量，必须真的出现在 .env.example 里，
        # 否则用户照着清单去注册了 Key，却不知道往哪个变量填
        env_text = by_path.get(".env.example").content if ".env.example" in by_path else ""
        for r in delivery.resources:
            for var in r.env_vars:
                if f"{var}=" not in env_text:
                    issues.append(
                        ValidationIssue(
                            level="error",
                            code="D002",
                            message=f"清单要求 {r.name} 的 {var}，但 .env.example 里没有这个变量",
                            target=".env.example",
                        )
                    )

        # D003 启动步骤太少说明没讲清怎么跑起来
        if len(delivery.startup_steps) < 3:
            issues.append(
                ValidationIssue(
                    level="warning",
                    code="D003",
                    message="启动步骤少于 3 步，用户可能不知道怎么从零跑通",
                    target="DELIVERY.md",
                )
            )

        # D006 启动步骤必须给出单命令入口，否则"一条命令跑起来"只是口号
        if not any("start.py" in (s.command or "") for s in delivery.startup_steps):
            issues.append(
                ValidationIssue(
                    level="warning",
                    code="D006",
                    message="启动步骤里没有出现 start.py，用户仍要自己拼多条命令",
                    target="DELIVERY.md",
                )
            )

        # D005 必需项没有任何说明，等于列了个名词
        for r in delivery.resources:
            if r.required and not r.steps:
                issues.append(
                    ValidationIssue(
                        level="warning",
                        code="D005",
                        message=f"必需项「{r.name}」没有获取步骤，用户会卡在这里",
                        target="DELIVERY.md",
                    )
                )

    return ValidationReport(
        passed=not any(i.level == "error" for i in issues), issues=issues
    )


def summary_json(result: CodegenResult) -> str:
    """便于日志/接口里快速看一眼生成规模。"""
    return json.dumps(
        {
            "project": result.project_name,
            "files": result.stats.files,
            "lines": result.stats.lines,
            "python": result.stats.python_files,
            "tests": result.stats.tests,
        },
        ensure_ascii=False,
    )
