from fastapi import APIRouter, HTTPException, status

from app.agents.requirement_analyzer import analyze_requirement
from app.core.llm import LLMError
from app.schemas.common import ApiResponse
from app.schemas.requirement import AnalyzeRequest, AnalyzeResult

router = APIRouter(prefix="/requirements", tags=["requirements"])


@router.post(
    "/analyze",
    response_model=ApiResponse[AnalyzeResult],
    summary="需求分析",
    description=(
        "把自然语言需求转换为结构化 RequirementSpec。"
        "若返回的 clarifying_questions 中存在 blocking=true，应先向用户澄清再进入架构设计。"
    ),
)
def analyze(payload: AnalyzeRequest) -> ApiResponse[AnalyzeResult]:
    try:
        result = analyze_requirement(payload.raw_text, payload.answers)
    except LLMError as exc:
        # 模型侧失败属于上游依赖错误，返回 502 让前端可以明确提示"模型暂不可用"
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)
        ) from exc

    return ApiResponse[AnalyzeResult](
        message="requirement analyzed",
        data=result,
    )
