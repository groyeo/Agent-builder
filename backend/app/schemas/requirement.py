"""需求分析相关的数据结构。

设计原则（对应方案 1.3 / 6.1）：
- 模型只能返回符合本 Schema 的 JSON，不合规就重试，而不是把自由文本往下游传；
- 字段只保留"会影响架构决策"的信息，避免为了完整而堆砌；
- 缺失的关键信息进入 clarifying_questions，而不是让模型自行编造。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

ArchitectureHint = Literal[
    "undecided",
    "single_agent",
    "multi_agent",
    "workflow",
    "hybrid",
]

ComplexityLevel = Literal["simple", "moderate", "complex"]


class ClarifyingQuestion(BaseModel):
    """需要向用户澄清的问题。blocking=True 表示不回答就无法可靠地做架构设计。"""

    id: str = Field(description="问题标识，如 q_input")
    question: str = Field(description="向用户提出的问题，一句话，具体可回答")
    why: str = Field(description="为什么这个问题会影响设计")
    options: list[str] = Field(
        default_factory=list, description="建议选项，可为空（表示开放式回答）"
    )
    blocking: bool = Field(default=False, description="是否阻塞后续架构设计")


class RequirementSpec(BaseModel):
    """结构化需求。这是需求分析器的唯一合法输出。"""

    agent_name: str = Field(min_length=1, max_length=60, description="Agent 名称")
    goal: str = Field(min_length=1, description="一句话说明要解决的业务问题")
    target_users: list[str] = Field(default_factory=list, description="目标用户/使用方")
    inputs: list[str] = Field(default_factory=list, description="输入项及其形态")
    outputs: list[str] = Field(default_factory=list, description="输出项及其形态")
    features: list[str] = Field(default_factory=list, description="需要实现的功能点")
    constraints: list[str] = Field(default_factory=list, description="业务/技术约束")
    quality_requirements: list[str] = Field(
        default_factory=list, description="质量要求，如响应时间、准确率、可审计"
    )
    external_dependencies: list[str] = Field(
        default_factory=list, description="依赖的外部系统、数据源或 API"
    )
    acceptance_criteria: list[str] = Field(
        default_factory=list, description="可判定的验收标准"
    )
    architecture_hint: ArchitectureHint = Field(
        default="undecided",
        description="模型对架构模式的初步倾向；最终由架构规划器决定",
    )
    complexity_level: ComplexityLevel = Field(
        default="moderate", description="实现复杂度预判"
    )
    clarifying_questions: list[ClarifyingQuestion] = Field(
        default_factory=list, description="需要用户补充的问题"
    )
    confidence: float = Field(
        default=0.5, ge=0.0, le=1.0, description="对本次分析的自信程度"
    )

    @field_validator(
        "target_users",
        "inputs",
        "outputs",
        "features",
        "constraints",
        "quality_requirements",
        "external_dependencies",
        "acceptance_criteria",
        mode="after",
    )
    @classmethod
    def _drop_blank(cls, value: list[str]) -> list[str]:
        """模型容易输出空串或纯空白项，这里统一清洗，避免污染下游。"""
        return [item.strip() for item in value if item and item.strip()]

    @property
    def blocking_questions(self) -> list[ClarifyingQuestion]:
        return [q for q in self.clarifying_questions if q.blocking]


class AnalyzeRequest(BaseModel):
    """需求分析请求。"""

    raw_text: str = Field(
        min_length=5, max_length=8000, description="用户对 Agent 的自然语言描述"
    )
    answers: dict[str, str] = Field(
        default_factory=dict,
        description="上一轮澄清问题的回答，key 为问题 id，用于多轮补全",
    )


class AnalyzeResult(BaseModel):
    """需求分析结果（含可观测信息）。"""

    spec: RequirementSpec
    provider: str
    model: str
    latency_ms: int
    attempts: int
    warnings: list[str] = Field(
        default_factory=list, description="降级、重试等需要提示调用方的情况"
    )
