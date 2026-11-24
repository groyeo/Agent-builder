"""构建流水线：需求分析 → 架构规划 → 组件设计 → 代码生成。

与之前三个独立接口的区别：
1. 整条链路作为一个**后台任务**执行，接口立即返回 job_id，前端轮询进度；
2. 每一步开始/结束/失败/重试都会写入事件流，前端能看到"这 60 秒在干什么"；
3. 需求分析发现 blocking 澄清问题时**主动暂停**（waiting_input），
   用户回答后再继续，而不是带着猜测往下走。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from app.agents.architecture_planner import ArchitecturePlanner
from app.agents.code_generator import generate_code
from app.agents.component_designer import ComponentDesigner
from app.agents.requirement_analyzer import RequirementAnalyzer
from app.core.config import settings
from app.core.llm import LLMError
from app.core.progress import bind_progress
from app.engine import project_store
from app.engine.job_store import Job
from app.schemas.architecture import PlanResult
from app.schemas.codegen import CodegenRequest
from app.schemas.requirement import AnalyzeResult


class JobCancelled(Exception):
    """用户主动取消。用于在步骤之间跳出流水线。"""


def _run_with_progress(job: Job, step_key: str, fn, *args, **kwargs):
    """在工作线程里执行同步 Agent，并把 emit() 接到该步骤的事件流上。"""

    def sink(message: str) -> None:
        job.add_event(message, step=step_key, level="progress")

    with bind_progress(sink):
        return fn(*args, **kwargs)


async def _step(job: Job, step_key: str, fn, *args, **kwargs) -> Any:
    """执行一个流水线步骤：标记开始 → 线程池跑同步逻辑 → 标记结束/失败。"""
    if job.cancel_requested:
        raise JobCancelled
    job.start_step(step_key)
    job.add_event(f"{job.step(step_key).label}开始", step=step_key, level="info")
    started = time.time()
    try:
        result = await asyncio.to_thread(_run_with_progress, job, step_key, fn, *args, **kwargs)
    except Exception as exc:  # noqa: BLE001 - 需要区分业务错误与未知错误
        message = str(exc) if isinstance(exc, LLMError) else f"{type(exc).__name__}: {exc}"
        job.fail_step(step_key, message)
        job.add_event(message, step=step_key, level="error")
        raise
    job.finish_step(step_key)
    attempts = getattr(result, "attempts", None)
    if isinstance(attempts, int) and attempts > 0:
        job.step(step_key).attempts = attempts
    job.add_event(
        f"{job.step(step_key).label}完成，耗时 {round(time.time() - started, 1)}s"
        + (f"，模型调用 {attempts} 次" if isinstance(attempts, int) and attempts > 1 else ""),
        step=step_key,
        level="info",
    )
    return result


def _save_project(job: Job, generated: Any) -> None:
    """把本次结果写成磁盘项目。已有同 job 的项目则跳过，避免重复占目录。"""
    try:
        existing = next(
            (m for m in project_store.list_projects() if m.get("job_id") == job.id),
            None,
        )
        if existing:
            job.result["project_id"] = existing["id"]
            return
        meta = project_store.create_project(
            name=generated.project_name,
            codegen=generated,
            requirement=job.result.get("requirement"),
            architecture=job.result.get("architecture"),
            components=job.result.get("components"),
            delivery=generated.delivery.model_dump() if generated.delivery else None,
            job_id=job.id,
            raw_text=job.raw_text,
        )
        job.result["project_id"] = meta["id"]
        job.add_event(
            f"已保存为项目「{meta['name']}」（{meta['id']}），可在项目库中管理",
            step="codegen",
            level="info",
        )
    except Exception as exc:  # noqa: BLE001 - 保存失败不该掩盖已经成功的构建
        job.add_event(
            f"项目保存失败（生成结果仍可下载）：{type(exc).__name__}: {exc}",
            step="codegen",
            level="warn",
        )


def _blocking_questions(spec: Any) -> list[dict[str, Any]]:    return [
        q.model_dump() if hasattr(q, "model_dump") else dict(q)
        for q in spec.clarifying_questions
        if getattr(q, "blocking", False)
    ]


async def run_pipeline(job: Job) -> None:
    """执行整条链路。失败/取消都在这里落状态，不向上抛。"""
    pause_on_blocking = bool(job.options.get("pause_on_blocking", True))
    try:
        # 第二轮（带澄清回答）会重跑需求分析，让回答参与结构化，而不是在旧结果上打补丁
        analyzer = RequirementAnalyzer()
        analyzed = await _step(
            job,
            "requirement",
            analyzer.analyze,
            job.raw_text,
            dict(job.answers) or None,
        )
        job.result["requirement"] = analyzed.model_dump()

        questions = _blocking_questions(analyzed.spec)
        if pause_on_blocking and questions and not job.answers:
            job.pending_questions = questions
            job.add_event(
                f"发现 {len(questions)} 个会影响架构选型的关键问题，等待用户确认",
                step="requirement",
                level="warn",
            )
            job.finish("waiting_input")
            return
        if questions:
            job.add_event(
                f"仍有 {len(questions)} 个关键问题未澄清，按当前信息继续（结论可信度会受影响）",
                step="requirement",
                level="warn",
            )

        planner = ArchitecturePlanner()
        planned = await _step(job, "architecture", planner.plan, analyzed.spec)
        job.result["architecture"] = planned.model_dump()

        # 人工审批闸门：架构是最省钱的纠错点。等组件设计和 1400 行代码都生成完
        # 再发现"拆分错了"，代价要大一个数量级。
        if _needs_approval(job):
            job.pending_approval = {
                "stage": "architecture",
                "summary": _architecture_summary(planned.spec),
            }
            job.add_event(
                "架构方案已生成，等您确认后再生成组件与代码（确认前不会有任何文件产出）",
                step="architecture",
                level="warn",
            )
            job.finish("waiting_approval")
            return

        await _build(job, analyzed, planned)

    except JobCancelled:
        job.finish("cancelled")
        job.add_event("任务已取消", level="warn")
    except LLMError as exc:
        job.finish("failed", str(exc))
    except Exception as exc:  # noqa: BLE001
        job.finish("failed", f"{type(exc).__name__}: {exc}")
        job.add_event(f"未预期错误：{type(exc).__name__}: {exc}", level="error")


def _needs_approval(job: Job) -> bool:
    """要不要停下来等人。已确认过的（job.approval）不再拦，避免二次确认。"""
    if job.approval:
        return False
    flag = job.options.get("require_architecture_approval")
    return bool(settings.require_architecture_approval if flag is None else flag)


def _architecture_summary(spec: Any) -> dict[str, Any]:
    """审批页要给人看的东西：几个节点、各自负责什么、怎么连、要哪些组件。"""
    return {
        "architecture_type": spec.architecture_type,
        "complexity_level": spec.complexity_level,
        "confidence": round(float(spec.confidence or 0), 2),
        "reasoning_summary": spec.reasoning_summary,
        "agents": [
            {
                "id": a.id,
                "name": a.name,
                "responsibility": a.responsibility,
                "tools": list(a.tools),
                "outputs": list(a.outputs),
            }
            for a in spec.agents
        ],
        "edges": [
            {"from": e.source, "to": e.target, "condition": e.condition}
            for e in spec.workflow_edges
        ],
        "required_components": [c.name for c in spec.required_components],
        "optional_components": [c.name for c in spec.optional_components],
        "risks": list(spec.risks),
    }


async def _build(job: Job, analyzed: Any, planned: Any) -> None:
    """架构之后的确定性部分：组件设计 → 代码生成 → 落盘成项目。"""
    if job.cancel_requested:
        raise JobCancelled

    designer = ComponentDesigner()
    designed = await _step(
        job, "components", designer.design, analyzed.spec, planned.spec
    )
    job.result["components"] = designed.model_dump()

    # 代码生成是确定性的，不调模型：既省钱，也保证同样输入产出同样文件
    codegen_payload = CodegenRequest(
        requirement=analyzed.spec,
        architecture=planned.spec,
        components=designed.plan,
    )
    generated = await _step(job, "codegen", generate_code, codegen_payload)
    job.result["codegen"] = generated.model_dump()
    job.add_event(
        f"生成 {generated.stats.files} 个文件 / {generated.stats.lines} 行"
        f"（模板生成，0 token）",
        step="codegen",
        level="info",
    )

    # 结果落盘成"项目"：任务活在内存里、重启就没，项目活在磁盘上。
    # 这一步失败不能让整条链路失败 —— 结果本来就在 job.result 里，用户仍可手动保存。
    if settings.auto_save_project:
        _save_project(job, generated)

    if generated.delivery:
        d = generated.delivery
        job.add_event(
            f"交付清单：共 {len(d.resources)} 项待准备，其中 {len(d.required_blocks)} 项必需"
            f"（见 DELIVERY.md，或前端「交付清单」页）",
            step="codegen",
            level="info",
        )

    if job.cancel_requested:
        raise JobCancelled
    job.pending_approval = None
    job.finish("done")
    job.add_event("全部完成", level="info")


async def resume_after_approval(job: Job) -> None:
    """用户确认架构后继续。

    刻意**不重跑**需求分析与架构：模型每次输出都会飘一点，重跑等于把用户刚
    点头的方案换成另一份 —— 那审批就白做了。这里直接复用 job.result 里的两份
    产物（所以任务持久化之后，重启也能接着走）。
    """
    try:
        analyzed = AnalyzeResult.model_validate(job.result["requirement"])
        planned = PlanResult.model_validate(job.result["architecture"])
    except (KeyError, ValueError) as exc:
        job.finish("failed", f"架构审批状态损坏，无法续跑：{exc}")
        job.add_event("缺少需求/架构产物，请重新提交构建", level="error")
        return

    # 只有 components / codegen 需要重来
    for step in job.steps:
        if step.key in ("components", "codegen"):
            step.status = "pending"
            step.error = None
    job.status = "running"
    job.finished_at = None
    job.add_event("架构已确认，继续生成组件与代码", level="info")
    try:
        await _build(job, analyzed, planned)
    except JobCancelled:
        job.finish("cancelled")
        job.add_event("任务已取消", level="warn")
    except LLMError as exc:
        job.finish("failed", str(exc))
    except Exception as exc:  # noqa: BLE001
        job.finish("failed", f"{type(exc).__name__}: {exc}")
        job.add_event(f"未预期错误：{type(exc).__name__}: {exc}", level="error")
