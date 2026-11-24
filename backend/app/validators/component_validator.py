"""组件方案校验。

架构校验（architecture_validator）管的是"图讲不讲得通"，
这里管的是"组件清单能不能真的被生成出来"：

- 每个节点声明要用的工具/技能，必须有对应组件；
- 组件的 interface 必须带上注册表要求的字段，否则生成器无从下手；
- depends_on 不能指向不存在的组件，也不能成环；
- 声明为 required 却没有任何节点使用 → 大概率是凑数；
- 需求依赖外部系统却全是 prompt → 无法真正落地。
"""

from __future__ import annotations

from app.engine.component_registry import get_meta
from app.schemas.architecture import ArchitectureSpec
from app.schemas.components import (
    ComponentIssue,
    ComponentPlan,
    ComponentValidationReport,
)
from app.schemas.requirement import RequirementSpec

_RAG_HINTS = ("知识库", "FAQ", "常见问题", "检索", "文档")


def _find_dep_cycle(plan: ComponentPlan) -> list[str] | None:
    graph = {c.name: [d for d in c.depends_on] for c in plan.components}
    WHITE, GRAY, BLACK = 0, 1, 2
    color = dict.fromkeys(graph, WHITE)
    stack: list[str] = []

    def dfs(node: str) -> list[str] | None:
        color[node] = GRAY
        stack.append(node)
        for nxt in graph.get(node, []):
            if nxt not in graph:
                continue
            if color[nxt] == GRAY:
                return stack[stack.index(nxt) :] + [nxt]
            if color[nxt] == WHITE:
                found = dfs(nxt)
                if found:
                    return found
        stack.pop()
        color[node] = BLACK
        return None

    for node in graph:
        if color[node] == WHITE:
            found = dfs(node)
            if found:
                return found
    return None


def validate(
    plan: ComponentPlan,
    requirement: RequirementSpec,
    architecture: ArchitectureSpec,
) -> ComponentValidationReport:
    issues: list[ComponentIssue] = []
    by_name = {c.name: c for c in plan.components}
    agent_ids = {a.id for a in architecture.agents}

    # ---- 空方案 ----
    if not plan.components:
        issues.append(
            ComponentIssue(level="error", code="C001", message="组件清单为空")
        )
        return ComponentValidationReport(passed=False, issues=issues)

    # ---- 架构声明的工具/技能必须有组件 ----
    for agent in architecture.agents:
        for ref in [*agent.tools, *agent.skills]:
            if ref not in by_name:
                issues.append(
                    ComponentIssue(
                        level="error",
                        code="C002",
                        message=f"节点 {agent.name} 使用 {ref}，但组件清单里没有该组件",
                        target=ref,
                    )
                )

    # ---- 注册表要求的接口字段 ----
    for comp in plan.components:
        meta = get_meta(comp.type)
        if meta is None:
            issues.append(
                ComponentIssue(
                    level="error",
                    code="C003",
                    message=f"未知组件类型 {comp.type}",
                    target=comp.name,
                )
            )
            continue
        missing = [
            f for f in meta.required_interface_fields if f not in comp.interface
        ]
        if missing:
            issues.append(
                ComponentIssue(
                    level="error",
                    code="C004",
                    message=f"{comp.type} 组件缺少 interface 字段：{', '.join(missing)}",
                    target=comp.name,
                )
            )
        if not comp.test_requirements:
            issues.append(
                ComponentIssue(
                    level="warning",
                    code="C005",
                    message=f"组件 {comp.name} 没有声明测试要求，后续无法验收",
                    target=comp.name,
                )
            )

    # ---- 依赖引用与环 ----
    for comp in plan.components:
        for dep in comp.depends_on:
            if dep not in by_name:
                issues.append(
                    ComponentIssue(
                        level="error",
                        code="C006",
                        message=f"组件 {comp.name} 依赖不存在的组件 {dep}",
                        target=comp.name,
                    )
                )
    cycle = _find_dep_cycle(plan)
    if cycle:
        issues.append(
            ComponentIssue(
                level="error",
                code="C007",
                message="组件依赖成环：" + " → ".join(cycle),
                target=cycle[0],
            )
        )

    # ---- 凑数嫌疑：required 但无人使用 ----
    used = {a: set() for a in agent_ids}
    for agent in architecture.agents:
        used[agent.id] = {*agent.tools, *agent.skills}
    referenced = set().union(*used.values()) if used else set()

    for comp in plan.components:
        if comp.priority == "required" and comp.type not in (
            "harness",
            "prompt",
        ) and comp.name not in referenced and not comp.owner_agents:
            issues.append(
                ComponentIssue(
                    level="warning",
                    code="C008",
                    message=f"组件 {comp.name} 标为 required，但没有任何节点或 owner_agents 使用它",
                    target=comp.name,
                )
            )
        for owner in comp.owner_agents:
            if owner not in agent_ids:
                issues.append(
                    ComponentIssue(
                        level="error",
                        code="C009",
                        message=f"组件 {comp.name} 的 owner_agents 指向不存在的节点 {owner}",
                        target=comp.name,
                    )
                )

    # ---- 需求与组件的一致性 ----
    text = "\n".join(
        [requirement.goal, *requirement.features, *requirement.external_dependencies]
    )
    has_rag_comp = any(c.type == "rag" for c in plan.components)
    if any(h in text for h in _RAG_HINTS) and not has_rag_comp:
        issues.append(
            ComponentIssue(
                level="warning",
                code="C010",
                message="需求涉及知识库/检索，但没有 rag 组件，确认是否遗漏",
                target="rag",
            )
        )

    if requirement.external_dependencies and not any(
        c.type in ("tool", "mcp") for c in plan.components
    ):
        issues.append(
            ComponentIssue(
                level="error",
                code="C011",
                message="需求依赖外部系统，但组件清单里没有 tool 或 mcp，无法真正接入",
            )
        )

    # ---- 过度设计 ----
    if len(plan.components) > 15 and architecture.complexity_level != "complex":
        issues.append(
            ComponentIssue(
                level="warning",
                code="C012",
                message=(
                    f"复杂度为 {architecture.complexity_level}，却规划了 "
                    f"{len(plan.components)} 个组件，存在过度设计风险"
                ),
            )
        )

    return ComponentValidationReport(
        passed=not any(i.level == "error" for i in issues),
        issues=issues,
    )
