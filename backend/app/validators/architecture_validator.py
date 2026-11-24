"""架构图结构校验。

这是第三层（方案 5.2）：模型给出的架构 JSON 即使字段合法，也可能在结构上讲不通，
例如 edges 引用了不存在的节点、single_agent 却给了 3 个节点、workflow 没有入口。

校验结果分三级：
- error：结构不成立，必须让模型修；
- warning：能跑但存在隐患，交付时要显式提示用户；
- info：提示性信息，不影响流程。
"""

from __future__ import annotations

from app.schemas.architecture import (
    ArchitectureSpec,
    ValidationIssue,
    ValidationReport,
)
from app.schemas.requirement import RequirementSpec

_RAG_HINTS = ("知识库", "FAQ", "常见问题", "检索", "帮助中心", "文档")


def _edges_of(spec: ArchitectureSpec) -> list[tuple[str, str]]:
    return [(e.source, e.target) for e in spec.workflow_edges]


def _find_cycle(spec: ArchitectureSpec) -> list[str] | None:
    """在 workflow_edges 上找一个环。返回环上的节点 id 序列。"""
    graph: dict[str, list[str]] = {a.id: [] for a in spec.agents}
    for src, dst in _edges_of(spec):
        graph.setdefault(src, []).append(dst)

    WHITE, GRAY, BLACK = 0, 1, 2
    color = dict.fromkeys(graph, WHITE)
    stack: list[str] = []

    def dfs(node: str) -> list[str] | None:
        color[node] = GRAY
        stack.append(node)
        for nxt in graph.get(node, []):
            if color.get(nxt, WHITE) == GRAY:
                return stack[stack.index(nxt) :] + [nxt]
            if color.get(nxt, WHITE) == WHITE:
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
    spec: ArchitectureSpec, requirement: RequirementSpec | None = None
) -> ValidationReport:
    issues: list[ValidationIssue] = []
    agent_ids = {a.id for a in spec.agents}

    # ---- 结构完整性 ----
    if not spec.agents:
        issues.append(
            ValidationIssue(
                level="error", code="E001", message="架构中没有任何节点"
            )
        )

    if spec.architecture_type == "single_agent" and len(spec.agents) > 1:
        issues.append(
            ValidationIssue(
                level="error",
                code="E002",
                message=f"声明为 single_agent，却定义了 {len(spec.agents)} 个节点",
            )
        )

    if spec.architecture_type in ("multi_agent", "hybrid") and len(spec.agents) < 2:
        issues.append(
            ValidationIssue(
                level="error",
                code="E003",
                message=f"{spec.architecture_type} 至少需要 2 个节点，当前只有 {len(spec.agents)} 个",
            )
        )

    if spec.architecture_type in ("workflow", "hybrid", "multi_agent") and not spec.workflow_edges:
        issues.append(
            ValidationIssue(
                level="error",
                code="E004",
                message=f"{spec.architecture_type} 必须给出节点之间的流转边（workflow_edges 为空）",
            )
        )

    # ---- 边的引用完整性 ----
    for edge in spec.workflow_edges:
        if edge.source not in agent_ids:
            issues.append(
                ValidationIssue(
                    level="error",
                    code="E005",
                    message=f"边引用了不存在的起始节点 {edge.source}",
                    target=edge.source,
                )
            )
        if edge.target not in agent_ids:
            issues.append(
                ValidationIssue(
                    level="error",
                    code="E005",
                    message=f"边引用了不存在的目标节点 {edge.target}",
                    target=edge.target,
                )
            )
        if edge.source == edge.target:
            issues.append(
                ValidationIssue(
                    level="error",
                    code="E006",
                    message=f"节点 {edge.source} 指向自身，形成自环",
                    target=edge.source,
                )
            )

    # ---- 入口与孤立节点 ----
    if spec.workflow_edges and spec.agents:
        sources = {e.source for e in spec.workflow_edges}
        targets = {e.target for e in spec.workflow_edges}
        entry = sources - targets
        if not entry:
            issues.append(
                ValidationIssue(
                    level="error",
                    code="E007",
                    message="找不到入口节点（所有节点都有入边），流程无法启动",
                )
            )

        for agent in spec.agents:
            if agent.id not in sources and agent.id not in targets:
                issues.append(
                    ValidationIssue(
                        level="warning",
                        code="W001",
                        message=f"节点 {agent.name} 与流程没有连接，可能是多余节点",
                        target=agent.id,
                    )
                )

    # ---- 组件清单 ----
    required_types = {c.type for c in spec.required_components}
    for must in ("prompt", "harness"):
        if must not in required_types:
            issues.append(
                ValidationIssue(
                    level="error",
                    code="E008",
                    message=f"必需组件缺失：{must}（任何 Agent 都需要行为指令与运行框架）",
                    target=must,
                )
            )

    declared_names = {c.name for c in spec.required_components} | {
        c.name for c in spec.optional_components
    }
    for agent in spec.agents:
        for ref in [*agent.tools, *agent.skills]:
            if ref not in declared_names:
                issues.append(
                    ValidationIssue(
                        level="warning",
                        code="W003",
                        message=f"节点 {agent.name} 引用了未在组件清单中声明的 {ref}",
                        target=ref,
                    )
                )

    both = {c.name for c in spec.required_components} & {
        c.name for c in spec.optional_components
    }
    for name in both:
        issues.append(
            ValidationIssue(
                level="info",
                code="I001",
                message=f"组件 {name} 同时出现在必需与可选清单中，将按必需处理",
                target=name,
            )
        )

    # ---- 需求与架构的一致性 ----
    if requirement is not None:
        text = "\n".join(
            [requirement.goal, *requirement.features, *requirement.external_dependencies]
        )
        if any(h in text for h in _RAG_HINTS) and "rag" not in required_types | {
            c.type for c in spec.optional_components
        }:
            issues.append(
                ValidationIssue(
                    level="warning",
                    code="W004",
                    message="需求出现知识库/检索类诉求，但架构中未规划 RAG 组件，确认是否遗漏",
                    target="rag",
                )
            )

        if requirement.external_dependencies and not any(
            a.tools for a in spec.agents
        ):
            issues.append(
                ValidationIssue(
                    level="warning",
                    code="W002",
                    message="需求依赖外部系统，但没有任何节点声明工具，可能无法真正接入",
                )
            )

    # ---- 死循环风险 ----
    cycle = _find_cycle(spec)
    if cycle:
        issues.append(
            ValidationIssue(
                level="warning",
                code="W005",
                message="检测到可能的循环流转（" + " → ".join(cycle) + "），需确认有终止条件",
                target=cycle[0],
            )
        )

    # ---- 过度拆分（违背"按需设计"原则）----
    if len(spec.agents) > 7 and spec.complexity_level in ("simple", "moderate"):
        issues.append(
            ValidationIssue(
                level="warning",
                code="W007",
                message=(
                    f"声明复杂度为 {spec.complexity_level}，却拆出 {len(spec.agents)} 个节点；"
                    "存在过度设计风险，建议合并职责相近的节点"
                ),
            )
        )

    if spec.confidence < 0.4:
        issues.append(
            ValidationIssue(
                level="info",
                code="W006",
                message=f"架构置信度较低（{spec.confidence}），建议人工重点审核",
            )
        )

    return ValidationReport(
        passed=not any(i.level == "error" for i in issues),
        issues=issues,
    )
