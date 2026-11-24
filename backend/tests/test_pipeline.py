"""流水线（异步任务）测试。

两个层面分别验证：
1. run_pipeline 协程本身：状态流转、事件流、澄清暂停、取消；
2. HTTP 接口：提交返回 202、轮询增量、澄清回答的状态约束、取消、404。

模型一律用 MockAdapter 或桩对象，不依赖网络。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.llm.mock import MockAdapter
from app.engine import pipeline as pipeline_mod
from app.engine.job_store import job_store
from app.engine.pipeline import run_pipeline
from app.main import app
from app.schemas.requirement import AnalyzeResult, ClarifyingQuestion, RequirementSpec

RAW = "做一个客服工单智能分流 Agent，判断工单类别并自动回复常见问题，处理不了的转人工。"


@pytest.fixture(autouse=True)
def _reset_store():
    job_store.clear()
    yield
    job_store.clear()


@pytest.fixture
def mock_llm(monkeypatch):
    """让三个 Agent 都用离线 Mock，整条链路可离线跑通。"""
    adapter = MockAdapter()
    for mod in (
        "app.agents.requirement_analyzer",
        "app.agents.architecture_planner",
        "app.agents.component_designer",
    ):
        monkeypatch.setattr(f"{mod}.get_llm", lambda: adapter)
    return adapter


def _run(job) -> None:
    asyncio.run(run_pipeline(job))


def test_pipeline_completes_with_three_results(mock_llm) -> None:
    # Mock 必然产出一条 blocking 问题，这里关掉暂停以验证完整三步链路
    job = job_store.create(RAW, {"pause_on_blocking": False})
    _run(job)

    assert job.status == "done"
    assert job.error is None
    assert set(job.result) == {
        "requirement",
        "architecture",
        "components",
        "codegen",
        "project_id",  # 自动保存为项目后回填，见 test_projects.py
    }
    assert [s.status for s in job.steps] == ["done", "done", "done", "done"]
    assert all(s.duration is not None for s in job.steps)


def test_pipeline_generates_runnable_project(mock_llm) -> None:
    job = job_store.create(RAW, {"pause_on_blocking": False})
    _run(job)

    codegen = job.result["codegen"]
    assert codegen["validation"]["passed"] is True
    assert codegen["stats"]["files"] >= 12
    assert any(f["path"] == "agent/router.py" for f in codegen["files"])


def test_pipeline_emits_progress_events(mock_llm) -> None:
    job = job_store.create(RAW, {"pause_on_blocking": False})
    _run(job)

    steps_seen = {e.step for e in job.events}
    assert {"requirement", "architecture", "components", "codegen"} <= steps_seen
    assert any("规则预判" in e.message for e in job.events)
    # seq 单调递增，前端靠它做增量拉取
    seqs = [e.seq for e in job.events]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)


def test_pipeline_pauses_on_blocking_question(monkeypatch) -> None:
    class StubAnalyzer:
        def analyze(self, raw_text: str, answers: dict[str, str] | None = None) -> AnalyzeResult:
            return AnalyzeResult(
                spec=RequirementSpec(
                    agent_name="工单分流",
                    goal="把工单分流并回复",
                    clarifying_questions=[
                        ClarifyingQuestion(
                            id="q_input",
                            question="工单系统用 REST 还是 Webhook？",
                            why="决定是否需要 MCP",
                            blocking=True,
                        )
                    ],
                ),
                provider="stub",
                model="stub",
                latency_ms=1,
                attempts=1,
                warnings=[],
            )

    monkeypatch.setattr(pipeline_mod, "RequirementAnalyzer", StubAnalyzer)
    job = job_store.create(RAW, {"pause_on_blocking": True})
    _run(job)

    assert job.status == "waiting_input"
    assert job.pending_questions[0]["id"] == "q_input"
    # 暂停时不应产出架构与组件
    assert "architecture" not in job.result
    assert job.step("architecture").status == "pending"


def test_pipeline_continues_when_pause_disabled(mock_llm, monkeypatch) -> None:
    class StubAnalyzer:
        def analyze(self, raw_text: str, answers: dict[str, str] | None = None) -> AnalyzeResult:
            return AnalyzeResult(
                spec=RequirementSpec(
                    agent_name="工单分流",
                    goal="把工单分流并回复",
                    clarifying_questions=[
                        ClarifyingQuestion(
                            id="q_input",
                            question="REST 还是 Webhook？",
                            why="决定是否需要 MCP",
                            blocking=True
                        )
                    ],
                ),
                provider="stub",
                model="stub",
                latency_ms=1,
                attempts=1,
                warnings=[],
            )

    monkeypatch.setattr(pipeline_mod, "RequirementAnalyzer", StubAnalyzer)
    job = job_store.create(RAW, {"pause_on_blocking": False})
    _run(job)

    assert job.status == "done"
    assert any("未澄清" in e.message and e.level == "warn" for e in job.events)


def test_pipeline_marks_failed_step_on_error(monkeypatch) -> None:
    class BoomAnalyzer:
        def analyze(self, raw_text: str, answers: dict[str, str] | None = None) -> Any:
            raise RuntimeError("模型网关超时")

    monkeypatch.setattr(pipeline_mod, "RequirementAnalyzer", BoomAnalyzer)
    job = job_store.create(RAW, {})
    _run(job)

    assert job.status == "failed"
    assert "模型网关超时" in (job.error or "")
    assert job.step("requirement").status == "failed"
    assert job.step("architecture").status == "pending"


def test_pipeline_cancel_stops_before_next_step(monkeypatch) -> None:
    class SlowAnalyzer:
        def analyze(self, raw_text: str, answers: dict[str, str] | None = None) -> Any:
            # 模拟"用户在第一步行进中点了取消"
            job_pending[0].cancel_requested = True
            raise pipeline_mod.JobCancelled

    job_pending: list[Any] = []
    monkeypatch.setattr(pipeline_mod, "RequirementAnalyzer", SlowAnalyzer)
    job = job_store.create(RAW, {})
    job_pending.append(job)
    _run(job)

    assert job.status in ("cancelled", "failed")
    assert job.step("architecture").status == "pending"


# ---------------- HTTP 层 ----------------

@pytest.fixture
def client(monkeypatch):
    """接口测试不真的跑后台任务，只验证接口契约。"""
    monkeypatch.setattr("app.api.routes.pipeline._spawn", lambda job_id: None)
    with TestClient(app) as c:
        yield c


def test_run_returns_202_and_job_id(client) -> None:
    resp = client.post("/api/pipeline/run", json={"raw_text": RAW})
    assert resp.status_code == 202
    body = resp.json()
    assert body["code"] == 0
    assert body["data"]["status"] == "queued"
    assert body["data"]["job_id"]


def test_run_rejects_too_short_text(client) -> None:
    resp = client.post("/api/pipeline/run", json={"raw_text": "hi"})
    assert resp.status_code == 422


def test_get_job_incremental_since(client, mock_llm) -> None:
    job_id = client.post("/api/pipeline/run", json={"raw_text": RAW}).json()["data"]["job_id"]
    job = job_store.get(job_id)
    assert job is not None
    job.add_event("第一条", level="info")
    job.add_event("第二条", level="info")

    first = client.get(f"/api/pipeline/{job_id}").json()["data"]
    # /run 自己会写一条"任务已创建"事件
    assert len(first["events"]) == 3
    assert first["last_seq"] == 3

    job.add_event("第三条", level="info")
    second = client.get(f"/api/pipeline/{job_id}?since={first['last_seq']}").json()["data"]
    assert [e["message"] for e in second["events"]] == ["第三条"]


def test_get_unknown_job_404(client) -> None:
    assert client.get("/api/pipeline/nope").status_code == 404


def test_answers_conflict_when_not_waiting(client) -> None:
    job_id = client.post("/api/pipeline/run", json={"raw_text": RAW}).json()["data"]["job_id"]
    resp = client.post(f"/api/pipeline/{job_id}/answers", json={"answers": {"q": "a"}})
    assert resp.status_code == 409


def test_answers_resumes_waiting_job(client) -> None:
    job_id = client.post("/api/pipeline/run", json={"raw_text": RAW}).json()["data"]["job_id"]
    job = job_store.get(job_id)
    assert job is not None
    job.status = "waiting_input"
    job.pending_questions = [{"id": "q_input", "question": "REST?"}]

    resp = client.post(
        f"/api/pipeline/{job_id}/answers", json={"answers": {"q_input": "REST"}}
    )
    assert resp.status_code == 202
    assert job.status == "queued"
    assert job.answers == {"q_input": "REST"}
    assert job.pending_questions == []
    assert job.result == {}


def test_cancel_then_cannot_cancel_again(client) -> None:
    job_id = client.post("/api/pipeline/run", json={"raw_text": RAW}).json()["data"]["job_id"]
    assert client.post(f"/api/pipeline/{job_id}/cancel").status_code == 200
    job = job_store.get(job_id)
    assert job is not None and job.cancel_requested
    job.status = "done"
    assert client.post(f"/api/pipeline/{job_id}/cancel").status_code == 409


def test_download_returns_json_attachment(client, mock_llm) -> None:
    job_id = client.post("/api/pipeline/run", json={"raw_text": RAW}).json()["data"]["job_id"]
    resp = client.get(f"/api/pipeline/{job_id}/download")
    assert resp.status_code == 200
    assert "attachment" in resp.headers["content-disposition"]
    assert resp.json()["job_id"] == job_id


def test_cancel_immediately_cancels_waiting_job(client) -> None:
    """暂停态没有协程在跑，取消必须立刻落状态，否则按钮点了没反应。"""
    job_id = client.post("/api/pipeline/run", json={"raw_text": RAW}).json()["data"]["job_id"]
    job = job_store.get(job_id)
    assert job is not None
    job.status = "waiting_input"
    job.pending_questions = [{"id": "q", "question": "?"}]

    assert client.post(f"/api/pipeline/{job_id}/cancel").status_code == 200
    assert job.status == "cancelled"
    assert job.pending_questions == []


def test_force_continue_disables_pause(client) -> None:
    job_id = client.post("/api/pipeline/run", json={"raw_text": RAW}).json()["data"]["job_id"]
    job = job_store.get(job_id)
    assert job is not None
    job.status = "waiting_input"

    client.post(
        f"/api/pipeline/{job_id}/answers",
        json={"answers": {}, "force_continue": True},
    )
    assert job.options["pause_on_blocking"] is False
    assert job.status == "queued"


# ---------------------------------------------------------------- 架构人工审批
def test_gate_stops_after_architecture(mock_llm, monkeypatch) -> None:
    """开了闸门就必须停在架构之后：组件和代码一行都不能先生成。"""
    monkeypatch.setattr(pipeline_mod.settings, "auto_save_project", False)
    job = job_store.create(RAW, {"pause_on_blocking": False, "require_architecture_approval": True})
    _run(job)

    assert job.status == "waiting_approval"
    assert "architecture" in job.result
    assert "components" not in job.result and "codegen" not in job.result
    assert job.pending_approval["stage"] == "architecture"
    summary = job.pending_approval["summary"]
    assert summary["architecture_type"]
    assert summary["agents"], "审批页至少要能看到节点"
    assert summary["reasoning_summary"], "审批页要能回答为什么这么拆"
    assert job.step("components").status == "pending"


def test_approve_resumes_without_replanning(mock_llm, monkeypatch) -> None:
    """确认后从组件设计续跑，且**不重跑**架构 —— 用户点头的方案不能被换掉。"""
    from app.engine.pipeline import resume_after_approval

    monkeypatch.setattr(pipeline_mod.settings, "auto_save_project", False)
    job = job_store.create(RAW, {"pause_on_blocking": False, "require_architecture_approval": True})
    _run(job)
    planned_before = job.result["architecture"]

    calls: list[str] = []
    original = pipeline_mod.ArchitecturePlanner.plan

    def spy(self, *a, **kw):  # noqa: ANN001, ARG001
        calls.append("plan")
        return original(self, *a, **kw)

    monkeypatch.setattr(pipeline_mod.ArchitecturePlanner, "plan", spy)

    job.approve(note="就按这个来")
    asyncio.run(resume_after_approval(job))

    assert calls == [], "确认后不该再调用架构规划"
    assert job.result["architecture"] == planned_before
    assert job.status in ("done", "failed")
    assert "components" in job.result
    assert job.approval and job.approval["note"] == "就按这个来"
    assert job.pending_approval is None


def test_approve_conflict_when_not_waiting(client) -> None:
    """不在待审批状态时确认要 409，而不是悄悄当成功。"""
    job = job_store.create(RAW, {"pause_on_blocking": False})
    resp = client.post(f"/api/pipeline/{job.id}/approve", json={"note": ""})
    assert resp.status_code == 409


def test_reject_marks_failed(client, mock_llm, monkeypatch) -> None:
    monkeypatch.setattr(pipeline_mod.settings, "auto_save_project", False)
    job = job_store.create(RAW, {"pause_on_blocking": False, "require_architecture_approval": True})
    _run(job)
    assert job.status == "waiting_approval"

    resp = client.post(f"/api/pipeline/{job.id}/reject", json={"note": "节点拆太细"})
    assert resp.status_code == 200
    assert job.status == "failed"
    assert "节点拆太细" in (job.error or "")
    assert job.pending_approval is None
