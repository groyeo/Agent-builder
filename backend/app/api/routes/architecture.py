from fastapi import APIRouter, HTTPException, status

from app.agents.architecture_planner import plan_architecture
from app.core.llm import LLMError
from app.schemas.architecture import PlanRequest, PlanResult
from app.schemas.common import ApiResponse

router = APIRouter(prefix="/architecture", tags=["architecture"])


@router.post(
    "/plan",
    response_model=ApiResponse[PlanResult],
    summary="架构规划",
    description=(
        "输入结构化需求，输出架构方案 + 规则预判依据 + 图结构校验结果。"
        "validation.passed=false 时说明方案结构不成立，应修正或人工介入。"
    ),
)
def plan(payload: PlanRequest) -> ApiResponse[PlanResult]:
    try:
        result = plan_architecture(payload.requirement)
    except LLMError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)
        ) from exc

    passed = result.validation.passed
    return ApiResponse[PlanResult](
        message="architecture planned" if passed else "架构方案存在结构问题",
        data=result,
    )
