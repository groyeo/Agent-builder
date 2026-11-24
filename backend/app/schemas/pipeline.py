from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

JobStatusT = Literal[
    "queued",
    "running",
    "waiting_input",
    "waiting_approval",
    "done",
    "failed",
    "cancelled",
]


class RunPipelineRequest(BaseModel):
    raw_text: str = Field(description="自然语言需求描述")
    pause_on_blocking: bool = Field(
        default=True,
        description=(
            "需求分析发现 blocking 澄清问题时是否暂停等待用户回答。"
            "false 表示带着假设直接跑完（结果可信度会下降）。"
        ),
    )
    answers: dict[str, str] = Field(
        default_factory=dict,
        description="可选的澄清回答，key 为 clarifying_questions[].id",
    )
    require_architecture_approval: bool | None = Field(
        default=None,
        description=(
            "架构规划完成后是否暂停等人工确认（true 停下来，false 一路跑完）。"
            "不传则用服务端默认值 settings.require_architecture_approval。"
        ),
    )


class RunPipelineResult(BaseModel):
    job_id: str
    status: JobStatusT
    poll_url: str


class StepStateData(BaseModel):
    key: str
    label: str
    status: Literal["pending", "running", "done", "failed", "skipped"]
    duration: float | None = None
    attempts: int = 0
    error: str | None = None


class JobEventData(BaseModel):
    seq: int
    ts: float
    step: str | None = None
    level: Literal["info", "progress", "warn", "error"] = "progress"
    message: str


class JobStatusData(BaseModel):
    job_id: str
    status: JobStatusT
    raw_text: str
    created_at: float
    updated_at: float
    finished_at: float | None = None
    elapsed: float
    steps: list[StepStateData] = Field(default_factory=list)
    events: list[JobEventData] = Field(default_factory=list)
    last_seq: int = 0
    pending_questions: list[dict[str, Any]] = Field(default_factory=list)
    pending_approval: dict[str, Any] | None = Field(
        default=None,
        description="status=waiting_approval 时的架构摘要（节点/边/复杂度/技术选型）",
    )
    approval: dict[str, Any] | None = Field(
        default=None, description="人工确认记录 {stage, at, note}"
    )
    answers: dict[str, str] = Field(default_factory=dict)
    error: str | None = None
    result: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "status=done 时包含 requirement / architecture / components / codegen；"
            "自动保存成功后还会有 project_id"
        ),
    )


class AnswerRequest(BaseModel):
    answers: dict[str, str] = Field(description="key 为 clarifying_questions[].id")
    force_continue: bool = Field(
        default=False,
        description="为 true 时不再因剩余 blocking 问题暂停，按当前信息一路跑完",
    )


class ApproveRequest(BaseModel):
    note: str = Field(default="", max_length=200, description="可选的确认/驳回说明")
