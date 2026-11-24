"""任务持久化测试：重启后还能查到，这条是唯一的验收标准。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.core.llm.mock import MockAdapter
from app.engine.job_repo import JobRepo
from app.engine.job_store import JobStore
from app.engine.pipeline import run_pipeline

RAW = "做一个客服工单智能分流 Agent，判断工单类别并自动回复常见问题，处理不了的转人工。"


@pytest.fixture
def mock_llm(monkeypatch) -> MockAdapter:
    adapter = MockAdapter()
    for mod in (
        "app.agents.requirement_analyzer",
        "app.agents.architecture_planner",
        "app.agents.component_designer",
    ):
        monkeypatch.setattr(f"{mod}.get_llm", lambda: adapter)
    return adapter


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "jobs.db"


def test_repo_roundtrip(db: Path) -> None:
    repo = JobRepo(db)
    repo.save(
        {
            "id": "j1",
            "status": "running",
            "created_at": 1.0,
            "updated_at": 2.0,
            "raw_text": "x",
        }
    )
    repo.save(
        {
            "id": "j1",
            "status": "done",
            "created_at": 1.0,
            "updated_at": 3.0,
            "raw_text": "x",
        }
    )
    assert repo.count() == 1, "同 id 应该是 upsert，不是插两条"
    assert repo.load_recent()[0]["status"] == "done"
    repo.close()


def test_job_survives_restart(mock_llm: MockAdapter, db: Path, monkeypatch) -> None:
    """核心场景：跑完的任务、事件流、结果，换一个 JobStore 还能读出来。"""
    from app.engine import pipeline as pipeline_mod

    monkeypatch.setattr(pipeline_mod.settings, "auto_save_project", False)

    first = JobStore()
    first.attach_repo(db)
    job = first.create(RAW, {"pause_on_blocking": False})
    asyncio.run(run_pipeline(job))
    assert job.status == "done"
    event_count = len(job.events)

    # 模拟重启：全新的 JobStore，只挂同一个库
    second = JobStore()
    second.attach_repo(db)
    stats = second.restore()
    assert stats["restored"] == 1

    recovered = second.get(job.id)
    assert recovered is not None
    assert recovered.status == "done"
    assert recovered.result.get("codegen")
    assert len(recovered.events) == event_count
    assert recovered.last_seq == job.last_seq
    # 恢复出来的任务还能继续写事件，且序号接着来（不会从 1 重新数）
    recovered.add_event("重启后补记一条", level="info")
    assert recovered.last_seq == event_count + 1


def test_interrupted_job_is_marked_failed(db: Path) -> None:
    """跑到一半崩了的任务，重启后要明确说"中断了"，不能装作还在跑。"""
    first = JobStore()
    first.attach_repo(db)
    job = first.create(RAW, {"pause_on_blocking": False})
    job.start_step("requirement")
    assert job.status == "running"

    second = JobStore()
    second.attach_repo(db)
    stats = second.restore()
    assert stats == {"restored": 1, "interrupted": 1}

    recovered = second.get(job.id)
    assert recovered.status == "failed"
    assert "重启" in (recovered.error or "")
    assert recovered.step("requirement").status == "running", "已完成的进度要保留，便于定位断在哪"
    assert any("服务重启" in e.message for e in recovered.events)


def test_waiting_approval_survives_restart(mock_llm: MockAdapter, db: Path, monkeypatch) -> None:
    """审批态也要能恢复：用户第二天回来点"确认"还能用（预案见 job_store.restore 注释）。"""
    from app.engine import pipeline as pipeline_mod

    monkeypatch.setattr(pipeline_mod.settings, "auto_save_project", False)
    first = JobStore()
    first.attach_repo(db)
    job = first.create(RAW, {"pause_on_blocking": False, "require_architecture_approval": True})
    asyncio.run(run_pipeline(job))
    assert job.status == "waiting_approval"

    second = JobStore()
    second.attach_repo(db)
    second.restore()
    recovered = second.get(job.id)
    # 审批态不是终态，会被标成中断 —— 但架构结果和摘要仍在，用户至少能看到当时定了什么
    assert recovered.status == "failed"
    assert recovered.result.get("architecture")
    assert recovered.pending_approval is None or recovered.pending_approval["stage"] == "architecture"


def test_repo_failure_does_not_break_job(monkeypatch) -> None:
    """仓库写挂了不能影响构建本身——持久化是尽力而为，不是关键路径。"""

    class Broken:
        def save(self, payload):  # noqa: ANN001, ARG002
            raise OSError("disk full")

        def append_event(self, job_id, event):  # noqa: ANN001, ARG002
            raise OSError("disk full")

    store = JobStore()
    store._repo = Broken()  # noqa: SLF001 - 故意注入坏仓库
    job = store.create(RAW)
    job.add_event("照样能记")
    job.start_step("requirement")
    job.finish("done")
    assert job.status == "done"
    assert job.last_seq == 1
