"""生成的网页真能打开吗？—— 起一个真实子进程，用 HTTP 敲一遍。

比"文件里有 index.html"更有意义的地方：
server.py 有没有导入问题、端口能不能起、路由回不回、缺 Key 时给的是
可读提示还是白屏。这几件事只有真跑才知道。
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from app.agents.code_generator import generate_code
from app.schemas.architecture import ArchitectureSpec
from app.schemas.codegen import CodegenRequest
from app.schemas.components import ComponentPlan, ComponentSpec
from app.schemas.requirement import RequirementSpec


def _payload() -> CodegenRequest:
    requirement = RequirementSpec(
        agent_name="工单分流助手",
        goal="把新工单分到技术/账单/物流三类并自动回复常见问题",
        inputs=["工单标题与正文"],
        outputs=["分类结果", "回复文本"],
        features=["读取新工单", "判断类别", "生成回复"],
        acceptance_criteria=["分类结果必须是三类之一"],
        confidence=0.8,
    )
    architecture = ArchitectureSpec(
        architecture_type="workflow",
        reasoning_summary="流程固定：先分类再回复",
        decision_factors=["顺序确定"],
        agents=[
            {
                "id": "classify",
                "name": "分类节点",
                "responsibility": "判断工单属于技术/账单/物流哪一类",
            },
            {
                "id": "reply",
                "name": "回复节点",
                "responsibility": "根据类别生成对客户的回复",
            },
        ],
        workflow_edges=[{"source": "classify", "target": "reply"}],
        required_components=[
            {"type": "prompt", "name": "classify_prompt", "purpose": "约束分类判断"},
            {"type": "prompt", "name": "reply_prompt", "purpose": "约束回复语气"},
            {"type": "harness", "name": "agent_harness", "purpose": "运行循环"},
        ],
    )
    plan = ComponentPlan(
        summary="最小可用链路",
        components=[
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
        ],
    )
    return CodegenRequest(
        requirement=requirement, architecture=architecture, components=plan
    )


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_ready(url: str, proc: subprocess.Popen, timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return False
        try:
            with urllib.request.urlopen(url + "/api/health", timeout=0.5) as resp:
                if resp.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(0.2)
    return False


def _post(url: str, payload: dict) -> dict:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:  # 500 也是 JSON，照样读出来断言
        return json.loads(exc.read())


@pytest.fixture()
def served_project(tmp_path: Path):
    """把项目写到磁盘，起 server.py，用完杀掉。"""
    result = generate_code(_payload())
    project = tmp_path / "agent_app"
    for f in result.files:
        target = project / f.path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f.content, encoding="utf-8")

    # 把模型配置清空：这条测的是"没配 Key 时会不会给可读提示"，
    # 而不是"这台机器上有没有 Key"。
    env = {k: v for k, v in os.environ.items() if not k.startswith("OPENAI_")}
    env["PYTHONUTF8"] = "1"
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-u", "server.py", "--no-browser", "--port", str(port)],
        cwd=str(project),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    url = f"http://127.0.0.1:{port}"
    try:
        if not _wait_ready(url, proc):
            output = ""
            if proc.stdout is not None:
                proc.terminate()
                output = proc.stdout.read() or ""
            pytest.fail(f"网页没起来，进程输出：\n{output[-1200:]}")
        yield url, project
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:  # pragma: no cover
                proc.kill()


def test_page_is_served_with_agent_name(served_project) -> None:
    url, _ = served_project
    with urllib.request.urlopen(url + "/", timeout=5) as resp:
        assert resp.status == 200
        assert "text/html" in resp.headers.get("Content-Type", "")
        html = resp.read().decode("utf-8")
    assert "工单分流助手" in html
    assert "/api/chat" in html


def test_meta_exposes_nodes_and_sample(served_project) -> None:
    url, _ = served_project
    with urllib.request.urlopen(url + "/api/meta", timeout=5) as resp:
        meta = json.loads(resp.read())
    assert meta["name"] == "工单分流助手"
    assert [n["id"] for n in meta["nodes"]] == ["classify", "reply"]


def test_missing_key_is_reported_not_white_screen(served_project) -> None:
    """缺 Key 时：health 说缺谁，对话给出可读错误 —— 而不是页面白屏或空 500。"""
    url, _ = served_project
    with urllib.request.urlopen(url + "/api/health", timeout=5) as resp:
        health = json.loads(resp.read())
    assert health["ok"] is True
    assert "OPENAI_API_KEY" in health["missing"]

    data = _post(url + "/api/chat", {"message": "这条工单怎么分"})
    assert data["ok"] is False
    assert data["error"]
    assert "OPENAI" in data["error"] or "模型" in data["error"]


def test_bad_json_is_400(served_project) -> None:
    url, _ = served_project
    request = urllib.request.Request(
        url + "/api/chat", data=b"not json", headers={"Content-Type": "application/json"}
    )
    with pytest.raises(urllib.error.HTTPError) as info:
        urllib.request.urlopen(request, timeout=5)
    assert info.value.code == 400


def test_answers_and_history_are_accepted(served_project) -> None:
    """补答与多轮上下文都是可选字段，服务端不能因为多传就 500。"""
    url, _ = served_project
    data = _post(
        url + "/api/chat",
        {
            "message": "那它呢",
            "answers": {"category": "技术"},
            "history": [
                {"role": "user", "content": "你好"},
                {"role": "assistant", "content": "你好，请讲"},
                {"role": "system", "content": "这条应被丢掉"},
            ],
        },
    )
    assert set(data) >= {"ok", "output", "error"}
