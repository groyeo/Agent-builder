"""组件设计相关的数据结构。

方案 6.3 要求区分七类组件的职责边界：
- prompt：行为指令，决定"怎么想、怎么说"
- skill：可复用的专业能力封装（一段稳定的方法论，不是一次调用）
- tool：确定性操作，输入输出可枚举，不依赖模型
- mcp：标准化协议接入的外部服务（跨应用时才需要）
- harness：运行循环、状态、权限、超时、重试、终止条件
- memory：跨步骤或跨会话保留的信息
- rag：检索外部知识

核心约束：组件不是为了凑数而存在的。每个组件都必须能回答"去掉它，哪个需求就满足不了"。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from app.schemas.architecture import ArchitectureSpec
from app.schemas.requirement import RequirementSpec

ComponentType = Literal[
    "prompt", "skill", "tool", "mcp", "harness", "memory", "rag"
]

Priority = Literal["required", "optional"]


class ComponentSpec(BaseModel):
    """一个待生成的组件。"""

    type: ComponentType
    name: str = Field(min_length=1, max_length=64, description="英文小写下划线")
    purpose: str = Field(
        min_length=1, description="解决什么问题；必须能说明去掉它会怎样"
    )
    priority: Priority = "required"
    owner_agents: list[str] = Field(
        default_factory=list, description="使用该组件的节点 id"
    )
    interface: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "类型相关契约：tool 需要 input_schema/output_desc；"
            "harness 需要 max_steps/timeout_s/retry"
        ),
    )
    depends_on: list[str] = Field(default_factory=list, description="依赖的组件名")
    test_requirements: list[str] = Field(
        default_factory=list, description="该组件必须被怎样测试"
    )

    @field_validator("name")
    @classmethod
    def _snake_case(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned.replace("_", "").isalnum() or cleaned != cleaned.lower():
            raise ValueError(
                f"组件名必须是英文小写加下划线（snake_case），当前为 {value}"
            )
        return cleaned

    @field_validator("owner_agents", "depends_on", "test_requirements")
    @classmethod
    def _drop_blank(cls, value: list[str]) -> list[str]:
        return [v.strip() for v in value if v and v.strip()]


class ComponentPlan(BaseModel):
    """一次组件设计的完整产物。"""

    components: list[ComponentSpec] = Field(default_factory=list)
    summary: str = Field(
        min_length=1, description="一段话说明这套组件如何支撑架构，哪些是刻意不做的"
    )
    notes: list[str] = Field(
        default_factory=list, description="需要用户注意的取舍或待确认项"
    )

    @field_validator("components")
    @classmethod
    def _unique_names(cls, value: list[ComponentSpec]) -> list[ComponentSpec]:
        seen: set[str] = set()
        for comp in value:
            if comp.name in seen:
                raise ValueError(f"组件名重复：{comp.name}")
            seen.add(comp.name)
        return value

    def by_type(self, comp_type: ComponentType) -> list[ComponentSpec]:
        return [c for c in self.components if c.type == comp_type]


class ComponentIssue(BaseModel):
    level: Literal["error", "warning", "info"]
    code: str
    message: str
    target: str | None = None


class ComponentValidationReport(BaseModel):
    passed: bool
    issues: list[ComponentIssue] = Field(default_factory=list)

    @property
    def errors(self) -> list[ComponentIssue]:
        return [i for i in self.issues if i.level == "error"]

    @property
    def warnings(self) -> list[ComponentIssue]:
        return [i for i in self.issues if i.level == "warning"]


class DesignRequest(BaseModel):
    """组件设计请求：需要同时给出需求与架构，否则无法判断组件的必要性。"""

    requirement: RequirementSpec
    architecture: ArchitectureSpec


class ComponentPlanResult(BaseModel):
    plan: ComponentPlan
    validation: ComponentValidationReport
    provider: str
    model: str
    latency_ms: int
    attempts: int
    warnings: list[str] = Field(default_factory=list)
