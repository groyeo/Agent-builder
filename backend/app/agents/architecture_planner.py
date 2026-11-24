"""Architecture Planner —— 需求 → 架构方案。

三层协作（方案 5.2）：
1. prejudge()：规则先算出一个倾向（不含模型）；
2. 模型在"倾向 + 依据"之上做评估，可以反对，但必须在 decision_factors 里说明理由；
3. validate()：对模型产出的图结构做机器校验，不通过就把错误喂回去让它修。

模型不可用（Mock 或异常）时，用规则结果 + 最小可用架构兜底，保证链路不中断。
"""

from __future__ import annotations

import json
import re

from pydantic import ValidationError

from app.core.config import settings
from app.core.llm import LLMAdapter, LLMError, Message, get_llm
from app.core.progress import emit
from app.engine.prejudge import prejudge
from app.schemas.architecture import (
    ArchitectureSpec,
    PlanResult,
    PreJudgement,
)
from app.schemas.requirement import RequirementSpec
from app.validators.architecture_validator import validate

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)

SYSTEM_PROMPT = """你是一名 AI Agent 架构师。你会收到一份结构化需求，以及一份由规则引擎给出的
架构倾向和判断依据。你的任务是产出可落地的架构方案。

架构模式定义：
- single_agent：一个 Agent 完成全部工作，可以有多个工具。步骤少、目标单一时选它。
- multi_agent：多个专业角色协作，节点之间靠消息/调用传递结果，没有固定顺序。
- workflow：流程固定、分支条件明确，节点按确定顺序推进（节点可以是 LLM 节点或确定性步骤）。
- hybrid：外层是固定工作流，某些步骤内部由子 Agent 动态完成。

必须遵守的规则：
1. 只输出一个 JSON 对象，不要 Markdown 代码块，不要解释性文字。
2. 可以不同意规则给出的 suggested_type，但必须把反对理由写进 decision_factors，
   并保证最终 agents / workflow_edges 的结构与你声明的 architecture_type 自洽。
3. 每个 Agent 的 responsibility 必须互不重叠，能一句话说清"它唯一负责什么"。
4. workflow_edges 的 source/target 必须是 agents 里存在的 id；必须有明确入口节点。
5. required_components 必须包含 prompt 和 harness；rag / memory / mcp 只在需求确实需要时出现。
   不要为了显得完整而把所有组件都列上。
6. tools 与 skills 中引用的名字，必须出现在 required_components 或 optional_components 的 name 里。
7. risks 写真实风险（如"对工单分类准确率无基准数据"），不要写套话。

JSON 字段定义：
{
  "architecture_type": "single_agent | multi_agent | workflow | hybrid",
  "reasoning_summary": "string，面向人的业务理由",
  "decision_factors": ["string，驱动该决策的具体因子"],
  "agents": [
    {
      "id": "string，英文小写下划线",
      "name": "string",
      "responsibility": "string，唯一职责",
      "inputs": ["string"],
      "outputs": ["string"],
      "tools": ["string，引用组件 name"],
      "skills": ["string，引用组件 name"]
    }
  ],
  "workflow_edges": [
    {
      "source": "agent id",
      "target": "agent id",
      "condition": "string 或 null",
      "description": "string 或 null"
    }
  ],
  "required_components": [
    {"type": "prompt|skill|tool|mcp|harness|memory|rag", "name": "string", "purpose": "string", "depends_on": ["string"]}
  ],
  "optional_components": [],
  "risks": ["string"],
  "complexity_level": "simple | moderate | complex",
  "confidence": 0.0
}"""


def _strip_code_fence(text: str) -> str:
    cleaned = _FENCE_RE.sub("", text.strip())
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start != -1 and end != -1 and end > start:
        return cleaned[start : end + 1]
    return cleaned


def _fallback_spec(requirement: RequirementSpec, pre: PreJudgement) -> ArchitectureSpec:
    """模型不可用时的兜底架构：单 Agent + 必需组件，结构一定合法。"""
    tools = [
        {"type": "tool", "name": name, "purpose": f"接入 {name}", "depends_on": []}
        for name in requirement.external_dependencies[:3]
    ]
    return ArchitectureSpec(
        architecture_type="single_agent",
        reasoning_summary=(
            "模型不可用，已按规则预判降级为单 Agent 架构；"
            "该结果仅保证结构合法，未经模型评估，请人工复核。"
        ),
        decision_factors=pre.signals,
        agents=[
            {
                "id": "main_agent",
                "name": requirement.agent_name,
                "responsibility": requirement.goal,
                "inputs": requirement.inputs,
                "outputs": requirement.outputs,
                "tools": [t["name"] for t in tools],
                "skills": [],
            }
        ],
        workflow_edges=[],
        required_components=[
            {
                "type": "prompt",
                "name": "main_prompt",
                "purpose": "定义主 Agent 的行为边界",
                "depends_on": [],
            },
            {
                "type": "harness",
                "name": "agent_harness",
                "purpose": "运行循环、超时与终止条件",
                "depends_on": ["main_prompt"],
            },
            *tools,
        ],
        optional_components=[],
        risks=["架构未经模型评估，可能低估了需求复杂度"],
        complexity_level="simple",
        confidence=0.2,
    )


class ArchitecturePlanner:
    def __init__(self, llm: LLMAdapter | None = None, *, max_retries: int | None = None):
        self._llm = llm or get_llm()
        self._max_retries = (
            settings.analyzer_max_retries if max_retries is None else max_retries
        )

    def plan(self, requirement: RequirementSpec) -> PlanResult:
        pre = prejudge(requirement)
        emit(
            f"规则预判：{pre.suggested_type}，复杂度 {pre.complexity_score}/10"
            f"（命中 {len(pre.signals)} 条信号）"
        )

        if self._llm.name == "mock":
            spec = _fallback_spec(requirement, pre)
            report = validate(spec, requirement)
            return PlanResult(
                spec=spec,
                pre_judgement=pre,
                validation=report,
                provider="mock",
                model="mock",
                latency_ms=0,
                attempts=1,
                warnings=[
                    "当前使用离线 Mock 模型，已按规则预判返回兜底架构，"
                    "结果仅用于验证链路，不代表真实架构设计能力。"
                ],
            )

        messages = [
            Message(role="system", content=SYSTEM_PROMPT),
            Message(
                role="user",
                content=self._build_user_prompt(requirement, pre),
            ),
        ]

        warnings: list[str] = []
        last_error = ""

        for attempt in range(1, self._max_retries + 2):
            emit(f"调用模型评估架构（第 {attempt} 次，含 reasoning）")
            result = self._llm.chat(messages, json_mode=True)
            try:
                spec = ArchitectureSpec.model_validate(
                    json.loads(_strip_code_fence(result.content))
                )
            except (json.JSONDecodeError, ValidationError) as exc:
                last_error = self._format_error(exc)
                if attempt > self._max_retries:
                    raise LLMError(
                        f"架构方案未通过 Schema 校验，已重试 {self._max_retries} 次：{last_error}",
                        provider=result.provider,
                    ) from exc
                warnings.append(f"第 {attempt} 次输出不合规，已带错误信息重试。")
                emit(f"第 {attempt} 次输出未通过 Schema 校验：{last_error.splitlines()[0]}")
                messages = self._repair_messages(messages, result.content, last_error)
                continue

            report = validate(spec, requirement)
            emit(
                f"结构校验：{len(report.errors)} 个错误 / {len(report.warnings)} 个警告"
            )
            if not report.passed and attempt <= self._max_retries:
                # 图结构不成立是"语义错误"，Schema 校验抓不到，只能靠反馈修复
                detail = "\n".join(
                    f"- {i.code}: {i.message}" for i in report.errors
                )
                warnings.append(
                    f"第 {attempt} 次方案未通过图结构校验（{len(report.errors)} 个错误），已要求修复。"
                )
                emit(
                    f"第 {attempt} 次方案未通过图结构校验（{len(report.errors)} 个错误），已带问题重试"
                )
                messages = self._repair_messages(
                    messages, result.content, f"结构校验错误：\n{detail}"
                )
                continue

            if not report.passed:
                warnings.append(
                    "架构方案仍有结构错误，已原样返回并由前端提示人工介入。"
                )

            return PlanResult(
                spec=spec,
                pre_judgement=pre,
                validation=report,
                provider=result.provider,
                model=result.model,
                latency_ms=result.latency_ms,
                attempts=attempt,
                warnings=warnings,
            )

        raise LLMError(last_error or "架构规划失败", provider=self._llm.name)

    @staticmethod
    def _build_user_prompt(spec: RequirementSpec, pre: PreJudgement) -> str:
        return (
            "结构化需求：\n"
            + json.dumps(spec.model_dump(), ensure_ascii=False, indent=2)
            + "\n\n规则引擎的预判：\n"
            + json.dumps(pre.model_dump(), ensure_ascii=False, indent=2)
            + "\n\n请输出架构方案 JSON。"
        )

    @staticmethod
    def _repair_messages(
        messages: list[Message], assistant_content: str, error: str
    ) -> list[Message]:
        return [
            *messages,
            Message(role="assistant", content=assistant_content),
            Message(
                role="user",
                content=(
                    "上面的方案不可用，问题如下：\n"
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


def plan_architecture(requirement: RequirementSpec) -> PlanResult:
    return ArchitecturePlanner().plan(requirement)
