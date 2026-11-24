"""代码生成相关的数据结构。

设计取向：生成物是**可读、可改、能跑**的脚手架，不是一次性的演示代码。
因此每个文件都带"为什么这样生成"的注释来源（需求/架构/组件名），
用户接手时能顺着线索回到设计依据。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.architecture import ArchitectureSpec, ValidationReport
from app.schemas.components import ComponentPlan
from app.schemas.delivery import DeliveryPlan
from app.schemas.requirement import RequirementSpec

FileKind = Literal["python", "markdown", "yaml", "text", "json"]


class GeneratedFile(BaseModel):
    path: str = Field(description="相对项目根目录的路径，统一用 / 分隔")
    content: str
    kind: FileKind = "text"
    lines: int = 0
    source: str = Field(
        default="template",
        description="该文件由什么生成：template / component:<name> / node:<id>",
    )


class CodegenStats(BaseModel):
    files: int = 0
    lines: int = 0
    python_files: int = 0
    tests: int = 0


class CoverItem(BaseModel):
    """一条"组件 → 落地位置"的映射，供人核对有没有被漏掉。"""

    component: str
    component_type: str
    target: str


class CodegenResult(BaseModel):
    project_name: str
    files: list[GeneratedFile] = Field(default_factory=list)
    tree: str = ""
    stats: CodegenStats = Field(default_factory=CodegenStats)
    coverage: list[CoverItem] = Field(default_factory=list)
    validation: ValidationReport = Field(
        default_factory=lambda: ValidationReport(passed=True)
    )
    warnings: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    delivery: DeliveryPlan | None = Field(
        default=None,
        description=(
            "交付清单：跑起来还需要准备什么（Key/语料/数据/账号）、"
            "去哪注册、填到哪、缺了会怎样，以及从零启动的步骤"
        ),
    )
    generator: str = Field(
        default="template",
        description="template 表示确定性模板生成，不消耗模型 token",
    )


class CodegenRequest(BaseModel):
    requirement: RequirementSpec
    architecture: ArchitectureSpec
    components: ComponentPlan
