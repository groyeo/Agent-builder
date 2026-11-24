"""架构设计相关的数据结构。

对应方案 5.3 的架构决策输出字段，并补充两类工程信息：
- decision_factors：把"为什么这么选"拆成可核查的因子，而不是一段模糊的理由；
- risks：把模型自己识别出的风险显式落库，供后续校验和人工审批参考。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

from app.schemas.requirement import RequirementSpec

ArchitectureType = Literal["single_agent", "multi_agent", "workflow", "hybrid"]

ComponentType = Literal[
    "prompt", "skill", "tool", "mcp", "harness", "memory", "rag"
]

ComplexityLevel = Literal["simple", "moderate", "complex"]


class AgentNode(BaseModel):
    """架构中的一个 Agent 节点（Workflow 场景下也可以是确定性步骤节点）。"""

    id: str = Field(min_length=1, description="节点标识，如 main_agent / router")
    name: str = Field(min_length=1, description="可读名称")
    responsibility: str = Field(min_length=1, description="这个节点唯一负责什么")
    inputs: list[str] = Field(default_factory=list)
    outputs: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list, description="需要的工具（引用组件名）")
    skills: list[str] = Field(default_factory=list, description="需要的技能（引用组件名）")


class WorkflowEdge(BaseModel):
    """节点之间的调用/流转边。condition 非空表示条件分支。"""

    source: str
    target: str
    condition: str | None = Field(
        default=None, description="分支条件，如 '分类=技术'；无条件流转留空"
    )
    description: str | None = Field(default=None, description="这条边传什么、为什么存在")


class ComponentNeed(BaseModel):
    """需要生成的组件。最终是否生成由架构校验和后续生成器共同决定。"""

    type: ComponentType
    name: str = Field(min_length=1)
    purpose: str = Field(min_length=1, description="解决什么问题，为什么不能省")
    depends_on: list[str] = Field(
        default_factory=list, description="依赖的其他组件名"
    )


class ArchitectureSpec(BaseModel):
    architecture_type: ArchitectureType
    reasoning_summary: str = Field(
        min_length=1, description="选择该架构的业务理由，面向人类阅读"
    )
    decision_factors: list[str] = Field(
        default_factory=list,
        description="驱动该决策的具体因子，如'存在 3 个专业角色分工'",
    )
    agents: list[AgentNode] = Field(default_factory=list)
    workflow_edges: list[WorkflowEdge] = Field(default_factory=list)
    required_components: list[ComponentNeed] = Field(default_factory=list)
    optional_components: list[ComponentNeed] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    complexity_level: ComplexityLevel = "moderate"
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)

    @field_validator("agents")
    @classmethod
    def _unique_ids(cls, value: list[AgentNode]) -> list[AgentNode]:
        seen: set[str] = set()
        for agent in value:
            if agent.id in seen:
                raise ValueError(f"agent id 重复：{agent.id}")
            seen.add(agent.id)
        return value


class PreJudgement(BaseModel):
    """规则预判结果。不依赖模型，先给出一个有依据的倾向，再让模型评估。"""

    suggested_type: ArchitectureType
    complexity_score: int = Field(ge=0, description="0~10 的复杂度粗评分")
    signals: list[str] = Field(default_factory=list, description="命中的判断依据")

    @property
    def decisive(self) -> bool:
        """信号足够强时才让规则拥有否决权。"""
        return self.complexity_score <= 2 or self.complexity_score >= 7


class ValidationIssue(BaseModel):
    level: Literal["error", "warning", "info"]
    code: str = Field(description="稳定的问题编码，便于前端分类展示")
    message: str
    target: str | None = Field(default=None, description="指向节点 id 或组件名")


class ValidationReport(BaseModel):
    passed: bool = Field(description="是否通过（无 error 即通过）")
    issues: list[ValidationIssue] = Field(default_factory=list)

    @property
    def errors(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.level == "error"]

    @property
    def warnings(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.level == "warning"]


class PlanRequest(BaseModel):
    """架构规划请求。传入结构化需求，避免重复分析。"""

    requirement: RequirementSpec


class PlanResult(BaseModel):
    spec: ArchitectureSpec
    pre_judgement: PreJudgement
    validation: ValidationReport
    provider: str
    model: str
    latency_ms: int
    attempts: int
    warnings: list[str] = Field(default_factory=list)
