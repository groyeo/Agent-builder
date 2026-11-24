from app.agents.component_designer import ComponentDesigner
from app.core.llm.mock import MockAdapter
from app.schemas.architecture import ArchitectureSpec
from app.schemas.components import ComponentPlan, ComponentSpec
from app.schemas.requirement import RequirementSpec
from app.validators.component_validator import validate


def _req(**kw) -> RequirementSpec:
    base = {"agent_name": "测试 Agent", "goal": "把 A 变成 B"}
    base.update(kw)
    return RequirementSpec(**base)


def _arch(**kw) -> ArchitectureSpec:
    base = {
        "architecture_type": "single_agent",
        "reasoning_summary": "目标单一",
        "agents": [
            {"id": "main_agent", "name": "Main", "responsibility": "全流程"},
        ],
        "required_components": [
            {"type": "prompt", "name": "main_prompt", "purpose": "行为指令"},
            {"type": "harness", "name": "agent_harness", "purpose": "运行框架"},
        ],
    }
    base.update(kw)
    return ArchitectureSpec(**base)


def _plan(components: list[dict]) -> ComponentPlan:
    return ComponentPlan(
        components=[ComponentSpec(**c) for c in components], summary="测试用"
    )


_GOOD = [
    {
        "type": "prompt",
        "name": "main_prompt",
        "purpose": "约束输出格式",
        "interface": {"role": "system"},
        "owner_agents": ["main_agent"],
        "test_requirements": ["输出符合预期格式"],
    },
    {
        "type": "harness",
        "name": "agent_harness",
        "purpose": "防止死循环",
        "interface": {"max_steps": 8, "timeout_s": 60},
        "owner_agents": ["main_agent"],
        "test_requirements": ["达到上限安全终止"],
    },
]


def test_valid_plan_passes() -> None:
    report = validate(_plan(_GOOD), _req(), _arch())
    assert report.passed, [i.message for i in report.errors]


def test_missing_interface_field_fails() -> None:
    components = [
        {**_GOOD[0], "interface": {}},  # prompt 缺 role
        _GOOD[1],
    ]
    report = validate(_plan(components), _req(), _arch())
    assert any(i.code == "C004" for i in report.errors)


def test_undeclared_tool_used_by_agent_fails() -> None:
    arch = _arch(
        agents=[
            {
                "id": "main_agent",
                "name": "Main",
                "responsibility": "x",
                "tools": ["ghost_tool"],
            }
        ]
    )
    report = validate(_plan(_GOOD), _req(), arch)
    assert any(i.code == "C002" for i in report.errors)


def test_dependency_cycle_fails() -> None:
    components = [
        {**_GOOD[0], "depends_on": ["agent_harness"]},
        {**_GOOD[1], "depends_on": ["main_prompt"]},
    ]
    report = validate(_plan(components), _req(), _arch())
    assert any(i.code == "C007" for i in report.errors)


def test_unknown_dependency_fails() -> None:
    components = [{**_GOOD[0], "depends_on": ["nope"]}, _GOOD[1]]
    report = validate(_plan(components), _req(), _arch())
    assert any(i.code == "C006" for i in report.errors)


def test_external_dependency_without_tool_fails() -> None:
    report = validate(
        _plan(_GOOD), _req(external_dependencies=["工单系统"]), _arch()
    )
    assert any(i.code == "C011" for i in report.errors)


def test_missing_rag_warns() -> None:
    report = validate(_plan(_GOOD), _req(features=["检索内部知识库"]), _arch())
    assert any(i.code == "C010" for i in report.warnings)


def test_orphan_required_component_warns() -> None:
    components = [
        _GOOD[0],
        _GOOD[1],
        {
            "type": "skill",
            "name": "unused_skill",
            "purpose": "没人用",
            "interface": {"steps": []},
            "owner_agents": [],
            "test_requirements": ["x"],
        },
    ]
    report = validate(_plan(components), _req(), _arch())
    assert any(i.code == "C008" for i in report.warnings)


def test_bad_component_name_rejected() -> None:
    import pytest

    with pytest.raises(Exception):
        ComponentSpec(
            type="prompt", name="BadName", purpose="x", interface={"role": "s"}
        )


def test_fallback_designer_produces_valid_plan() -> None:
    result = ComponentDesigner(llm=MockAdapter()).design(
        _req(external_dependencies=["工单系统"]),
        _arch(
            agents=[
                {
                    "id": "main_agent",
                    "name": "Main",
                    "responsibility": "x",
                    "tools": ["ticket_tool"],
                }
            ],
            required_components=[
                {"type": "prompt", "name": "main_prompt", "purpose": "行为指令"},
                {"type": "harness", "name": "agent_harness", "purpose": "运行框架"},
                {"type": "tool", "name": "ticket_tool", "purpose": "读工单"},
            ],
        ),
    )
    assert result.provider == "mock"
    assert result.validation.passed
    assert len(result.plan.components) == 3
