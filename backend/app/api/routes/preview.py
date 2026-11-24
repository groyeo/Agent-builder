"""项目预览接口：把生成的目标 Agent 起成本机网页。

为什么单独一个模块而不是塞进 projects.py：
预览是有副作用的（起进程、占端口、退出要清理），和"读项目元数据"不是一类事。
放一起会让 projects.py 里混进一个生命周期完全不同的东西。

- POST   /projects/{id}/preview   启动（已在跑则复用），返回网页地址
- GET    /projects/{id}/preview   只查状态
- DELETE /projects/{id}/preview   停止
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from app.engine import preview
from app.engine.project_store import ProjectError
from app.schemas.common import ApiResponse

router = APIRouter(prefix="/projects", tags=["preview"])


@router.post(
    "/{project_id}/preview",
    response_model=ApiResponse[dict[str, Any]],
    summary="启动项目网页",
    description=(
        "在项目目录里起 `python server.py`（只监听 127.0.0.1），返回可直接打开的地址。"
        "模型配置复用平台已填的 OPENAI_*，目标 Agent 不必再单独配 Key。"
        "已经在跑时直接返回原地址，不会重复起进程。"
    ),
)
def start(project_id: str) -> ApiResponse[dict[str, Any]]:
    try:
        data = preview.start(project_id)
    except ProjectError as exc:
        raise HTTPException(status_code=exc.code, detail=exc.message) from exc
    msg = "网页已在运行，直接打开即可" if data.get("reused") else "网页已启动"
    return ApiResponse[dict[str, Any]](message=f"{msg}：{data['url']}", data=data)


@router.get(
    "/{project_id}/preview",
    response_model=ApiResponse[dict[str, Any]],
    summary="查看项目网页状态",
)
def read(project_id: str) -> ApiResponse[dict[str, Any]]:
    try:
        data = preview.status(project_id)
    except ProjectError as exc:
        raise HTTPException(status_code=exc.code, detail=exc.message) from exc
    return ApiResponse[dict[str, Any]](
        message="运行中" if data["running"] else "未运行", data=data
    )


@router.delete(
    "/{project_id}/preview",
    response_model=ApiResponse[dict[str, Any]],
    summary="停止项目网页",
)
def stop(project_id: str) -> ApiResponse[dict[str, Any]]:
    data = preview.stop(project_id)
    return ApiResponse[dict[str, Any]](
        message="已停止" if data["stopped"] else "本来就没在运行", data=data
    )
