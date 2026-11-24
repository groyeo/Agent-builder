"""构建任务存储与状态机。

边界（不假装它是生产级队列）：
- 运行中的任务活在**进程内存**里，执行靠 asyncio 协程；多 worker 部署时各 worker
  互不可见，所以仍假设 uvicorn 单进程运行；
- 但状态会**写穿到 SQLite**（见 `job_repo.py`）：重启后任务、事件流、待澄清问题
  都还在，不会出现"昨天提交成功、今天查 404"。挂上仓库后 `restore()` 会把上次
  没跑完的任务标成 failed 并说明原因，而不是悄悄消失。
- 上 PostgreSQL / Celery 时，只要换掉 repo 实现，本模块的领域模型与状态流转不动。

状态机：
  queued -> running -> done
                   -> waiting_approval -> running ... -> done
                   -> waiting_input -> running ... -> done
                   -> failed
                   -> cancelled
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal

from app.engine.job_repo import JobRepo

JobStatus = Literal[
    "queued",
    "running",
    "waiting_input",
    "waiting_approval",
    "done",
    "failed",
    "cancelled",
]
StepStatus = Literal["pending", "running", "done", "failed", "skipped"]
EventLevel = Literal["info", "progress", "warn", "error"]

STEP_DEFS: tuple[tuple[str, str], ...] = (
    ("requirement", "需求分析"),
    ("architecture", "架构规划"),
    ("components", "组件设计"),
    ("codegen", "代码生成"),
)


@dataclass
class JobEvent:
    seq: int
    ts: float
    step: str | None
    level: EventLevel
    message: str

    def dump(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "ts": self.ts,
            "step": self.step,
            "level": self.level,
            "message": self.message,
        }


@dataclass
class StepState:
    key: str
    label: str
    status: StepStatus = "pending"
    started_at: float | None = None
    ended_at: float | None = None
    attempts: int = 0
    error: str | None = None

    @property
    def duration(self) -> float | None:
        if self.started_at is None:
            return None
        end = self.ended_at if self.ended_at is not None else time.time()
        return round(end - self.started_at, 1)

    def dump(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "status": self.status,
            "duration": self.duration,
            "attempts": self.attempts,
            "error": self.error,
        }


@dataclass
class Job:
    id: str
    raw_text: str
    options: dict[str, Any] = field(default_factory=dict)
    status: JobStatus = "queued"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    steps: list[StepState] = field(
        default_factory=lambda: [StepState(key=k, label=v) for k, v in STEP_DEFS]
    )
    events: list[JobEvent] = field(default_factory=list)
    answers: dict[str, str] = field(default_factory=dict)
    pending_questions: list[dict[str, Any]] = field(default_factory=list)
    #: 待确认的架构方案摘要（status=waiting_approval 时前端据此渲染审批页）
    pending_approval: dict[str, Any] | None = None
    #: 用户确认记录 {stage, at, note}；存在即表示已过闸门
    approval: dict[str, Any] | None = None
    result: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    cancel_requested: bool = False
    _seq: int = 0
    # 用 RLock：写穿到仓库时要在持有锁的情况下再做一次快照（dump 也要这把锁）
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    #: 由 JobStore 注入的落库回调；None 表示纯内存（测试默认如此）
    _persist_hook: Callable[["Job"], None] | None = field(default=None, repr=False)

    def _notify(self) -> None:
        """状态变了就写穿。仓库写失败不能影响任务本身，最多丢持久化。"""
        hook = self._persist_hook
        if hook is None:
            return
        try:
            hook(self)
        except Exception:  # noqa: BLE001 - 持久化是尽力而为
            pass

    # ---- 状态写入（线程安全：Agent 在工作线程里也会调 emit） ----

    def add_event(
        self, message: str, *, step: str | None = None, level: EventLevel = "progress"
    ) -> None:
        with self._lock:
            self._seq += 1
            self.events.append(
                JobEvent(
                    seq=self._seq,
                    ts=time.time(),
                    step=step,
                    level=level,
                    message=message,
                )
            )
            self.updated_at = time.time()
        self._notify()

    def step(self, key: str) -> StepState:
        for s in self.steps:
            if s.key == key:
                return s
        raise KeyError(f"unknown step: {key}")

    def start_step(self, key: str) -> None:
        with self._lock:
            s = self.step(key)
            s.status = "running"
            s.started_at = time.time()
            s.ended_at = None
            s.error = None
            s.attempts += 1
            self.status = "running"
            self.updated_at = time.time()
        self._notify()

    def finish_step(self, key: str) -> None:
        with self._lock:
            s = self.step(key)
            s.status = "done"
            s.ended_at = time.time()
            self.updated_at = time.time()
        self._notify()

    def fail_step(self, key: str, error: str) -> None:
        with self._lock:
            s = self.step(key)
            s.status = "failed"
            s.error = error
            s.ended_at = time.time()
            self.updated_at = time.time()
        self._notify()

    def skip_step(self, key: str) -> None:
        with self._lock:
            self.step(key).status = "skipped"
        self._notify()

    def bump_attempt(self, key: str) -> None:
        with self._lock:
            self.step(key).attempts += 1
        self._notify()

    def approve(self, *, note: str = "") -> None:
        """记录确认动作。调用方负责把它交给 resume_after_approval。"""
        with self._lock:
            self.approval = {
                "stage": "architecture",
                "at": time.time(),
                "note": note,
            }
            self.pending_approval = None
            self.updated_at = time.time()
        self._notify()

    def finish(self, status: JobStatus, error: str | None = None) -> None:
        with self._lock:
            self.status = status
            self.error = error
            self.finished_at = time.time()
            self.updated_at = time.time()
        self._notify()

    # ---- 读取 ----

    def events_since(self, seq: int) -> list[JobEvent]:
        with self._lock:
            return [e for e in self.events if e.seq > seq]

    @property
    def last_seq(self) -> int:
        with self._lock:
            return self._seq

    def dump(self, *, since: int = 0, include_result: bool = True) -> dict[str, Any]:
        with self._lock:
            snapshot: dict[str, Any] = {
                "job_id": self.id,
                "status": self.status,
                "raw_text": self.raw_text,
                "created_at": self.created_at,
                "updated_at": self.updated_at,
                "finished_at": self.finished_at,
                "elapsed": round(
                    (self.finished_at or time.time()) - self.created_at, 1
                ),
                "steps": [s.dump() for s in self.steps],
                "events": [e.dump() for e in self.events if e.seq > since],
                "last_seq": self._seq,
                "pending_questions": list(self.pending_questions),
                "pending_approval": dict(self.pending_approval or {}) or None,
                "approval": dict(self.approval or {}) or None,
                "answers": dict(self.answers),
                "error": self.error,
            }
            if include_result:
                snapshot["result"] = dict(self.result)
            return snapshot


    # ---- 持久化 ----

    def to_persist(self) -> dict[str, Any]:
        """落库用的快照：保留内部时间戳，重启后恢复出来的进度条不会错位。

        不含 events（事件单独一张表追加），但带 _seq，恢复时能和事件表对齐。
        """
        with self._lock:
            return {
                "id": self.id,
                "raw_text": self.raw_text,
                "options": dict(self.options),
                "status": self.status,
                "created_at": self.created_at,
                "updated_at": self.updated_at,
                "finished_at": self.finished_at,
                "steps": [
                    {
                        "key": s.key,
                        "label": s.label,
                        "status": s.status,
                        "started_at": s.started_at,
                        "ended_at": s.ended_at,
                        "attempts": s.attempts,
                        "error": s.error,
                    }
                    for s in self.steps
                ],
                "answers": dict(self.answers),
                "pending_questions": list(self.pending_questions),
                "pending_approval": dict(self.pending_approval or {}) or None,
                "approval": dict(self.approval or {}) or None,
                "result": dict(self.result),
                "error": self.error,
                "cancel_requested": self.cancel_requested,
                "seq": self._seq,
            }


def _from_persist(payload: dict[str, Any]) -> Job:
    job = Job(
        id=payload["id"],
        raw_text=payload.get("raw_text", ""),
        options=payload.get("options") or {},
        status=payload.get("status", "failed"),
        created_at=payload.get("created_at", time.time()),
        updated_at=payload.get("updated_at", time.time()),
        finished_at=payload.get("finished_at"),
        answers=payload.get("answers") or {},
        pending_questions=payload.get("pending_questions") or [],
        pending_approval=payload.get("pending_approval"),
        approval=payload.get("approval"),
        result=payload.get("result") or {},
        error=payload.get("error"),
        cancel_requested=bool(payload.get("cancel_requested")),
    )
    steps = payload.get("steps") or []
    if steps:
        job.steps = [
            StepState(
                key=s["key"],
                label=s.get("label", s["key"]),
                status=s.get("status", "pending"),
                started_at=s.get("started_at"),
                ended_at=s.get("ended_at"),
                attempts=s.get("attempts", 0),
                error=s.get("error"),
            )
            for s in steps
        ]
    job._seq = int(payload.get("seq") or 0)
    return job


class JobStore:
    def __init__(self, *, max_jobs: int = 50) -> None:
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._lock = threading.Lock()
        self._max_jobs = max_jobs
        self._repo: JobRepo | None = None
        # 每个任务已经落库到第几个事件，避免重复写
        self._persisted_seq: dict[str, int] = {}

    # ---- 持久化接线 ----

    def attach_repo(self, path: str | Path) -> JobRepo:
        """挂上仓库并恢复最近的任务。返回 repo 便于测试里直接查。"""
        self._repo = JobRepo(path)
        return self._repo

    @property
    def repo(self) -> JobRepo | None:
        return self._repo

    def _persist(self, job: Job) -> None:
        repo = self._repo
        if repo is None:
            return
        repo.save(job.to_persist())
        last = self._persisted_seq.get(job.id, 0)
        with job._lock:  # noqa: SLF001 - 同模块内的内部协作
            new_events = [e.dump() for e in job.events if e.seq > last]
            if new_events:
                self._persisted_seq[job.id] = new_events[-1]["seq"]
        for event in new_events:
            repo.append_event(job.id, event)

    def restore(self) -> dict[str, int]:
        """把库里的任务装回内存。上次没跑完的一律标成 failed 并说明原因。

        为什么标 failed 而不是"继续跑"：跑一半的协程随进程一起没了，
        重新调度等于让模型从头再来一遍且可能重复落盘项目，代价和迷惑性都更大。
        """
        repo = self._repo
        if repo is None:
            return {"restored": 0, "interrupted": 0}
        restored = interrupted = 0
        for payload in repo.load_recent(limit=self._max_jobs):
            job = _from_persist(payload)
            events = repo.events_of(job.id)
            job.events = [
                JobEvent(
                    seq=e["seq"],
                    ts=e["ts"],
                    step=e["step"],
                    level=e["level"],
                    message=e["message"],
                )
                for e in events
            ]
            job._seq = max([job._seq] + [e["seq"] for e in events])  # noqa: SLF001
            job._persist_hook = self._persist  # noqa: SLF001
            self._persisted_seq[job.id] = job._seq
            if job.status not in ("done", "failed", "cancelled"):
                job.add_event(
                    "服务重启，任务已中断（已生成的步骤结果仍可查看，重跑请重新提交）",
                    level="error",
                )
                job.finish("failed", "服务重启导致任务中断")
                interrupted += 1
            with self._lock:
                self._jobs[job.id] = job
                self._order.append(job.id)
            restored += 1
        return {"restored": restored, "interrupted": interrupted}

    def create(self, raw_text: str, options: dict[str, Any] | None = None) -> Job:
        job = Job(id=uuid.uuid4().hex[:12], raw_text=raw_text, options=options or {})
        job._persist_hook = self._persist  # noqa: SLF001
        with self._lock:
            self._jobs[job.id] = job
            self._order.append(job.id)
            # 只保留最近的 N 个，避免长时间开发时内存无限增长
            while len(self._order) > self._max_jobs:
                evicted = self._order.pop(0)
                self._jobs.pop(evicted, None)
                self._persisted_seq.pop(evicted, None)
        job._notify()  # noqa: SLF001 - 走同一条"尽力而为"的落库路径
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def clear(self) -> None:
        """测试用：清空内存里的任务（不动仓库，仓库由测试自己管）。"""
        with self._lock:
            self._jobs.clear()
            self._order.clear()
            self._persisted_seq.clear()


job_store = JobStore()
