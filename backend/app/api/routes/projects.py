"""项目管理接口。

一句话：把"任务结果"变成"能管的东西"。

- POST   /projects                从已完成任务落库（幂等：同一 job 只存一份）
- GET    /projects                项目列表（默认含归档，按更新时间倒序）
- GET    /projects/{id}           详情 + 文件清单
- PATCH  /projects/{id}           改名 / 改描述 / 归档 / 打标签
- DELETE /projects/{id}           彻底删除（删目录，不可恢复）
- GET    /projects/{id}/files     读文件正文
- PUT    /projects/{id}/files     写回文件正文（在线改代码的最小实现）
- GET    /projects/{id}/design/{stage}  读需求/架构/组件设计产物
- POST   /projects/{id}/refresh    用最新模板重新生成代码（0 token，保留 .env）
- POST   /projects/{id}/tests      真跑一遍 pytest，结果存 design/tests.json
- GET    /projects/{id}/tests      读上次测试结果（没跑过 404）
- POST   /projects/{id}/heal       Debug Agent 修测试失败（最多 2 轮，禁改 tests/）
- GET    /projects/{id}/history    版本快照列表（最多保留 10 个）
- GET    /projects/{id}/history/{v}/diff  版本与当前的差异
- POST   /projects/{id}/history/{v}/restore 回滚（只覆盖不回删，先自动备份）
- POST   /projects/{id}/snapshot   手动打版本
- GET    /projects/{id}/zip       打包下载（从磁盘打，不重新生成）
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Body, HTTPException, Query, status

from app.engine import debug_agent, project_store, test_runner
from app.engine.job_store import job_store
from app.engine.project_store import ProjectError
from app.schemas.codegen import CodegenResult
from app.schemas.common import ApiResponse
from app.schemas.project import (
    ProjectCreateRequest,
    ProjectDetail,
    ProjectFileContent,
    ProjectFileWriteRequest,
    ProjectSummary,
    ProjectUpdateRequest,
)

router = APIRouter(prefix="/projects", tags=["projects"])


def _fail(exc: ProjectError) -> HTTPException:
    return HTTPException(status_code=exc.code, detail=exc.message)


@router.post(
    "",
    response_model=ApiResponse[ProjectSummary],
    status_code=status.HTTP_201_CREATED,
    summary="把任务结果保存为项目",
    description=(
        "从已完成（status=done）的任务里取代码生成结果，写成磁盘上的项目。"
        "同一 job_id 重复调用会返回已存在的项目，不会产生副本。"
    ),
)
def create(payload: ProjectCreateRequest) -> ApiResponse[ProjectSummary]:
    job = job_store.get(payload.job_id)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="任务不存在（内存态任务，服务重启后丢失；请重新构建）",
        )

    existing = next(
        (m for m in project_store.list_projects() if m.get("job_id") == job.id), None
    )
    if existing:
        return ApiResponse[ProjectSummary](
            message="该任务已保存过项目，直接返回已有项目",
            data=ProjectSummary.model_validate(existing),
        )

    codegen = job.result.get("codegen")
    if not codegen:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"任务尚无代码生成结果（当前状态：{job.status}）",
        )

    try:
        meta = project_store.create_project(
            name=payload.name or CodegenResult.model_validate(codegen).project_name,
            codegen=codegen,
            requirement=job.result.get("requirement"),
            architecture=job.result.get("architecture"),
            components=job.result.get("components"),
            job_id=job.id,
            raw_text=job.raw_text,
            description=payload.description,
            tags=payload.tags,
        )
    except ProjectError as exc:
        raise _fail(exc) from exc

    # 让前端轮询结束时就能拿到 project_id，不用再查一次列表
    job.result["project_id"] = meta["id"]
    job.add_event(f"已保存为项目「{meta['name']}」（{meta['id']}）", level="info")
    return ApiResponse[ProjectSummary](
        message="项目已创建", data=ProjectSummary.model_validate(meta)
    )


@router.get(
    "",
    response_model=ApiResponse[list[ProjectSummary]],
    summary="项目列表",
)
def list_all(
    include_archived: bool = Query(default=True, description="是否包含已归档项目"),
) -> ApiResponse[list[ProjectSummary]]:
    items = project_store.list_projects(include_archived=include_archived)
    return ApiResponse[list[ProjectSummary]](
        message=f"{len(items)} 个项目",
        data=[ProjectSummary.model_validate(m) for m in items],
    )


@router.get(
    "/{project_id}",
    response_model=ApiResponse[ProjectDetail],
    summary="项目详情",
)
def detail(project_id: str) -> ApiResponse[ProjectDetail]:
    try:
        data = project_store.get_detail(project_id)
    except ProjectError as exc:
        raise _fail(exc) from exc
    return ApiResponse[ProjectDetail](
        message="ok", data=ProjectDetail.model_validate(data)
    )


@router.patch(
    "/{project_id}",
    response_model=ApiResponse[ProjectSummary],
    summary="更新项目信息",
    description="改名、改描述、归档/取消归档、打标签。只更新传入的字段。",
)
def update(project_id: str, payload: ProjectUpdateRequest) -> ApiResponse[ProjectSummary]:
    try:
        meta = project_store.update_meta(
            project_id,
            name=payload.name,
            description=payload.description,
            status=payload.status,
            tags=payload.tags,
        )
    except ProjectError as exc:
        raise _fail(exc) from exc
    return ApiResponse[ProjectSummary](message="已更新", data=ProjectSummary.model_validate(meta))


@router.delete(
    "/{project_id}",
    response_model=ApiResponse[dict[str, Any]],
    summary="彻底删除项目",
    description="从磁盘上删除项目目录及其全部文件，不可恢复（前端需二次确认）。",
)
def remove(project_id: str) -> ApiResponse[dict[str, Any]]:
    try:
        info = project_store.delete_project(project_id)
    except ProjectError as exc:
        raise _fail(exc) from exc
    return ApiResponse[dict[str, Any]](
        message=f"已彻底删除（{info['files_removed']} 个文件）", data=info
    )


@router.post(
    "/{project_id}/refresh",
    response_model=ApiResponse[dict[str, Any]],
    summary="用最新模板重新生成代码",
    description=(
        "拿项目里存的需求/架构/组件设计产物重跑一遍模板生成，把最新的模板修复"
        "带进老项目。**纯模板，0 token、毫秒级**。只覆盖同路径文件，不删除任何文件；"
        ".env、knowledge/、data/ 永远跳过。"
    ),
)
def refresh(project_id: str) -> ApiResponse[dict[str, Any]]:
    try:
        info = project_store.refresh_project(project_id)
    except ProjectError as exc:
        raise _fail(exc) from exc
    return ApiResponse[dict[str, Any]](
        message=f"已重新生成：写入 {info['files_written']} 个文件、"
        f"跳过 {info['files_kept']} 个（内容未变或属于你的数据）",
        data=info,
    )


@router.get(
    "/{project_id}/history",
    response_model=ApiResponse[dict[str, Any]],
    summary="版本历史",
    description="初始生成、模板重刷前、AI 自动修复前、回滚前都会留一份快照，最多保留 10 个。",
)
def history(project_id: str) -> ApiResponse[dict[str, Any]]:
    try:
        items = project_store.list_history(project_id)
    except ProjectError as exc:
        raise _fail(exc) from exc
    return ApiResponse[dict[str, Any]](
        message=f"{len(items)} 个版本" if items else "还没有快照",
        data={"versions": items, "max_keep": project_store.MAX_SNAPSHOTS},
    )


@router.get(
    "/{project_id}/history/{version}",
    response_model=ApiResponse[dict[str, Any]],
    summary="版本详情",
)
def version_detail(project_id: str, version: str) -> ApiResponse[dict[str, Any]]:
    try:
        data = project_store.read_version(project_id, version)
    except (ProjectError, OSError, ValueError) as exc:
        raise _fail(exc) from exc
    return ApiResponse[dict[str, Any]](message=version, data=data)


@router.get(
    "/{project_id}/history/{version}/diff",
    response_model=ApiResponse[dict[str, Any]],
    summary="版本与当前文件的差异",
    description="逐文件给 added / removed / modified，用来判断能不能直接回滚。",
)
def version_diff(project_id: str, version: str) -> ApiResponse[dict[str, Any]]:
    try:
        data = project_store.diff_version(project_id, version)
    except ProjectError as exc:
        raise _fail(exc) from exc
    msg = "与当前完全一致" if data["identical"] else (
        f"{data['modified']} 改 / {data['added']} 增 / {data['removed']} 缺"
    )
    return ApiResponse[dict[str, Any]](message=msg, data=data)


@router.post(
    "/{project_id}/history/{version}/restore",
    response_model=ApiResponse[dict[str, Any]],
    summary="回滚到某个版本",
    description=(
        "只覆盖/新增快照里的文件，不删除当前多出来的文件；"
        "`.env`、`knowledge/`、`data/` 不动；回滚前自动给当前状态拍一张快照。"
    ),
)
def version_restore(project_id: str, version: str) -> ApiResponse[dict[str, Any]]:
    try:
        data = project_store.restore_version(project_id, version)
    except ProjectError as exc:
        raise _fail(exc) from exc
    return ApiResponse[dict[str, Any]](
        message=f"已回滚到 {version}（回滚前状态存为 {data['backup_version']}）",
        data=data,
    )


@router.post(
    "/{project_id}/snapshot",
    response_model=ApiResponse[dict[str, Any]],
    summary="手动打一个版本",
    description="改代码前想留个退路时用。",
)
def make_snapshot(project_id: str, note: str = "") -> ApiResponse[dict[str, Any]]:
    try:
        data = project_store.snapshot(project_id, reason="手动", note=note)
    except ProjectError as exc:
        raise _fail(exc) from exc
    return ApiResponse[dict[str, Any]](
        message=f"已保存版本 {data['version']}", data=data
    )

@router.post(
    "/{project_id}/tests",
    response_model=ApiResponse[dict[str, Any]],
    summary="在项目里真跑一遍 pytest",
    description=(
        "在工作区里执行 `pytest -q`，返回逐条用例结果、通过/失败数、输出尾部，"
        "并存到 design/tests.json。**有用例失败不是接口错误**：照样 200，看 data.ok。"
        "当前没有容器隔离 —— 跑的是本平台渲染的模板代码，不是任意上传代码。"
    ),
)
def run_project_tests(
    project_id: str,
    timeout: int = Query(default=180, ge=10, le=900, description="超时秒数，超了强杀"),
) -> ApiResponse[dict[str, Any]]:
    try:
        data = test_runner.run_tests(project_id, timeout=timeout)
    except ProjectError as exc:
        raise _fail(exc) from exc
    if data["ok"]:
        msg = f"{data['passed']} 个用例全部通过（{data['duration_s']}s）"
    elif data["timed_out"]:
        msg = f"跑超时了（>{timeout}s），已强制结束"
    else:
        msg = f"{data['failed'] + data['error']} 个用例失败 / 共 {data['total']} 个"
    return ApiResponse[dict[str, Any]](message=msg, data=data)


@router.get(
    "/{project_id}/tests",
    response_model=ApiResponse[dict[str, Any]],
    summary="读取上一次测试结果",
    description="没跑过测试返回 404，前端据此区分「没跑」和「跑了没过」。",
)
def last_tests(project_id: str) -> ApiResponse[dict[str, Any]]:
    try:
        data = test_runner.read_last(project_id)
    except ProjectError as exc:
        raise _fail(exc) from exc
    return ApiResponse[dict[str, Any]](message="ok", data=data)


@router.post(
    "/{project_id}/heal",
    response_model=ApiResponse[dict[str, Any]],
    summary="让 Debug Agent 尝试自动修复测试失败",
    description=(
        "跑测试 → 失败就请模型提最小补丁 → 重跑，默认最多 2 次。"
        "**不许改 tests/ 和 .env**（改断言等于作弊）；越修越糟会整体回滚；"
        "最后还是红的就返回 needs_human=true，不粉饰。全程记录在 design/debug.json。"
    ),
)
def heal_project(
    project_id: str,
    max_attempts: int = Query(default=2, ge=1, le=4, description="最多修几轮"),
) -> ApiResponse[dict[str, Any]]:
    try:
        data = debug_agent.heal(project_id, max_attempts=max_attempts)
    except ProjectError as exc:
        raise _fail(exc) from exc
    if data["final"]["ok"]:
        msg = f"修好了：{data['note']}（改了 {len(data['changed_files'])} 个文件）"
    else:
        msg = data["note"]
    return ApiResponse[dict[str, Any]](message=msg, data=data)


@router.get(
    "/{project_id}/heal",
    response_model=ApiResponse[dict[str, Any]],
    summary="读取上一次自动修复过程",
    description="包含每轮诊断、改动文件、拒绝原因和最终结论。没试过返回 404。",
)
def last_heal(project_id: str) -> ApiResponse[dict[str, Any]]:
    try:
        data = debug_agent.read_last(project_id)
    except ProjectError as exc:
        raise _fail(exc) from exc
    return ApiResponse[dict[str, Any]](message="ok", data=data)


@router.get(
    "/{project_id}/files",
    response_model=ApiResponse[ProjectFileContent],
    summary="读取项目文件",
)
def get_file(
    project_id: str, path: str = Query(description="相对项目根目录的路径，如 agent/llm.py")
) -> ApiResponse[ProjectFileContent]:
    try:
        project_store.get_meta(project_id)
        data = project_store.read_file(project_id, path)
    except ProjectError as exc:
        raise _fail(exc) from exc
    return ApiResponse[ProjectFileContent](
        message="ok", data=ProjectFileContent.model_validate(data)
    )


@router.put(
    "/{project_id}/files",
    response_model=ApiResponse[ProjectFileContent],
    summary="保存项目文件",
)
def put_file(
    project_id: str,
    payload: ProjectFileWriteRequest = Body(...),
    path: str = Query(description="相对项目根目录的路径"),
) -> ApiResponse[ProjectFileContent]:
    try:
        data = project_store.write_file(project_id, path, payload.content)
    except ProjectError as exc:
        raise _fail(exc) from exc
    return ApiResponse[ProjectFileContent](message="已保存", data=ProjectFileContent.model_validate(data))


@router.get(
    "/{project_id}/design/{stage}",
    response_model=ApiResponse[dict[str, Any]],
    summary="读取设计产物",
    description=(
        "stage 取 requirement / architecture / components / delivery。"
        "delivery 返回交付清单：要准备什么、去哪注册、填到哪、怎么启动。"
    ),
)
def design(project_id: str, stage: str) -> ApiResponse[dict[str, Any]]:
    try:
        project_store.get_meta(project_id)
        data = project_store.read_design(project_id, stage)
    except ProjectError as exc:
        raise _fail(exc) from exc
    return ApiResponse[dict[str, Any]](message="ok", data=data)


@router.get(
    "/{project_id}/zip",
    summary="下载项目 zip",
    description="从磁盘上已有的文件打包，不会重新生成。顶层目录为项目 id。",
)
def download(project_id: str) -> Any:
    from fastapi.responses import Response

    try:
        content, name = project_store.zip_bytes(project_id)
    except ProjectError as exc:
        raise _fail(exc) from exc
    # 中文项目名不能直接放进 Content-Disposition（HTTP 头是 latin-1），
    # 用 RFC 5987 的 filename* 传 UTF-8，同时留一个 ASCII 兜底名。
    ascii_fallback = re.sub(r"[^A-Za-z0-9._-]", "_", project_id) or "project"
    quoted = quote(f"{name or project_id}.zip", safe="")
    disposition = (
        f'attachment; filename="{ascii_fallback}.zip"; '
        f"filename*=UTF-8''{quoted}"
    )
    return Response(
        content=content,
        media_type="application/zip",
        headers={"Content-Disposition": disposition},
    )
