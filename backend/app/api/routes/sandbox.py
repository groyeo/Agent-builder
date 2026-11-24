"""执行隔离的状态与切换。

为什么要给前端一个接口：用户有权知道"跑测试"到底是关在容器里，还是就在自己
机器上裸跑。这个信息不能只写在文档里 —— 界面上一眼看不到，就等于没说。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.core.config import settings
from app.engine import sandbox
from app.schemas.common import ApiResponse

router = APIRouter(prefix="/sandbox", tags=["sandbox"])

DRIVERS = ("auto", "docker", "local")


def _state_file() -> Path:
    return Path(settings.data_dir) / "sandbox_settings.json"


def load() -> None:
    """启动时把上次的选择灌回配置，免得每次重启都回到 .env 默认值。"""
    path = _state_file()
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    driver = data.get("driver")
    if driver in DRIVERS:
        settings.sandbox_driver = driver


def _save(driver: str) -> None:
    path = _state_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"driver": driver}, ensure_ascii=False), encoding="utf-8")


class DriverPatch(BaseModel):
    driver: Literal["auto", "docker", "local"]


@router.get(
    "",
    response_model=ApiResponse[dict[str, Any]],
    summary="执行隔离状态",
    description="当前用的是容器还是本机子进程、为什么这么选、容器限额是多少。",
)
def get_status() -> ApiResponse[dict[str, Any]]:
    data = sandbox.status()
    return ApiResponse[dict[str, Any]](message=data["reason"], data=data)


@router.put(
    "",
    response_model=ApiResponse[dict[str, Any]],
    summary="切换执行隔离档位",
    description="auto=能容器就容器；docker=强制容器（不可用时报错）；local=强制本机子进程。",
)
def put_status(patch: DriverPatch) -> ApiResponse[dict[str, Any]]:
    if patch.driver == "docker":
        info = sandbox.probe(refresh=True)
        if not info["docker_server"]:
            raise HTTPException(
                status_code=409,
                detail="本机 docker 不可用："
                + (info.get("docker_error") or "未安装 docker")
                + "。请先启动 Docker Desktop，或选择 auto/local。",
            )
    settings.sandbox_driver = patch.driver
    _save(patch.driver)
    sandbox.reset_probe_cache()
    data = sandbox.status()
    return ApiResponse[dict[str, Any]](
        message=f"已切换为 {patch.driver}（当前：{data['driver']}）", data=data
    )


@router.post(
    "/warm",
    response_model=ApiResponse[dict[str, Any]],
    summary="预构建沙箱镜像",
    description="首次跑容器前需要拉取基础镜像并装依赖，可能要几分钟。",
)
def warm() -> ApiResponse[dict[str, Any]]:
    try:
        image = sandbox.ensure_image()
    except sandbox.SandboxError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return ApiResponse[dict[str, Any]](
        message=f"镜像就绪：{image}", data=sandbox.status()
    )
