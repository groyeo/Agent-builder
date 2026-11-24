"""测试执行器测试。

这里验的是"真跑"这件事本身：pytest 有没有被执行、结果解析对不对、
失败能不能把原因带回来、超时会不会强杀、结果会不会落盘。
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from app.core.llm.mock import MockAdapter
from app.engine import project_store, test_runner
from app.engine.job_store import job_store
from app.engine.pipeline import run_pipeline
from app.main import app

RAW = "做一个销售周报 Agent，读取销售明细 Excel，按区域汇总并生成周报邮件草稿。"

client = TestClient(app)


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


def test_runs_generated_tests_and_all_pass(done_job, isolated_data_dir) -> None:
    project_id = done_job.result["project_id"]
    resp = client.post(f"/api/projects/{project_id}/tests")
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]

    assert data["ok"] is True
    assert data["total"] >= 1
    assert data["failed"] == 0 and data["error"] == 0
    assert data["passed"] == data["total"]
    # 用例名要能认出来是哪个文件（不是一串点路径）
    assert all(c["name"].startswith("tests/") for c in data["cases"])
    assert "passed" in data["output_tail"]

    # 结果要落盘，刷新页面还能读回来
    again = client.get(f"/api/projects/{project_id}/tests")
    assert again.status_code == 200
    assert again.json()["data"]["total"] == data["total"]


def test_failure_is_reported_not_raised(done_job) -> None:
    """用例挂了不是接口错误：照样 200，但 ok=false 且带回失败原因。"""
    project_id = done_job.result["project_id"]
    files = project_store._root() / project_id / project_store.FILES_DIR  # noqa: SLF001
    target = files / "tests" / "test_broken.py"
    target.write_text(
        "def test_on_purpose():\n    assert 1 == 2, '故意失败，用来验证失败能被报出来'\n",
        encoding="utf-8",
    )

    resp = client.post(f"/api/projects/{project_id}/tests")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["ok"] is False
    assert data["failed"] >= 1
    broken = next(c for c in data["cases"] if c["name"].endswith("test_on_purpose"))
    assert broken["status"] == "failed"
    assert "故意失败" in broken["message"]


def test_meta_records_last_result(done_job) -> None:
    """project.json 要带上测试结论，列表页才能不点进去就看出红绿。"""
    project_id = done_job.result["project_id"]
    client.post(f"/api/projects/{project_id}/tests")
    meta = project_store.get_meta(project_id)
    assert meta["tests_ok"] is True
    assert meta["tests_total"] >= 1
    assert meta["tests_ran_at"] > 0


def test_no_tests_returns_409(done_job) -> None:
    project_id = done_job.result["project_id"]
    files = project_store._root() / project_id / project_store.FILES_DIR  # noqa: SLF001
    for path in (files / "tests").glob("*.py"):
        path.unlink()
    (files / "tests").rmdir()

    resp = client.post(f"/api/projects/{project_id}/tests")
    assert resp.status_code == 409
    assert "没有 tests" in resp.json()["detail"]


def test_last_result_404_before_first_run(done_job) -> None:
    project_id = done_job.result["project_id"]
    assert client.get(f"/api/projects/{project_id}/tests").status_code == 404


def test_missing_project_404() -> None:
    assert client.post("/api/projects/nope-000000/tests").status_code == 404


def test_pretty_name_handles_odd_classnames() -> None:
    assert (
        test_runner._pretty_name("a.b.files.tests.test_x", "test_y")  # noqa: SLF001
        == "tests/test_x.py::test_y"
    )
    # 没有 tests 段时退化成模块名，不能抛异常
    assert test_runner._pretty_name("test_loose", "test_z") == "test_loose.py::test_z"  # noqa: SLF001
    assert test_runner._pretty_name("", "test_alone") == "test_alone"  # noqa: SLF001
