"""代码生成测试。

最有力的一条是 `test_generated_project_passes_its_own_tests`：
把生成结果写到临时目录，用真实的 pytest 跑生成项目自带的测试。
如果脚手架连自己的测试都过不了，"可运行"就是空话。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import zipfile
from io import BytesIO
from pathlib import Path

import pytest

from app.agents.code_generator import generate_code, to_zip
from app.engine.code_generator import render_tree
from app.schemas.codegen import CodegenRequest, GeneratedFile
from app.schemas.components import ComponentPlan, ComponentSpec
from app.schemas.architecture import ArchitectureSpec
from app.schemas.requirement import ClarifyingQuestion, RequirementSpec


def _requirement() -> RequirementSpec:
    return RequirementSpec(
        agent_name="工单分流助手",
        goal="把新工单分到技术/账单/物流三类并自动回复常见问题",
        inputs=["工单标题与正文"],
        outputs=["分类结果", "回复文本"],
        features=["读取新工单", "判断类别", "生成回复"],
        external_dependencies=["工单系统 REST API"],
        acceptance_criteria=["分类结果必须是三类之一", "无法处理时给出转人工标记"],
        confidence=0.8,
    )


def _architecture() -> ArchitectureSpec:
    return ArchitectureSpec(
        architecture_type="workflow",
        reasoning_summary="流程固定：先分类再回复",
        decision_factors=["顺序确定"],
        agents=[
            {
                "id": "classify",
                "name": "分类节点",
                "responsibility": "判断工单属于技术/账单/物流哪一类",
                "tools": ["read_ticket"],
            },
            {
                "id": "reply",
                "name": "回复节点",
                "responsibility": "根据类别生成对客户的回复",
            },
        ],
        workflow_edges=[
            {"source": "classify", "target": "reply", "condition": "分类成功"}
        ],
        required_components=[
            {"type": "prompt", "name": "classify_prompt", "purpose": "约束分类判断"},
            {"type": "prompt", "name": "reply_prompt", "purpose": "约束回复语气"},
            {"type": "harness", "name": "agent_harness", "purpose": "运行循环"},
            {"type": "tool", "name": "read_ticket", "purpose": "读取工单"},
        ],
    )


def _plan(*, with_rag: bool = False, with_memory: bool = False) -> ComponentPlan:
    components = [
        ComponentSpec(
            type="prompt",
            name="classify_prompt",
            purpose="没有它模型会自由发挥分类标准",
            owner_agents=["classify"],
            interface={"role": "工单分类专家"},
            test_requirements=["同一工单重复分类结果应一致"],
        ),
        ComponentSpec(
            type="prompt",
            name="reply_prompt",
            purpose="没有它回复语气不受控",
            owner_agents=["reply"],
            interface={"role": "客服回复撰写人"},
            test_requirements=["回复必须包含类别与下一步动作"],
        ),
        ComponentSpec(
            type="harness",
            name="agent_harness",
            purpose="没有它就没有步数与超时约束",
            owner_agents=["classify", "reply"],
            interface={"max_steps": 4, "timeout_s": 60, "retry": 1},
            test_requirements=["超过 max_steps 必须终止"],
        ),
        ComponentSpec(
            type="tool",
            name="read_ticket",
            purpose="没有它读不到工单原文",
            owner_agents=["classify"],
            interface={
                "input_schema": {
                    "type": "object",
                    "properties": {"ticket_id": {"type": "string"}},
                    "required": ["ticket_id"],
                },
                "output_desc": "工单标题与正文",
            },
            test_requirements=["传入不存在的 id 应抛错"],
        ),
    ]
    if with_rag:
        components.append(
            ComponentSpec(
                type="rag",
                name="faq_kb",
                purpose="没有它无法回答常见问题",
                owner_agents=["reply"],
                interface={"knowledge_source": "客服 FAQ 文档", "chunk_strategy": "按问答对切分"},
                test_requirements=["检索结果必须来自 FAQ 文档"],
            )
        )
    if with_memory:
        components.append(
            ComponentSpec(
                type="memory",
                name="session_memory",
                purpose="同一工单的多轮追问需要上下文",
                owner_agents=["reply"],
                interface={"scope": "session", "key_fields": ["ticket_id"]},
                test_requirements=["同一 ticket_id 能读到上一轮内容"],
            )
        )
    return ComponentPlan(components=components, summary="四个核心组件", notes=[])


def _payload(**kw) -> CodegenRequest:
    return CodegenRequest(
        requirement=_requirement(), architecture=_architecture(), components=_plan(**kw)
    )


def test_generates_core_files() -> None:
    result = generate_code(_payload())
    paths = {f.path for f in result.files}
    assert {
        "README.md",
        "requirements.txt",
        "config.yaml",
        "main.py",
        "conftest.py",
        "agent/prompts.py",
        "agent/tools.py",
        "agent/harness.py",
        "agent/router.py",
        "agent/llm.py",
        "tests/test_prompts.py",
        "tests/test_tools.py",
        "tests/test_router.py",
    } <= paths
    assert result.stats.files == len(result.files)
    assert result.stats.python_files >= 8


def test_no_model_token_used() -> None:
    result = generate_code(_payload())
    assert result.generator == "template"


def test_validation_passes() -> None:
    result = generate_code(_payload())
    assert result.validation.passed, [i.message for i in result.validation.issues]


def test_optional_components_add_files() -> None:
    result = generate_code(_payload(with_rag=True, with_memory=True))
    paths = {f.path for f in result.files}
    assert "agent/rag.py" in paths
    assert "agent/memory.py" in paths
    assert result.validation.passed


def test_tool_stub_raises_not_returns_fake() -> None:
    result = generate_code(_payload())
    tools = next(f for f in result.files if f.path == "agent/tools.py")
    assert "NotImplementedError" in tools.content
    assert "ticket_id" in tools.content  # input_schema 被写进注释


def test_conditions_are_kept_as_comments() -> None:
    result = generate_code(_payload())
    router = next(f for f in result.files if f.path == "agent/router.py")
    # 条件文本是自然语言，生成成 if 语句会是假严谨
    assert "进入条件：分类成功" in router.content


def test_unresolved_blocking_question_warns() -> None:
    payload = _payload()
    payload.requirement.clarifying_questions = [
        ClarifyingQuestion(id="q1", question="工单从哪来？", why="决定工具实现", blocking=True)
    ]
    result = generate_code(payload)
    assert any("阻塞问题未澄清" in w for w in result.warnings)


def test_zip_contains_project_folder() -> None:
    result = generate_code(_payload())
    with zipfile.ZipFile(BytesIO(to_zip(result))) as zf:
        names = zf.namelist()
    assert all(n.startswith(f"{result.project_name}/") for n in names)
    assert f"{result.project_name}/agent/router.py" in names


def test_tree_has_nested_structure() -> None:
    files = [
        GeneratedFile(path="a/b.py", content="x"),
        GeneratedFile(path="README.md", content="x"),
    ]
    tree = render_tree(files)
    assert "README.md" in tree
    assert "a/" in tree and "b.py" in tree


def test_generated_project_passes_its_own_tests(tmp_path: Path) -> None:
    result = generate_code(_payload())
    project = tmp_path / result.project_name
    for f in result.files:
        target = project / f.path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f.content, encoding="utf-8")

    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q"],
        cwd=project,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-1500:]


def test_generated_router_runs_with_injected_chat(tmp_path: Path) -> None:
    """不依赖 pytest：直接当模块跑一次，确认编排真的能走通。"""
    result = generate_code(_payload())
    project = tmp_path / result.project_name
    for f in result.files:
        target = project / f.path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f.content, encoding="utf-8")

    script = (
        "import json\n"
        "from agent.router import handle\n"
        "r = handle('测试工单', chat_fn=lambda m: 'ok')\n"
        "print(json.dumps({'ok': r['ok'], 'nodes': sorted(r['output']), 'steps': r['steps']}))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=project,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert '"classify"' in proc.stdout and '"reply"' in proc.stdout


def test_cyclic_graph_falls_back_with_warning() -> None:
    base = _architecture()
    # model_copy(update=...) 不触发校验，会留下 dict；这里走 model_validate 保证类型正确
    cyclic = ArchitectureSpec.model_validate(
        {
            **base.model_dump(),
            "workflow_edges": [
                {"source": "classify", "target": "reply"},
                {"source": "reply", "target": "classify"},
            ],
        }
    )
    payload = CodegenRequest(
        requirement=_requirement(), architecture=cyclic, components=_plan()
    )
    result = generate_code(payload)
    assert any("环" in w for w in result.warnings)


def _write_project(result, tmp_path: Path) -> Path:
    project = tmp_path / result.project_name
    for f in result.files:
        target = project / f.path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f.content, encoding="utf-8")
    return project


def test_generates_single_command_entry_and_notebook() -> None:
    """单命令启动与 notebook 是入口体验的一半，缺了就等于没做。"""
    result = generate_code(_payload())
    paths = {f.path for f in result.files}
    assert {"start.py", "run.bat", "run.sh", "notebook.ipynb", "agent/clarify.py"} <= paths

    start = next(f for f in result.files if f.path == "start.py")
    for flag in ("--check", "ensure_env", "ensure_deps"):
        assert flag in start.content

    nb = next(f for f in result.files if f.path == "notebook.ipynb")
    notebook = json.loads(nb.content)  # 打不开的 notebook 等于没给
    assert notebook["nbformat"] == 4
    source = "".join("".join(c["source"]) for c in notebook["cells"])
    assert "handle(" in source


def test_generates_openable_web_page() -> None:
    """每个目标 Agent 都要有一个"点开就能用"的网页，而不是只有命令行。"""
    result = generate_code(_payload())
    paths = {f.path for f in result.files}
    assert {"server.py", "web/index.html"} <= paths

    server = next(f for f in result.files if f.path == "server.py")
    # 只监听本机、标准库起服务、两个接口齐备
    for token in ("ThreadingHTTPServer", "127.0.0.1", "/api/chat", "/api/health"):
        assert token in server.content
    # 名称与节点写进服务：页面不需要再读设计产物
    assert "工单分流助手" in server.content
    assert "classify" in server.content

    page = next(f for f in result.files if f.path == "web/index.html")
    assert "工单分流助手" in page.content
    assert "/api/chat" in page.content
    assert "<script" in page.content and "@media" in page.content

    start = next(f for f in result.files if f.path == "start.py")
    assert "--web" in start.content and "serve(" in start.content
    # --port 的值不能混进"要处理的那句话"，否则会变成把端口号发给模型
    assert '"--port"' in start.content

    runbat = next(f for f in result.files if f.path == "run.bat")
    assert "--web" in runbat.content
    runsh = next(f for f in result.files if f.path == "run.sh")
    assert "--web" in runsh.content

    # 网页补答走 answers，不靠把回答拼回原句再让正则猜
    router = next(f for f in result.files if f.path == "agent/router.py")
    assert "answers: dict[str, str] | None = None" in router.content
    assert "preset=answers" in router.content
    # 多轮上下文：网页传来的 history 要进每个节点的 messages
    assert "history: list[dict[str, str]] | None = None" in router.content
    assert 'state.get("history")' in router.content
    clarify = next(f for f in result.files if f.path == "agent/clarify.py")
    assert "preset" in clarify.content


def test_generated_clarify_asks_what_the_request_misses(tmp_path: Path) -> None:
    """照着那句话跑：有天数/预算/偏好，缺出发地和日期，就必须问这两个。"""
    travel = RequirementSpec(
        agent_name="旅行规划助手",
        goal="根据用户偏好生成多天行程与预算表",
        inputs=["自然语言出行需求"],
        outputs=["行程表", "预算表"],
        features=["规划行程", "估算预算", "推荐本地美食"],
        confidence=0.7,
    )
    payload = CodegenRequest(
        requirement=travel, architecture=_architecture(), components=_plan()
    )
    project = _write_project(generate_code(payload), tmp_path)

    script = (
        "import json\n"
        "from agent.clarify import pending, extract\n"
        "text = '帮我规划成都3天旅行，预算3000元，喜欢吃货和熊猫'\n"
        "print(json.dumps({\n"
        "  'ask': [s.id for s in pending(text)],\n"
        "  'got': extract(text),\n"
        "}, ensure_ascii=False))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script], cwd=project, capture_output=True, text=True, timeout=120
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    data = json.loads(proc.stdout)
    assert {"origin", "dates"} <= set(data["ask"])
    assert {"days", "budget"} <= set(data["got"])


def test_start_script_runs_check_and_reports_gaps(tmp_path: Path) -> None:
    """`python start.py --check` 必须能自己跑起来并说清缺什么。"""
    project = _write_project(generate_code(_payload()), tmp_path)
    proc = subprocess.run(
        [sys.executable, "start.py", "--check"],
        cwd=project,
        capture_output=True,
        text=True,
        timeout=180,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    assert proc.returncode == 1, proc.stdout[-2000:]  # 没填 Key，就该是非 0
    assert (project / ".env").exists()  # 顺手把 .env 生成了
    assert "OPENAI_API_KEY" in proc.stdout


# ---------------------------------------------------------------- .env 载入回归
def _clean_env() -> dict[str, str]:
    """去掉宿主环境里的 OPENAI_*，避免测试"碰巧通过"。"""
    return {k: v for k, v in os.environ.items() if not k.startswith("OPENAI_")}


def test_generated_runtime_actually_loads_dotenv(tmp_path: Path) -> None:
    """回归：.env 必须真的被运行时读到。

    曾出现过的问题：scripts/check_env.py 自己读了 .env 文件，而 agent/llm.py
    只看 os.environ —— 自检通过、一跑就报"缺少 OPENAI_BASE_URL"。
    这里在子进程里跑生成物自己的模块，确认 .env → os.environ 这条链是通的。
    """
    result = generate_code(_payload())
    project = _write_project(result, tmp_path)
    assert (project / "agent" / "env.py").exists()

    (project / ".env").write_text(
        "OPENAI_BASE_URL=https://api.deepseek.com/v1\n"
        "OPENAI_API_KEY=sk-test-123\n"
        "OPENAI_MODEL=deepseek-chat\n",
        encoding="utf-8",
    )
    out = subprocess.run(
        [
            sys.executable,
            "-c",
            "from agent.llm import _settings, preflight;"
            "print('missing', preflight());print(_settings())",
        ],
        cwd=project,
        capture_output=True,
        text=True,
        env=_clean_env(),
    )
    assert out.returncode == 0, out.stderr
    assert "missing []" in out.stdout
    assert "sk-test-123" in out.stdout


def test_missing_config_message_is_actionable(tmp_path: Path) -> None:
    """缺配置时的报错必须能指导动作：路径 + 三行示例 + 复检命令 + 申请入口。"""
    result = generate_code(_payload())
    project = _write_project(result, tmp_path)

    out = subprocess.run(
        [
            sys.executable,
            "-c",
            "from agent.llm import diagnose, preflight;"
            "m = preflight();print(m);print(diagnose(m))",
        ],
        cwd=project,
        capture_output=True,
        text=True,
        env=_clean_env(),
    )
    assert out.returncode == 0, out.stderr
    text = out.stdout
    assert "OPENAI_BASE_URL" in text and "OPENAI_MODEL" in text and "OPENAI_API_KEY" in text
    assert str(project / ".env") in text   # 明确告诉用户读的是哪个文件
    assert "start.py --check" in text      # 给出复检命令
    assert "sk-你的Key" in text            # 给出可照抄的示例行
    assert "https://" in text              # 给出申请入口


def test_start_script_checks_model_config_before_asking() -> None:
    """体检必须在提问之前：答完两个问题才发现没配 Key，等于白答。"""
    result = generate_code(_payload())
    start = next(f.content for f in result.files if f.path == "start.py")
    assert "def check_model_config" in start
    assert "def prepare" in start
    # main() 里必须先 prepare 再 run_once/chat_loop
    main_body = start[start.index("def main(argv"):]
    assert main_body.index("prepare()") < main_body.index("run_once(text")
    assert main_body.index("prepare()") < main_body.index("chat_loop()")

    llm = next(f.content for f in result.files if f.path == "agent/llm.py")
    assert "from .env import" in llm
    assert "def preflight" in llm


def test_confirmed_line_shows_what_was_confirmed() -> None:
    """"[已确认 2 项]" 只报个数等于没说 —— 必须带上确认成了什么。

    曾出现的问题：连着几次都只看到"[已确认 2 项]"，看不出到底确认了哪两项，
    只能去翻【已确认的关键信息】那段。现在直接把 k=v 拼在同一行。
    """
    result = generate_code(_payload())
    for path in ("start.py", "main.py"):
        content = next(f.content for f in result.files if f.path == path)
        assert "[已确认" in content
        assert "brief" in content
        assert 'f" {detail}"' in content


def test_confirmed_line_shows_what_was_confirmed() -> None:
    """"[已确认 2 项]" 只报个数等于没说 —— 必须带上确认成了什么。

    曾出现的问题：连着几次都只看到"[已确认 2 项]"，看不出到底确认了哪两项，
    只能去翻【已确认的关键信息】那段。现在直接把 k=v 拼在同一行。
    """
    result = generate_code(_payload())
    for path in ("start.py", "main.py"):
        content = next(f.content for f in result.files if f.path == path)
        assert "[已确认" in content
        assert "brief" in content
        assert 'f" {detail}"' in content
