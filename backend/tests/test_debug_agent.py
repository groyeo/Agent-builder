"""Debug Agent 测试。

重点不是"补丁写得好不好"（那是模型的事），而是**边界有没有守住**：
不许改测试、不许改 .env、语法错误要拦、越修越糟要回滚、失败要说失败。
"""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from app.core.llm import LLMError, LLMResult
from app.core.llm.mock import MockAdapter
from app.engine import debug_agent, project_store
from app.engine.job_store import job_store
from app.engine.pipeline import run_pipeline
from app.main import app

RAW = "做一个销售周报 Agent，读取销售明细 Excel，按区域汇总并生成周报邮件草稿。"

client = TestClient(app)

BROKEN_PROMPTS = 'PROMPTS: dict[str, str] = {}\nNODE_IDS: tuple[str, ...] = tuple(PROMPTS)\n'


class _PatchAdapter:
    """按脚本回放补丁的假模型，顺便数一下被调了几次。"""

    name = "fake"

    def __init__(self, payloads: list[dict]) -> None:
        self.payloads = payloads
        self.calls = 0

    def chat(self, messages, *, json_mode=False, temperature=None) -> LLMResult:
        index = min(self.calls, len(self.payloads) - 1)
        self.calls += 1
        return LLMResult(
            content=json.dumps(self.payloads[index], ensure_ascii=False),
            model="fake",
            provider="fake",
        )


@pytest.fixture(autouse=True)
def _reset_store():
    job_store.clear()
    yield
    job_store.clear()


@pytest.fixture
def mock_llm(monkeypatch):
    adapter = MockAdapter()
    for mod in (
        "app.agents.requirement_analyzer",
        "app.agents.architecture_planner",
        "app.agents.component_designer",
    ):
        monkeypatch.setattr(f"{mod}.get_llm", lambda: adapter)
    return adapter


@pytest.fixture
def done_job(mock_llm):
    job = job_store.create(RAW, {"pause_on_blocking": False})
    asyncio.run(run_pipeline(job))
    assert job.status == "done", job.result
    return job


def _files(project_id: str):
    return project_store._root() / project_id / project_store.FILES_DIR  # noqa: SLF001


def _break_prompts(project_id: str) -> str:
    """把 prompts.py 改成空表：import 照样成功，但 test_every_node_has_prompt 挂。"""
    prompts = _files(project_id) / "agent" / "prompts.py"
    backup = prompts.read_text(encoding="utf-8")
    prompts.write_text(BROKEN_PROMPTS, encoding="utf-8")
    return backup


def test_green_project_needs_no_repair(done_job, monkeypatch) -> None:
    """本来就是绿的：不调模型、不动文件，直接说"没啥可修"。"""
    called = {"n": 0}

    def _boom():
        called["n"] += 1
        raise AssertionError("全绿时不该调模型")

    monkeypatch.setattr("app.engine.debug_agent.get_llm", _boom)
    data = client.post(f"/api/projects/{done_job.result['project_id']}/heal").json()["data"]

    assert data["initial"]["ok"] is True
    assert data["needs_human"] is False
    assert data["attempts"] == []
    assert called["n"] == 0


def test_patch_is_applied_and_tests_pass(done_job, monkeypatch) -> None:
    """模型给出正确补丁 → 应用 → 重跑变绿 → 记录改了哪个文件。"""
    project_id = done_job.result["project_id"]
    original = _break_prompts(project_id)

    adapter = _PatchAdapter(
        [
            {
                "diagnosis": "prompts.py 被清空了，节点拿不到系统提示词",
                "confidence": "high",
                "edits": [
                    {"path": "agent/prompts.py", "reason": "恢复提示词表", "content": original}
                ],
            }
        ]
    )
    monkeypatch.setattr("app.engine.debug_agent.get_llm", lambda: adapter)

    resp = client.post(f"/api/projects/{project_id}/heal")
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]

    assert data["initial"]["ok"] is False
    assert data["final"]["ok"] is True
    assert data["needs_human"] is False
    assert data["reverted"] is False
    assert data["changed_files"] == ["agent/prompts.py"]
    assert adapter.calls == 1
    assert "修好了" in resp.json()["message"]
    assert (_files(project_id) / "agent" / "prompts.py").read_text(encoding="utf-8") == original


def test_refuses_to_edit_tests(done_job, monkeypatch) -> None:
    """改断言让它过，是作弊 —— 必须拒收，并如实说没修成。"""
    project_id = done_job.result["project_id"]
    _break_prompts(project_id)
    test_file = _files(project_id) / "tests" / "test_prompts.py"
    before = test_file.read_text(encoding="utf-8")

    adapter = _PatchAdapter(
        [
            {
                "diagnosis": "测试写错了",
                "confidence": "low",
                "edits": [
                    {
                        "path": "tests/test_prompts.py",
                        "reason": "把断言放宽",
                        "content": before.replace("assert not missing", "assert True or not missing"),
                    },
                    {
                        "path": ".env",
                        "reason": "顺手改环境",
                        "content": "OPENAI_API_KEY=sk-x\n",
                    },
                ],
            }
        ]
    )
    monkeypatch.setattr("app.engine.debug_agent.get_llm", lambda: adapter)

    resp = client.post(f"/api/projects/{project_id}/heal")
    data = resp.json()["data"]

    assert data["needs_human"] is True
    assert data["reverted"] is False
    assert data["changed_files"] == []
    assert test_file.read_text(encoding="utf-8") == before

    rejected = " ".join(data["attempts"][0]["rejected"])
    assert "tests/test_prompts.py" in rejected
    assert ".env" in rejected


def test_bad_patch_is_reverted(done_job, monkeypatch) -> None:
    """越修越糟（失败数增加）就整体撤销，别留半成品。"""
    project_id = done_job.result["project_id"]
    _break_prompts(project_id)

    # 补丁本身语法合法，但把 prompts 模块写坏到 import 就炸 —— 失败数会变多
    adapter = _PatchAdapter(
        [
            {
                "diagnosis": "我乱改一气",
                "confidence": "low",
                "edits": [
                    {
                        "path": "agent/prompts.py",
                        "reason": "试试",
                        "content": "raise RuntimeError('把整个模块炸掉')\n",
                    }
                ],
            }
        ]
    )
    monkeypatch.setattr("app.engine.debug_agent.get_llm", lambda: adapter)

    data = client.post(
        f"/api/projects/{project_id}/heal", params={"max_attempts": 1}
    ).json()["data"]

    assert data["needs_human"] is True
    assert data["reverted"] is True
    assert "撤销" in data["note"]
    # 撤销后回到修复前的样子（也就是我故意弄坏的 BROKEN_PROMPTS）
    assert (_files(project_id) / "agent" / "prompts.py").read_text(
        encoding="utf-8"
    ) == BROKEN_PROMPTS


def test_llm_failure_is_reported(done_job, monkeypatch) -> None:
    """模型调用挂掉不能把接口打成 500，要说清"模型没连上"。"""
    project_id = done_job.result["project_id"]
    _break_prompts(project_id)

    def _fail():
        raise LLMError("连不上模型")

    monkeypatch.setattr("app.engine.debug_agent.get_llm", _fail)
    resp = client.post(f"/api/projects/{project_id}/heal")

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["needs_human"] is True
    assert data["attempts"][0]["error"].startswith("模型调用失败")
    assert data["changed_files"] == []


def test_heal_result_is_persisted(done_job, monkeypatch) -> None:
    project_id = done_job.result["project_id"]
    original = _break_prompts(project_id)
    adapter = _PatchAdapter(
        [
            {
                "diagnosis": "恢复提示词",
                "confidence": "high",
                "edits": [{"path": "agent/prompts.py", "reason": "还原", "content": original}],
            }
        ]
    )
    monkeypatch.setattr("app.engine.debug_agent.get_llm", lambda: adapter)
    client.post(f"/api/projects/{project_id}/heal")

    again = client.get(f"/api/projects/{project_id}/heal")
    assert again.status_code == 200
    assert again.json()["data"]["final"]["ok"] is True


def test_last_heal_404_before_first_try(done_job) -> None:
    assert client.get(f"/api/projects/{done_job.result['project_id']}/heal").status_code == 404


def test_validate_edits_blocks_escape(tmp_path) -> None:
    """手工过一遍安检：路径穿越、非 .py 不管，但 .py 语法必须过。"""
    root = tmp_path / "files"
    root.mkdir()
    edits, rejects = debug_agent._validate_edits(  # noqa: SLF001
        root,
        [
            {"path": "../../etc/passwd", "content": "x"},
            {"path": "agent/ok.py", "content": "print(1)\n", "reason": "fine"},
            {"path": "agent/bad.py", "content": "def broken(:\n", "reason": "syntax"},
            {"path": "tests/test_x.py", "content": "def test(): pass\n"},
        ],
    )
    assert [e["path"] for e in edits] == ["agent/ok.py"]
    joined = " ".join(rejects)
    assert "路径越界" in joined
    assert "语法错误" in joined
    assert "不允许改 tests/test_x.py" in joined
