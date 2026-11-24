from fastapi import APIRouter, HTTPException, status

from app.agents.component_designer import design_components
from app.core.llm import LLMError
from app.engine.component_registry import describe_for_prompt
from app.schemas.common import ApiResponse
from app.schemas.components import ComponentPlanResult, DesignRequest

router = APIRouter(prefix="/components", tags=["components"])


@router.get(
    "/registry",
    summary="组件类型注册表",
    description="返回平台支持的组件类型及其适用场景，供前端展示与用户理解组件边界。",
)
def registry() -> ApiResponse[dict[str, str]]:
    return ApiResponse[dict[str, str]](
        message="registry loaded", data={"description": describe_for_prompt()}
    )


@router.post(
    "/design",
    response_model=ApiResponse[ComponentPlanResult],
    summary="组件设计",
    description=(
        "根据需求与架构方案，产出可生成、可测试的组件清单。"
        "validation.passed=false 表示清单存在无法生成或无法集成的错误。"
    ),
)
def design(payload: DesignRequest) -> ApiResponse[ComponentPlanResult]:
    try:
        result = design_components(payload)
    except LLMError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)
        ) from exc

    return ApiResponse[ComponentPlanResult](
        message="components designed" if result.validation.passed else "组件方案存在问题",
        data=result,
    )
