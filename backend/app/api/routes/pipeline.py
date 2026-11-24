"""构建流水线接口。

设计要点：
- POST /run 立即返回 job_id（HTTP 202），不在请求线程里等 90 秒；
- GET /{job_id}?since=N 返回增量事件，前端轮询即可实现"流式"观感，
  且刷新页面后仍能接上（不依赖 WebSocket 连接存活）；
- 澄清环节是**一等公民**：status=waiting_input 时带 pending_questions，
  用户回答后走 /answers 续跑；
- 架构审批同理：status=waiting_approval 时带 pending_approval，走 /approve 从
  组件设计续跑（不重跑需求与架构），或 /reject 结束本次构建。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, HTTPException, Query, status

from app.engine.job_store import job_store
from app.engine.pipeline import resume_after_approval, run_pipeline
from app.schemas.common import ApiResponse
from app.schemas.pipeline import (
    AnswerRequest,
    ApproveRequest,
    JobStatusData,
    RunPipelineRequest,
    RunPipelineResult,
)

router = APIRouter(prefix="/pipeline", tags=["pipeline"])

# 持有任务引用，防止 asyncio 把未完成的 Task 当垃圾回收掉
_tasks: dict[str, asyncio.Task[None]] = {}


def _spawn(job_id: str, *, resume: bool = False) -> None:
    job = job_store.get(job_id)
    if job is None:  # pragma: no cover - 调用方已确认存在
        return
    coro = resume_after_approval(job) if resume else run_pipeline(job)
    task = asyncio.create_task(coro)
    _tasks[job_id] = task
    task.add_done_callback(lambda _t: _tasks.pop(job_id, None))


@router.post(
    "/run",
    response_model=ApiResponse[RunPipelineResult],
    status_code=status.HTTP_202_ACCEPTED,
    summary="提交构建任务",
    description=(
        "提交后立即返回 job_id，流水线在后台执行。"
        "用 GET /api/pipeline/{job_id} 轮询进度与结果。"
    ),
)
async def run(payload: RunPipelineRequest) -> ApiResponse[RunPipelineResult]:
    raw = payload.raw_text.strip()
    if len(raw) < 5:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="需求描述太短（至少 5 个字）",
        )

    job = job_store.create(
        raw,
        {
            "pause_on_blocking": payload.pause_on_blocking,
            "answers": payload.answers,
            "require_architecture_approval": payload.require_architecture_approval,
        },
    )
    job.answers = dict(payload.answers)
    job.add_event("任务已创建，等待执行", level="info")
    _spawn(job.id)

    return ApiResponse[RunPipelineResult](
        message="job accepted",
        data=RunPipelineResult(
            job_id=job.id,
            status=job.status,
            poll_url=f"/api/pipeline/{job.id}",
        ),
    )


@router.get(
    "/{job_id}",
    response_model=ApiResponse[JobStatusData],
    summary="查询任务进度",
    description=(
        "since=上次返回的 last_seq 时只返回新增事件，用于增量轮询。"
        "status=done 时 result 内含 requirement / architecture / components 三段结果。"
    ),
)
def get_job(
    job_id: str,
    since: int = Query(default=0, ge=0, description="只返回 seq 大于该值的事件"),
    include_result: bool = Query(default=True, description="是否返回已完成的结果体"),
) -> ApiResponse[JobStatusData]:
    job = job_store.get(job_id)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="任务不存在（任务已落库，但可能被清理或从未创建）",
        )
    payload = job.dump(since=since, include_result=include_result)
    return ApiResponse[JobStatusData](
        message=job.status, data=JobStatusData.model_validate(payload)
    )


@router.post(
    "/{job_id}/answers",
    response_model=ApiResponse[RunPipelineResult],
    status_code=status.HTTP_202_ACCEPTED,
    summary="提交澄清回答并继续",
)
async def answer(
    job_id: str, payload: AnswerRequest
) -> ApiResponse[RunPipelineResult]:
    job = job_store.get(job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="任务不存在")
    if job.status != "waiting_input":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"当前任务状态为 {job.status}，只有 waiting_input 可以提交澄清回答",
        )

    job.answers.update(payload.answers)
    if payload.force_continue:
        job.options["pause_on_blocking"] = False
    job.pending_questions = []
    # 重跑需求分析（回答会被写进 Prompt），之前的步骤状态重置
    for step in job.steps:
        step.status = "pending"
        step.error = None
    job.result.clear()
    job.finished_at = None
    job.status = "queued"
    job.add_event(
        f"收到 {len(payload.answers)} 条澄清回答，重新执行需求分析",
        level="info",
    )
    _spawn(job.id)

    return ApiResponse[RunPipelineResult](
        message="answers accepted",
        data=RunPipelineResult(
            job_id=job.id, status=job.status, poll_url=f"/api/pipeline/{job.id}"
        ),
    )


@router.post(
    "/{job_id}/approve",
    response_model=ApiResponse[RunPipelineResult],
    status_code=status.HTTP_202_ACCEPTED,
    summary="确认架构方案并继续",
    description=(
        "仅在 status=waiting_approval 时可用。确认后从组件设计继续，"
        "**不会重跑**需求分析与架构 —— 否则用户刚确认的方案会被模型换成另一份。"
    ),
)
async def approve_architecture(
    job_id: str, payload: ApproveRequest
) -> ApiResponse[RunPipelineResult]:
    job = job_store.get(job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="任务不存在")
    if job.status != "waiting_approval":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"当前任务状态为 {job.status}，只有 waiting_approval 可以确认架构",
        )

    job.approve(note=payload.note)
    job.add_event(
        "架构已确认" + (f"（{payload.note}）" if payload.note else "") + "，继续生成",
        step="architecture",
        level="info",
    )
    _spawn(job.id, resume=True)
    return ApiResponse[RunPipelineResult](
        message="approved",
        data=RunPipelineResult(
            job_id=job.id, status=job.status, poll_url=f"/api/pipeline/{job.id}"
        ),
    )


@router.post(
    "/{job_id}/reject",
    response_model=ApiResponse[RunPipelineResult],
    summary="驳回架构方案",
    description="任务标记为 failed，note 作为原因。想重来请重新提交需求（带着更明确的约束）。",
)
def reject_architecture(job_id: str, payload: ApproveRequest) -> ApiResponse[RunPipelineResult]:
    job = job_store.get(job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="任务不存在")
    if job.status != "waiting_approval":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"当前任务状态为 {job.status}，只有 waiting_approval 可以驳回架构",
        )
    note = payload.note.strip() or "未说明原因"
    job.pending_approval = None
    job.finish("failed", f"架构被驳回：{note}")
    job.add_event(f"架构被驳回：{note}", step="architecture", level="warn")
    return ApiResponse[RunPipelineResult](
        message="rejected",
        data=RunPipelineResult(
            job_id=job.id, status=job.status, poll_url=f"/api/pipeline/{job.id}"
        ),
    )


@router.post(
    "/{job_id}/cancel",
    response_model=ApiResponse[RunPipelineResult],
    summary="取消任务",
    description=(
        "取消标记在步骤之间生效：正在进行的模型调用无法中断，"
        "会在该步骤结束后停止后续步骤。"
    ),
)
def cancel(job_id: str) -> ApiResponse[RunPipelineResult]:
    job = job_store.get(job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="任务不存在")
    if job.status in ("done", "failed", "cancelled"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"任务已结束（{job.status}），无法取消",
        )
    job.cancel_requested = True
    if job.status in ("waiting_input", "waiting_approval"):
        # 暂停态没有协程在跑，没人会消费 cancel_requested，必须立刻落状态
        job.pending_questions = []
        job.pending_approval = None
        job.finish("cancelled")
        job.add_event("任务在等待中（澄清/审批）被取消", level="warn")
    else:
        job.add_event("收到取消请求，将在当前步骤结束后停止", level="warn")
    return ApiResponse[RunPipelineResult](
        message="cancel requested",
        data=RunPipelineResult(
            job_id=job.id, status=job.status, poll_url=f"/api/pipeline/{job.id}"
        ),
    )


@router.get(
    "/{job_id}/zip",
    summary="下载生成的工程 zip",
    description="从已完成任务的 codegen 结果打包，无需重新生成。",
)
def download_zip(job_id: str) -> Any:
    from fastapi.responses import Response

    from app.agents.code_generator import to_zip
    from app.schemas.codegen import CodegenResult

    job = job_store.get(job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="任务不存在")
    payload = job.result.get("codegen")
    if not payload:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"任务尚无代码生成结果（当前状态：{job.status}）",
        )
    result = CodegenResult.model_validate(payload)
    return Response(
        content=to_zip(result),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{result.project_name}.zip"'
        },
    )


@router.get(
    "/{job_id}/download",
    summary="下载完整结果 JSON",
    description="把三段结果与事件流打包成一个 JSON 文件，便于留档或在外部工具里复核。",
)
def download(job_id: str) -> Any:
    from fastapi.responses import Response

    job = job_store.get(job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="任务不存在")
    body = json.dumps(job.dump(since=0), ensure_ascii=False, indent=2)
    return Response(
        content=body,
        media_type="application/json; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="agent-builder-{job.id}.json"'
        },
    )
