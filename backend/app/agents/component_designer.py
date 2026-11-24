"""Component Designer —— 架构 + 需求 → 可生成、可测试的组件清单。

与架构规划器的分工：
- 架构规划器回答"有哪些节点、怎么连"（required_components 只是类型级的占位）；
- 本模块回答"每个组件具体长什么样"：接口字段、依赖、由谁使用、怎么测。

方案 6.3 强调"必要性"：所以 Prompt 里强制要求每个组件说明"去掉它会怎样"，
并且校验器会把无人使用的 required 组件标出来。
"""

from __future__ import annotations

import json
import re

from pydantic import ValidationError

from app.core.config import settings
from app.core.llm import LLMAdapter, LLMError, Message, get_llm
from app.core.progress import emit
from app.engine.component_registry import describe_for_prompt, get_meta
from app.schemas.architecture import ArchitectureSpec
from app.schemas.components import (
    ComponentPlan,
    ComponentPlanResult,
    ComponentSpec,
    DesignRequest,
)
from app.schemas.requirement import RequirementSpec
from app.validators.component_validator import validate

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)

SYSTEM_PROMPT = """你是一名 AI Agent 组件设计师。你会收到结构化需求、已确认的架构方案，
以及一份组件类型注册表。你的任务是产出可直接用于代码生成的组件清单。

严格规则：
1. 只输出一个 JSON 对象，不要 Markdown 代码块，不要解释文字。
2. 以架构方案中的 required_components / optional_components 为基础，把它们细化为组件；
   可以补充架构遗漏的必需组件（例如某节点缺少 prompt），但必须在 notes 里说明补充原因。
3. 架构中每个节点的 tools / skills 引用的名字，都必须在组件清单里存在，且类型一致。
4. 每个组件的 purpose 必须能回答"去掉它会怎样"，不要写"提升效果"这类空话。
5. interface 必须带注册表要求的必填字段：
   - prompt: role
   - skill: steps（数组）
   - tool: input_schema（JSON Schema 对象）、output_desc
   - mcp: transport、server_ref
   - harness: max_steps（数字）、timeout_s（数字）
   - memory: scope、key_fields
   - rag: knowledge_source、chunk_strategy
6. owner_agents 填实际使用该组件的节点 id，必须是架构里存在的 id。
7. test_requirements 至少一条，且必须可判定（能被写成断言）。
8. 不要为了完整而增加组件。需求里没提到的能力，不要凭空规划 mcp / memory / rag。

组件类型注册表：
{registry}

JSON 字段定义：
{{
  "components": [
    {{
      "type": "prompt|skill|tool|mcp|harness|memory|rag",
      "name": "snake_case 英文名",
      "purpose": "string",
      "priority": "required|optional",
      "owner_agents": ["节点 id"],
      "interface": {{ }},
      "depends_on": ["组件名"],
      "test_requirements": ["string"]
    }}
  ],
  "summary": "string，这套组件如何支撑架构，以及刻意没有做什么",
  "notes": ["string，取舍或待确认项"]
}}"""


def _strip_code_fence(text: str) -> str:
    cleaned = _FENCE_RE.sub("", text.strip())
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start != -1 and end != -1 and end > start:
        return cleaned[start : end + 1]
    return cleaned


_DEFAULT_INTERFACE: dict[str, dict] = {
    "prompt": {"role": "system"},
    "skill": {"steps": []},
    "tool": {"input_schema": {}, "output_desc": ""},
    "mcp": {"transport": "stdio", "server_ref": ""},
    "harness": {"max_steps": 8, "timeout_s": 120},
    "memory": {"scope": "session", "key_fields": []},
    "rag": {"knowledge_source": "", "chunk_strategy": "fixed_512"},
}


def _fallback_plan(
    requirement: RequirementSpec, architecture: ArchitectureSpec
) -> ComponentPlan:
    """模型不可用：按架构声明平铺成合法组件，接口取注册表默认值。"""
    components: list[ComponentSpec] = []
    owners = [a.id for a in architecture.agents]

    for need in [*architecture.required_components, *architecture.optional_components]:
        meta = get_meta(need.type)
        components.append(
            ComponentSpec(
                type=need.type,
                name=need.name,
                purpose=need.purpose,
                priority="required"
                if need in architecture.required_components
                else "optional",
                owner_agents=owners,
                interface=dict(_DEFAULT_INTERFACE.get(need.type, {})),
                depends_on=need.depends_on,
                test_requirements=list(meta.default_tests) if meta else [],
            )
        )

    return ComponentPlan(
        components=components,
        summary=(
            "模型不可用，已按架构声明的组件平铺生成，接口字段取注册表默认值；"
            "该清单仅保证结构合法，未经细化，请人工复核后再进入代码生成。"
        ),
        notes=["组件细节未经过模型设计，interface 字段为占位值"],
    )


class ComponentDesigner:
    def __init__(self, llm: LLMAdapter | None = None, *, max_retries: int | None = None):
        self._llm = llm or get_llm()
        self._max_retries = (
            settings.analyzer_max_retries if max_retries is None else max_retries
        )

    def design(self, requirement: RequirementSpec, architecture: ArchitectureSpec) -> ComponentPlanResult:
        if self._llm.name == "mock":
            plan = _fallback_plan(requirement, architecture)
            report = validate(plan, requirement, architecture)
            return ComponentPlanResult(
                plan=plan,
                validation=report,
                provider="mock",
                model="mock",
                latency_ms=0,
                attempts=1,
                warnings=[
                    "当前使用离线 Mock 模型，组件清单为按架构平铺的兜底结果，"
                    "接口字段是占位值，不可直接用于代码生成。"
                ],
            )

        messages = [
            Message(
                role="system",
                content=SYSTEM_PROMPT.format(registry=describe_for_prompt()),
            ),
            Message(role="user", content=self._build_user_prompt(requirement, architecture)),
        ]

        warnings: list[str] = []
        last_error = ""

        for attempt in range(1, self._max_retries + 2):
            emit(
                f"调用模型细化组件（第 {attempt} 次，架构含 "
                f"{len(architecture.agents)} 个节点 / "
                f"{len(architecture.required_components)} 个必需组件）"
            )
            result = self._llm.chat(messages, json_mode=True)
            try:
                plan = ComponentPlan.model_validate(
                    json.loads(_strip_code_fence(result.content))
                )
            except (json.JSONDecodeError, ValidationError) as exc:
                last_error = self._format_error(exc)
                if attempt > self._max_retries:
                    raise LLMError(
                        f"组件清单未通过 Schema 校验，已重试 {self._max_retries} 次：{last_error}",
                        provider=result.provider,
                    ) from exc
                warnings.append(f"第 {attempt} 次输出不合规，已带错误信息重试。")
                emit(f"第 {attempt} 次输出未通过 Schema 校验：{last_error.splitlines()[0]}")
                messages = self._repair(messages, result.content, last_error)
                continue

            report = validate(plan, requirement, architecture)
            emit(
                f"组件校验：{len(report.errors)} 个错误 / {len(report.warnings)} 个警告"
            )
            if not report.passed and attempt <= self._max_retries:
                detail = "\n".join(f"- {i.code}: {i.message}" for i in report.errors)
                warnings.append(
                    f"第 {attempt} 次组件方案未通过校验（{len(report.errors)} 个错误），已要求修复。"
                )
                emit(
                    f"第 {attempt} 次组件方案未通过校验（{len(report.errors)} 个错误），已带问题重试"
                )
                messages = self._repair(
                    messages, result.content, f"组件校验错误：\n{detail}"
                )
                continue

            if not report.passed:
                warnings.append("组件方案仍有错误，已原样返回，请人工介入。")

            return ComponentPlanResult(
                plan=plan,
                validation=report,
                provider=result.provider,
                model=result.model,
                latency_ms=result.latency_ms,
                attempts=attempt,
                warnings=warnings,
            )

        raise LLMError(last_error or "组件设计失败", provider=self._llm.name)

    @staticmethod
    def _build_user_prompt(
        requirement: RequirementSpec, architecture: ArchitectureSpec
    ) -> str:
        return (
            "结构化需求：\n"
            + json.dumps(requirement.model_dump(), ensure_ascii=False, indent=2)
            + "\n\n已确认的架构方案：\n"
            + json.dumps(architecture.model_dump(), ensure_ascii=False, indent=2)
            + "\n\n请输出组件清单 JSON。"
        )

    @staticmethod
    def _repair(messages: list[Message], content: str, error: str) -> list[Message]:
        return [
            *messages,
            Message(role="assistant", content=content),
            Message(
                role="user",
                content=(
                    "上面的组件清单不可用，问题如下：\n"
                    f"{error}\n"
                    "请只输出修正后的完整 JSON 对象，不要任何额外文字。"
                ),
            ),
        ]

    @staticmethod
    def _format_error(exc: Exception) -> str:
        if isinstance(exc, ValidationError):
            return "\n".join(
                f"- 字段 {'.'.join(str(p) for p in err['loc'])}: {err['msg']}"
                for err in exc.errors()[:10]
            )
        return str(exc)


def design_components(payload: DesignRequest) -> ComponentPlanResult:
    return ComponentDesigner().design(payload.requirement, payload.architecture)
