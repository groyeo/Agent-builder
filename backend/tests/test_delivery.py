"""交付清单测试。

三件事必须成立，否则清单就是装饰：
1. 该命中的服务命中，不该命中的不能瞎命中（误报会让人去注册用不上的 Key）；
2. 清单里要求的每个变量，.env.example 里真的有位置；
3. 生成的自检脚本真的能跑 —— 缺项报 1，补齐报 0。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from app.engine.code_generator import generate
from app.engine.delivery_planner import plan_delivery
from app.schemas.architecture import ArchitectureSpec
from app.schemas.components import ComponentPlan
from app.schemas.requirement import RequirementSpec


def _req(**kw) -> RequirementSpec:
    base = dict(
        agent_name="测试 Agent",
        goal="完成一件具体的事",
        confidence=0.8,
    )
    base.update(kw)
    return RequirementSpec(**base)


def _arch(nodes: int = 2) -> ArchitectureSpec:
    return ArchitectureSpec(
        architecture_type="workflow",
        reasoning_summary="流程固定",
        decision_factors=["顺序确定"],
        agents=[
            {
                "id": f"n{i}",
                "name": f"节点{i}",
                "responsibility": f"做第 {i} 步",
            }
            for i in range(nodes)
        ],
        workflow_edges=[],
        required_components=[],
        optional_components=[],
        risks=[],
        complexity_level="simple",
        confidence=0.8,
    )


def _plan(types: list[str] | None = None, purpose: str = "做点事") -> ComponentPlan:
    comps = [
        {
            "type": "prompt",
            "name": "main_prompt",
            "purpose": "定义角色与输出格式",
            "priority": "required",
            "owner_agents": ["n0"],
            "interface": {"role": "你是助手"},
            "depends_on": [],
            "test_requirements": ["提示词非空"],
        }
    ]
    for t in types or []:
        comps.append(
            {
                "type": t,
                "name": f"{t}_comp",
                "purpose": purpose,
                "priority": "required",
                "owner_agents": ["n0"],
                "interface": {"role": purpose} if t == "prompt" else {"input_schema": {}},
                "depends_on": [],
                "test_requirements": ["能跑"],
            }
        )
    return ComponentPlan(components=comps, summary="测试用组件清单")


def test_llm_is_always_required() -> None:
    d = plan_delivery(_req(), _arch(), _plan())
    llm = next(r for r in d.resources if r.kind == "llm")
    assert llm.required
    assert "OPENAI_API_KEY" in llm.fallback
    assert llm.signup_url and llm.steps


def test_travel_agent_hits_map_key() -> None:
    req = _req(
        agent_name="旅行规划助手",
        goal="根据用户偏好规划三日游，给出地图路线与景点顺序",
        features=["查询景点", "规划路线", "生成行程"],
        external_dependencies=["高德地图开放平台"],
    )
    d = plan_delivery(req, _arch(), _plan())
    amap = next((r for r in d.resources if r.key == "amap"), None)
    assert amap is not None, "旅行规划必须命中地图 Key"
    assert amap.env_vars == ["AMAP_API_KEY"]
    assert amap.required and amap.signup_url
    assert any("实名" in s for s in amap.steps)


def test_customer_service_agent_needs_handbook_and_email() -> None:
    req = _req(
        agent_name="客服助手",
        goal="回答客户关于退换货的问题，生成邮件回复草稿",
        inputs=["客户的文字提问"],
        outputs=["回复文本", "邮件草稿"],
        features=["检索客服手册", "回复常见问题"],
        external_dependencies=["客服知识库"],
    )
    plan = _plan(["rag"], purpose="检索客服手册中的退换货政策")
    d = plan_delivery(req, _arch(), plan)
    keys = {r.key for r in d.resources}
    assert "knowledge_base" in keys, "客服 Agent 必须提示准备客服手册"
    assert "smtp" in keys, "有邮件输出就该提示 SMTP 授权码"
    kb = next(r for r in d.resources if r.key == "knowledge_base")
    assert kb.required and kb.target.startswith("knowledge/")
    assert any("转成纯文本" in s for s in kb.steps)


def test_no_false_positive_on_plain_orders() -> None:
    """业务数据里出现「订单」「下单」很常见，不能因此让人去开通微信支付。"""
    req = _req(
        agent_name="销售周报",
        goal="统计订单明细并找出重复下单的异常记录，生成周报",
        inputs=["Excel 销售明细"],
        outputs=["周报"],
    )
    d = plan_delivery(req, _arch(), _plan(["tool"], purpose="读取 Excel 订单明细"))
    keys = {r.key for r in d.resources}
    assert "wechat_pay" not in keys
    assert "input_data" in keys, "有 Excel 输入就该提示放样例数据"


def test_internal_kb_retrieval_does_not_require_web_search() -> None:
    """「检索知识库」是本地 RAG，不该让人去注册联网搜索的 Key。"""
    req = _req(
        agent_name="客服知识库助手",
        goal="检索客服手册回答客户问题，必要时搜索内部文档库",
        inputs=["客户问题"],
        outputs=["回复文本"],
        external_dependencies=["客服手册文档源"],
    )
    plan = _plan(["rag"], purpose="检索知识库中与问题最相关的段落")
    d = plan_delivery(req, _arch(), plan)
    keys = {r.key for r in d.resources}
    assert "serper" not in keys
    assert "knowledge_base" in keys

    # 但明确要求上网找，就该出现
    web = _req(goal="联网搜索竞品最新动态并汇总成周报")
    assert "serper" in {r.key for r in plan_delivery(web, _arch(), _plan()).resources}


def test_internal_kb_retrieval_does_not_require_web_search() -> None:
    """「检索知识库」是本地 RAG，不该让人去注册联网搜索的 Key。"""
    req = _req(
        agent_name="客服知识库助手",
        goal="检索客服手册回答客户问题，必要时搜索内部文档库",
        inputs=["客户问题"],
        outputs=["回复文本"],
        external_dependencies=["客服手册文档源"],
    )
    plan = _plan(["rag"], purpose="检索知识库中与问题最相关的段落")
    d = plan_delivery(req, _arch(), plan)
    keys = {r.key for r in d.resources}
    assert "serper" not in keys
    assert "knowledge_base" in keys

    # 但明确要求上网找，就该出现
    web = _req(goal="联网搜索竞品最新动态并汇总成周报")
    assert "serper" in {r.key for r in plan_delivery(web, _arch(), _plan()).resources}


def test_mcp_component_requires_server() -> None:
    d = plan_delivery(_req(), _arch(), _plan(["mcp"], purpose="调用外部 MCP 工具"))
    mcp = next((r for r in d.resources if r.key == "mcp_server"), None)
    assert mcp is not None
    assert mcp.env_vars == ["MCP_SERVER_URL", "MCP_TOKEN"]


def _write_project(tmp_path: Path, **req_kw) -> Path:
    req = _req(**req_kw)
    arch, plan = _arch(), _plan(["rag"], purpose="检索知识库")
    result = generate(req, arch, plan)
    root = tmp_path / result.project_name
    for f in result.files:
        target = root / f.path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f.content, encoding="utf-8")
    return root


def test_env_example_covers_every_required_var(tmp_path: Path) -> None:
    root = _write_project(tmp_path, goal="检索知识库回答问题，并发送邮件通知")
    env_example = (root / ".env.example").read_text(encoding="utf-8")
    result = generate(_req(goal="检索知识库回答问题，并发送邮件通知"), _arch(), _plan(["rag"], purpose="检索知识库"))
    for r in result.delivery.resources:
        for var in r.env_vars:
            assert f"{var}=" in env_example, f"{var} 没有在 .env.example 里留位置"

    delivery_md = (root / "DELIVERY.md").read_text(encoding="utf-8")
    assert "交付清单" in delivery_md
    assert "从零启动" in delivery_md
    assert "platform.deepseek.com" in delivery_md
    assert "knowledge/" in delivery_md


def _run_check(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "scripts/check_env.py"],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def test_check_env_script_reports_then_passes(tmp_path: Path) -> None:
    root = _write_project(tmp_path)

    # 1) 只有 .env.example，没有 .env：必须报缺 Key 且退出码 1
    first = _run_check(root)
    assert first.returncode == 1, first.stdout + first.stderr
    assert "OPENAI_API_KEY" in first.stdout

    # 2) 补上 Key 还不够：RAG 组件的语料是必需项，空目录必须继续报缺
    (root / ".env").write_text(
        "OPENAI_API_KEY=sk-test\nOPENAI_BASE_URL=https://api.deepseek.com/v1\n"
        "OPENAI_MODEL=deepseek-chat\n",
        encoding="utf-8",
    )
    still_missing = _run_check(root)
    assert still_missing.returncode == 1
    assert "knowledge" in still_missing.stdout

    # 3) 语料放进去之后才算齐
    (root / "knowledge" / "退换货政策.md").write_text("七天无理由退货。", encoding="utf-8")
    second = _run_check(root)
    assert second.returncode == 0, second.stdout + second.stderr
    assert "必需项齐全" in second.stdout


def test_delivery_saved_with_project(tmp_path, monkeypatch) -> None:
    """项目落库要能单独取回交付清单，供前端「交付清单」面板使用。"""
    from fastapi.testclient import TestClient

    from app.engine import project_store
    from app.main import app

    client = TestClient(app)
    result = generate(_req(goal="检索知识库并发送邮件"), _arch(), _plan(["rag"], purpose="检索知识库"))
    meta = project_store.create_project(name="交付测试", codegen=result)

    detail = client.get(f"/api/projects/{meta['id']}").json()["data"]
    assert detail["has_delivery"] is True

    delivery = client.get(f"/api/projects/{meta['id']}/design/delivery").json()["data"]
    assert delivery["resources"]
    assert delivery["startup_steps"]


def test_travel_agent_is_reminded_about_map_key() -> None:
    """用户只说"推荐交通方式"，但旅行 Agent 没地图就只能给文字建议。"""
    req = _req(
        agent_name="旅行规划助手",
        goal="生成多天行程和预算表，推荐交通方式与本地美食",
        inputs=["一句话出行需求"],
        outputs=["行程表", "预算表"],
    )
    d = plan_delivery(req, _arch(), _plan())
    by_key = {r.key: r for r in d.resources}
    assert "amap" in by_key
    assert by_key["amap"].env_vars == ["AMAP_API_KEY"]
    # 可后补：没 Key 也能先跑通，只是方案质量下降
    assert by_key["amap"].required is False

    # 非旅行领域不该被塞地图 Key
    other = plan_delivery(_req(), _arch(), _plan())
    assert "amap" not in {r.key for r in other.resources}


def test_travel_agent_is_reminded_about_map_key() -> None:
    """用户只说"推荐交通方式"，但旅行 Agent 没地图就只能给文字建议。"""
    req = _req(
        agent_name="旅行规划助手",
        goal="生成多天行程和预算表，推荐交通方式与本地美食",
        inputs=["一句话出行需求"],
        outputs=["行程表", "预算表"],
    )
    d = plan_delivery(req, _arch(), _plan())
    by_key = {r.key: r for r in d.resources}
    assert "amap" in by_key
    assert by_key["amap"].env_vars == ["AMAP_API_KEY"]
    # 可后补：没 Key 也能先跑通，只是方案质量下降
    assert by_key["amap"].required is False

    # 非旅行领域不该被塞地图 Key
    other = plan_delivery(_req(), _arch(), _plan())
    assert "amap" not in {r.key for r in other.resources}
