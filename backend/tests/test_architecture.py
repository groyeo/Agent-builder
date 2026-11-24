import pytest

from app.agents.architecture_planner import ArchitecturePlanner
from app.core.llm.mock import MockAdapter
from app.engine.prejudge import prejudge
from app.schemas.architecture import ArchitectureSpec
from app.schemas.requirement import RequirementSpec
from app.validators.architecture_validator import validate


def _req(**kw) -> RequirementSpec:
    base = {"agent_name": "测试 Agent", "goal": "把 A 变成 B"}
    base.update(kw)
    return RequirementSpec(**base)


def _spec(**kw) -> ArchitectureSpec:
    base = {
        "architecture_type": "single_agent",
        "reasoning_summary": "目标单一",
        "agents": [
            {
                "id": "main_agent",
                "name": "Main",
                "responsibility": "完成全部工作",
            }
        ],
        "required_components": [
            {"type": "prompt", "name": "main_prompt", "purpose": "行为指令"},
            {"type": "harness", "name": "agent_harness", "purpose": "运行框架"},
        ],
    }
    base.update(kw)
    return ArchitectureSpec(**base)


# ---------------- 规则预判 ----------------

def test_prejudge_simple_requirement() -> None:
    pre = prejudge(_req(goal="把 CSV 转成 JSON"))
    assert pre.suggested_type == "single_agent"
    assert pre.complexity_score <= 2


def test_prejudge_detects_roles_and_flow() -> None:
    req = _req(
        goal="检索资料后分析并生成报告，然后人工审核",
        features=["检索历史工单", "分析问题根因", "生成回复草稿", "审核草稿质量"],
        external_dependencies=["工单系统", "知识库"],
    )
    pre = prejudge(req)
    assert pre.complexity_score >= 4
    assert pre.suggested_type in ("multi_agent", "hybrid", "workflow")
    assert pre.signals


def test_prejudge_detects_rag_signal() -> None:
    pre = prejudge(_req(goal="基于内部知识库回答问题"))
    assert any("检索" in s or "RAG" in s for s in pre.signals)


# ---------------- 结构校验 ----------------

def test_valid_single_agent_passes() -> None:
    report = validate(_spec())
    assert report.passed, [i.message for i in report.errors]


def test_single_agent_with_two_nodes_fails() -> None:
    spec = _spec(
        agents=[
            {"id": "a", "name": "A", "responsibility": "x"},
            {"id": "b", "name": "B", "responsibility": "y"},
        ]
    )
    report = validate(spec)
    assert not report.passed
    assert any(i.code == "E002" for i in report.errors)


def test_edge_to_missing_node_fails() -> None:
    spec = _spec(
        architecture_type="workflow",
        agents=[
            {"id": "a", "name": "A", "responsibility": "x"},
            {"id": "b", "name": "B", "responsibility": "y"},
        ],
        workflow_edges=[{"source": "a", "target": "ghost"}],
    )
    report = validate(spec)
    assert any(i.code == "E005" for i in report.errors)


def test_missing_entry_node_fails() -> None:
    spec = _spec(
        architecture_type="workflow",
        agents=[
            {"id": "a", "name": "A", "responsibility": "x"},
            {"id": "b", "name": "B", "responsibility": "y"},
        ],
        workflow_edges=[
            {"source": "a", "target": "b"},
            {"source": "b", "target": "a"},
        ],
    )
    report = validate(spec)
    codes = {i.code for i in report.errors}
    assert "E007" in codes
    assert any(w.code == "W005" for w in report.warnings)  # 环检测


def test_missing_harness_fails() -> None:
    spec = _spec(
        required_components=[
            {"type": "prompt", "name": "p", "purpose": "x"},
        ]
    )
    report = validate(spec)
    assert any(i.code == "E008" for i in report.errors)


def test_undeclared_tool_reference_warns() -> None:
    spec = _spec(
        agents=[
            {
                "id": "main_agent",
                "name": "Main",
                "responsibility": "x",
                "tools": ["sql_tool"],
            }
        ]
    )
    report = validate(spec)
    assert any(i.code == "W003" for i in report.warnings)


def test_rag_gap_warns() -> None:
    spec = _spec()
    report = validate(spec, _req(features=["检索内部知识库并回答"]))
    assert any(i.code == "W004" for i in report.warnings)


# ---------------- 规划器兜底 ----------------

def test_planner_fallback_with_mock_is_valid() -> None:
    result = ArchitecturePlanner(llm=MockAdapter()).plan(
        _req(external_dependencies=["工单系统"])
    )
    assert result.spec.architecture_type == "single_agent"
    assert result.validation.passed
    assert result.provider == "mock"
    assert result.warnings


@pytest.mark.parametrize("dup_count", [2, 3])
def test_duplicate_agent_ids_rejected(dup_count: int) -> None:
    with pytest.raises(Exception):
        _spec(
            architecture_type="multi_agent",
            agents=[
                {"id": "same", "name": f"A{i}", "responsibility": "x"}
                for i in range(dup_count)
            ],
        )


def test_over_split_warns() -> None:
    agents = [
        {"id": f"n{i}", "name": f"N{i}", "responsibility": "x"} for i in range(8)
    ]
    edges = [
        {"source": f"n{i}", "target": f"n{i + 1}"} for i in range(7)
    ]
    spec = _spec(
        architecture_type="workflow",
        complexity_level="moderate",
        agents=agents,
        workflow_edges=edges,
    )
    report = validate(spec)
    assert any(i.code == "W007" for i in report.warnings)
