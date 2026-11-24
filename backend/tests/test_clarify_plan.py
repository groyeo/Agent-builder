"""缺口清单（clarify）的规则测试。

核心断言只有一条：**用户已经说过的不要再问，没说到的必须问**。
这条错了，生成出来的 Agent 要么变成盘问机器，要么继续替用户瞎猜。
"""

from __future__ import annotations

import json

from app.engine.clarify_plan import (
    MAX_QUESTIONS,
    design_questions,
    pick_domain,
    plan_slots,
    sample_input,
)
from app.schemas.requirement import ClarifyingQuestion, RequirementSpec


def _req(**kwargs) -> RequirementSpec:
    base = dict(
        agent_name="助理",
        goal="帮用户处理事情",
        inputs=["一句话需求"],
        outputs=["方案"],
        features=["理解需求"],
        confidence=0.7,
    )
    base.update(kwargs)
    return RequirementSpec(**base)


def _travel() -> RequirementSpec:
    return _req(
        agent_name="旅行规划助手",
        goal="根据用户偏好生成多天行程与预算表",
        inputs=["自然语言出行需求"],
        outputs=["行程表", "预算表"],
        features=["规划行程", "估算预算", "推荐本地美食"],
    )


def test_travel_domain_is_picked() -> None:
    domain = pick_domain(_travel())
    assert domain is not None and domain.key == "travel"


def test_travel_slots_skip_what_user_already_said() -> None:
    """截图里那句话：有天数、预算、偏好，缺出发地和日期。"""
    slots, domain = plan_slots(_travel())
    ids = [s.id for s in slots]
    assert {"origin", "dates", "days", "budget"} <= set(ids)
    sample = sample_input(_travel(), domain)
    assert "成都" in sample and "3000" in sample
    # 示例本身必须真的缺 origin / dates，否则演示不出提问
    assert set(domain.sample_missing) <= set(domain.sample_has) | {"origin", "dates"}


def test_design_questions_stay_out_of_runtime() -> None:
    """“渠道用对话还是 API”这类问题在构建时问，运行时再问就是噪音。"""
    req = _travel()
    req.clarifying_questions = [
        ClarifyingQuestion(
            id="q_channel",
            question="用户输入通过什么渠道进入（对话界面、API、还是其他）？",
            why="决定交互形态",
            blocking=True,
        ),
        ClarifyingQuestion(
            id="q_nice",
            question="需要推荐餐厅吗？",
            why="锦上添花",
            blocking=False,
        ),
    ]
    slots, _ = plan_slots(req)
    assert all("渠道" not in s.question for s in slots)
    assert [q for q, _ in design_questions(req)] == [
        "用户输入通过什么渠道进入（对话界面、API、还是其他）？"
    ]


def test_unknown_domain_falls_back_gracefully() -> None:
    req = _req(agent_name="会议纪要助手", goal="把录音转成纪要并提取待办")
    slots, domain = plan_slots(req)
    assert domain is None
    assert slots == []
    assert sample_input(req, domain)


def test_question_quota_is_sane() -> None:
    """一次问太多，用户会直接关掉。"""
    assert 2 <= MAX_QUESTIONS <= 5


def test_sample_is_json_safe() -> None:
    """示例输入会嵌进 notebook.ipynb 的代码单元，必须能被 JSON 转义。"""
    slots, domain = plan_slots(_travel())
    assert json.dumps(sample_input(_travel(), domain), ensure_ascii=False)
