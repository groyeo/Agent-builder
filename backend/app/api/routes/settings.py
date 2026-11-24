"""模型运行时设置接口。

- GET /settings/llm   当前 provider / 模型 / 思考强度 / 会发出去的请求字段
- PUT /settings/llm   改思考强度（off / low / medium / high）、temperature

为什么单独开一个接口而不是只读 .env：
思考强度是"这一次想快还是想深"的临时决定，改环境变量要重启服务，
体验上是不可接受的。设置落 data/llm_settings.json，下次任务自动用新档位。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.engine import llm_config
from app.schemas.common import ApiResponse
from app.schemas.settings import LLMSettings, LLMSettingsUpdate

router = APIRouter(prefix="/settings", tags=["settings"])


@router.get(
    "/llm",
    response_model=ApiResponse[LLMSettings],
    summary="读取模型设置",
)
def get_llm_settings() -> ApiResponse[LLMSettings]:
    return ApiResponse[LLMSettings](
        message="ok", data=LLMSettings.model_validate(llm_config.current())
    )


@router.put(
    "/llm",
    response_model=ApiResponse[LLMSettings],
    summary="修改模型设置",
    description=(
        "只传要改的字段。思考强度取 off/low/medium/high，"
        "off=不思考最省，high=深度思考最贵。改动在下次构建生效。"
    ),
)
def update_llm_settings(payload: LLMSettingsUpdate) -> ApiResponse[LLMSettings]:
    try:
        data = llm_config.update(
            thinking_level=payload.thinking, temperature=payload.temperature
        )
    except ValueError as exc:
        # 直接用 422 字面量：starlette 的 HTTP_422_UNPROCESSABLE_ENTITY 已弃用
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return ApiResponse[LLMSettings](
        message=f"已切换为「{data['thinking_label']}」思考，下次构建生效",
        data=LLMSettings.model_validate(data),
    )
