"""覆盖"真实模型分支"的测试。

背景：之前的测试只用 MockAdapter，而 Mock 走的是 fallback 早返回分支，
模型调用的代码路径（Schema 解析 → 图结构校验 → 重试）**从未被执行**。
这里用固定内容的假适配器把这条路径跑起来，并顺带验证进度事件确实发出。
"""

from __future__ import annotations

import json

import pytest

from app.agents.architecture_planner import ArchitecturePlanner
from app.agents.component_designer import ComponentDesigner
from app.agents.requirement_analyzer import RequirementAnalyzer
from app.core.llm.base import LLMAdapter, LLMResult, Message
from app.core.progress import bind_progress
from app.schemas.requirement import RequirementSpec


class CannedAdapter(LLMAdapter):
    """按顺序返回预置内容，记录调用次数。"""

    name = "canned"

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
        return LLMResult(content=content, model="canned", provider="canned", latency_ms=5)


REQUIREMENT_JSON = json.dumps(
    {
        "agent_name": "工单分流",
        "goal": "把工单分类并回复",
        "features": ["读取新工单", "判断类别"],
        "clarifying_questions": [],
        "confidence": 0.8,
    },
    ensure_ascii=False,
)


def _architecture_json(**override) -> str:
    payload = {
        "architecture_type": "single_agent",
        "reasoning_summary": "目标单一，一个节点足够",
        "decision_factors": ["只有一个决策点"],
        "agents": [
            {
                "id": "main_agent",
                "name": "Main",
                "responsibility": "分类并回复工单",
                "inputs": ["工单文本"],
                "outputs": ["分类结果"],
                "tools": [],
                "skills": [],
            }
        ],
        "workflow_edges": [],
        "required_components": [
            {"type": "prompt", "name": "main_prompt", "purpose": "行为指令"},
            {"type": "harness", "name": "agent_harness", "purpose": "运行框架"},
        ],
        "optional_components": [],
        "risks": ["分类准确率无基准"],
        "complexity_level": "simple",
        "confidence": 0.8,
    }
    payload.update(override)
    return json.dumps(payload, ensure_ascii=False)


def _component_json() -> str:
    return json.dumps(
        {
            "components": [
                {
                    "name": "main_prompt",
                    "type": "prompt",
                    "purpose": "去掉它节点就不知道自己该做什么",
                    "interface": {"role": "工单分类助手"},
                    "owner_agents": ["main_agent"],
                    "test_requirements": ["给定一条工单应输出三个类别之一"],
                    "priority": "required",
                },
                {
                    "name": "agent_harness",
                    "type": "harness",
                    "purpose": "去掉它就没有运行循环与终止条件",
                    "interface": {"max_steps": 5, "timeout_s": 60},
                    "owner_agents": ["main_agent"],
                    "test_requirements": ["超过 max_steps 应终止并返回部分结果"],
                    "priority": "required",
                },
            ],
            "notes": [],
            "summary": "两个必需组件",
        },
        ensure_ascii=False,
    )


def test_analyzer_real_llm_branch() -> None:
    events: list[str] = []
    with bind_progress(events.append):
        result = RequirementAnalyzer(llm=CannedAdapter([REQUIREMENT_JSON])).analyze("需求")
    assert result.attempts == 1
    assert result.spec.agent_name == "工单分流"
    assert any("第 1 次" in e for e in events)


def test_planner_real_llm_branch_runs_validation() -> None:
    """这条用例正是之前 UnboundLocalError 的回归防线。"""
    events: list[str] = []
    with bind_progress(events.append):
        result = ArchitecturePlanner(llm=CannedAdapter([_architecture_json()])).plan(
            RequirementSpec(agent_name="A", goal="B")
        )
    assert result.validation.passed
    assert result.attempts == 1
    assert any("规则预判" in e for e in events)
    assert any("结构校验" in e for e in events)


def test_planner_repairs_after_validation_error() -> None:
    # 第一次给出多节点但只声明 single_agent 的非法结构，第二次给出合法结构
    broken = _architecture_json(
        agents=[
            {"id": f"n{i}", "name": f"N{i}", "responsibility": f"职责{i}"} for i in range(3)
        ],
        workflow_edges=[
            {"source": "n0", "target": "n1"},
            {"source": "n1", "target": "n2"},
        ],
    )
    adapter = CannedAdapter([broken, _architecture_json()])
    result = ArchitecturePlanner(llm=adapter).plan(
        RequirementSpec(agent_name="A", goal="B")
    )
    assert adapter.calls == 2
    assert result.attempts == 2
    assert result.validation.passed
    assert any("未通过图结构校验" in w for w in result.warnings)


def test_designer_real_llm_branch_runs_validation() -> None:
    events: list[str] = []
    planner = ArchitecturePlanner(llm=CannedAdapter([_architecture_json()]))
    requirement = RequirementSpec(agent_name="A", goal="B")
    architecture = planner.plan(requirement).spec
    with bind_progress(events.append):
        result = ComponentDesigner(llm=CannedAdapter([_component_json()])).design(
            requirement, architecture
        )
    assert result.validation.passed
    assert len(result.plan.components) == 2
    assert any("组件校验" in e for e in events)


def test_planner_raises_after_retries_exhausted() -> None:
    from app.core.llm import LLMError

    with pytest.raises(LLMError):
        ArchitecturePlanner(llm=CannedAdapter(["不是 JSON"]), max_retries=1).plan(
            RequirementSpec(agent_name="A", goal="B")
        )
