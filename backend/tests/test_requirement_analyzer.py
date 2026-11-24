import json

import pytest

from app.agents.requirement_analyzer import RequirementAnalyzer
from app.core.llm.base import LLMAdapter, LLMError, LLMResult, Message
from app.core.llm.mock import MockAdapter


class SequenceAdapter(LLMAdapter):
    """按预设顺序返回内容，用来验证"不合规就重试"的行为。"""

    name = "sequence"

    def __init__(self, contents: list[str]) -> None:
        self._contents = contents
        self.calls = 0

    def chat(
        self,
        messages: list[Message],
        *,
        json_mode: bool = False,
        temperature: float | None = None,
    ) -> LLMResult:
        content = self._contents[min(self.calls, len(self._contents) - 1)]
        self.calls += 1
        return LLMResult(
            content=content, model="seq", provider="sequence", latency_ms=1
        )


def test_mock_adapter_produces_valid_spec() -> None:
    result = RequirementAnalyzer(llm=MockAdapter()).analyze(
        "做一个销售数据分析 Agent，读取 Excel 文件，输出趋势报告和异常提醒"
    )
    assert result.spec.agent_name
    assert result.spec.features
    assert result.provider == "mock"
    assert result.attempts == 1


def test_code_fence_is_stripped() -> None:
    payload = {
        "agent_name": "测试 Agent",
        "goal": "验证围栏剥离",
        "features": ["做一件事"],
    }
    wrapped = "```json\n" + json.dumps(payload, ensure_ascii=False) + "\n```"
    result = RequirementAnalyzer(llm=SequenceAdapter([wrapped])).analyze("任意需求")
    assert result.spec.agent_name == "测试 Agent"


def test_invalid_output_triggers_retry_then_succeeds() -> None:
    good = json.dumps({"agent_name": "好的 Agent", "goal": "重试后通过"})
    adapter = SequenceAdapter(['{"agent_name": 123}', good])
    result = RequirementAnalyzer(llm=adapter, max_retries=1).analyze("任意需求")
    assert result.attempts == 2
    assert any("重试" in w for w in result.warnings)
    assert adapter.calls == 2


def test_gives_up_after_max_retries() -> None:
    adapter = SequenceAdapter(["不是 JSON"])
    with pytest.raises(LLMError):
        RequirementAnalyzer(llm=adapter, max_retries=1).analyze("任意需求")
    assert adapter.calls == 2


def test_blank_items_are_cleaned() -> None:
    payload = {
        "agent_name": "清洗测试",
        "goal": "验证空串清理",
        "features": ["真实功能", "   ", ""],
    }
    result = RequirementAnalyzer(
        llm=SequenceAdapter([json.dumps(payload, ensure_ascii=False)])
    ).analyze("任意需求")
    assert result.spec.features == ["真实功能"]
