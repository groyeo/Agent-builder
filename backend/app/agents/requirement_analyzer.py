"""Requirement Analyzer —— 把自然语言需求转成结构化 RequirementSpec。

这一层的三条硬约束（来自方案 6.1）：
1. 只产出 Schema 内的字段，不合格就带错误信息重试；
2. 关键信息缺失时提问，不自行编造；
3. 每次调用都留下 provider/model/耗时/重试次数的可观测信息。
"""

from __future__ import annotations

import json
import re

from pydantic import ValidationError

from app.core.config import settings
from app.core.llm import LLMAdapter, LLMError, Message, get_llm
from app.core.progress import emit
from app.schemas.requirement import AnalyzeResult, RequirementSpec

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)

SYSTEM_PROMPT = """你是一名资深的 AI Agent 需求分析师。你的任务是把用户模糊的自然语言描述，
转化为一份结构化需求规格，供后续的架构规划器使用。

必须遵守的规则：
1. 只输出一个 JSON 对象，不要输出解释、不要用 Markdown 代码块包裹。
2. 只用下面列出的字段，不要新增字段。
3. 不确定的信息一律留空数组，不要编造具体数字、系统名或数据格式。
4. 如果缺少会直接影响架构选型的关键信息（输入来源与形态、输出去向、是否有外部系统、
   是否需要多角色协作），必须写进 clarifying_questions，并把 blocking 设为 true。
   非关键缺口放 blocking=false。
5. features 要写成可实现的动词短语（如"读取 Excel 并清洗空值"），不要写空泛口号。
6. acceptance_criteria 必须可判定（能明确回答"通过还是不通过"）。
7. architecture_hint 只是你的初步倾向：单一目标用 single_agent；需要多个专业角色分工用
   multi_agent；流程固定用 workflow；两者兼有用 hybrid；信息不足就填 undecided。
8. complexity_level：single_agent 且工具少为 simple；需要外部系统、RAG 或多步协作为
   moderate；需要多 Agent 协作 + 长流程 + 高可靠为 complex。
9. confidence 反映你对这份需求的把握程度（0~1）。信息越少，值越低。

JSON 字段定义：
{
  "agent_name": "string，Agent 名称，20 字以内",
  "goal": "string，一句话业务目标",
  "target_users": ["string，谁会用它"],
  "inputs": ["string，输入项及形态"],
  "outputs": ["string，输出项及形态"],
  "features": ["string，功能点，动词短语"],
  "constraints": ["string，业务或技术约束"],
  "quality_requirements": ["string，响应时间/准确率/可审计等"],
  "external_dependencies": ["string，外部系统、数据源、API"],
  "acceptance_criteria": ["string，可判定的验收标准"],
  "architecture_hint": "undecided | single_agent | multi_agent | workflow | hybrid",
  "complexity_level": "simple | moderate | complex",
  "clarifying_questions": [
    {
      "id": "string，如 q_input",
      "question": "string，一句话问题",
      "why": "string，为什么这会影响设计",
      "options": ["string，建议选项，可为空数组"],
      "blocking": true 或 false
    }
  ],
  "confidence": 0.0 到 1.0
}"""


def _strip_code_fence(text: str) -> str:
    """模型偶尔仍会加 ```json 包裹，这里做一次兜底剥离。"""
    cleaned = _FENCE_RE.sub("", text.strip())
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start != -1 and end != -1 and end > start:
        return cleaned[start : end + 1]
    return cleaned


class RequirementAnalyzer:
    def __init__(
        self,
        llm: LLMAdapter | None = None,
        *,
        max_retries: int | None = None,
    ) -> None:
        self._llm = llm or get_llm()
        self._max_retries = (
            settings.analyzer_max_retries if max_retries is None else max_retries
        )

    def analyze(self, raw_text: str, answers: dict[str, str] | None = None) -> AnalyzeResult:
        messages = [
            Message(role="system", content=SYSTEM_PROMPT),
            Message(role="user", content=self._build_user_prompt(raw_text, answers or {})),
        ]

        warnings: list[str] = []
        if self._llm.name == "mock":
            warnings.append(
                "当前使用的是离线 Mock 模型（未配置 LLM_API_KEY 或 provider=mock），"
                "结果仅用于验证链路，不代表真实分析质量。"
            )

        last_error = ""
        for attempt in range(1, self._max_retries + 2):
            emit(f"调用模型提取结构化需求（第 {attempt} 次）")
            result = self._llm.chat(messages, json_mode=True)
            try:
                payload = json.loads(_strip_code_fence(result.content))
                spec = RequirementSpec.model_validate(payload)
            except (json.JSONDecodeError, ValidationError) as exc:
                last_error = self._format_error(exc)
                if attempt > self._max_retries:
                    raise LLMError(
                        f"模型输出未通过 Schema 校验，已重试 {self._max_retries} 次：{last_error}",
                        provider=result.provider,
                    ) from exc
                warnings.append(f"第 {attempt} 次输出不合规，已带错误信息重试。")
                emit(f"第 {attempt} 次输出未通过 Schema 校验：{last_error.splitlines()[0]}")
                messages = [
                    *messages,
                    Message(role="assistant", content=result.content),
                    Message(
                        role="user",
                        content=(
                            "上面的 JSON 不符合要求，错误如下：\n"
                            f"{last_error}\n"
                            "请只输出修正后的完整 JSON 对象，不要任何额外文字。"
                        ),
                    ),
                ]
                continue

            return AnalyzeResult(
                spec=spec,
                provider=result.provider,
                model=result.model,
                latency_ms=result.latency_ms,
                attempts=attempt,
                warnings=warnings,
            )

        # 理论上不可达：循环内要么 return 要么 raise
        raise LLMError(last_error or "需求分析失败", provider=self._llm.name)

    @staticmethod
    def _build_user_prompt(raw_text: str, answers: dict[str, str]) -> str:
        parts = ["需求原文：", raw_text.strip()]
        if answers:
            parts.append("\n用户在澄清环节的回答：")
            for key, value in answers.items():
                parts.append(f"- {key}: {value}")
        parts.append("\n请按系统提示中的字段定义输出 JSON。")
        return "\n".join(parts)

    @staticmethod
    def _format_error(exc: Exception) -> str:
        """把 Pydantic 错误压成模型能看懂的一行行提示。"""
        if isinstance(exc, ValidationError):
            lines = [
                f"- 字段 {'.'.join(str(p) for p in err['loc'])}: {err['msg']}"
                for err in exc.errors()[:10]
            ]
            return "\n".join(lines)
        return str(exc)


def analyze_requirement(
    raw_text: str, answers: dict[str, str] | None = None
) -> AnalyzeResult:
    """模块级便捷入口，供 API 层直接使用。"""
    return RequirementAnalyzer().analyze(raw_text, answers)
