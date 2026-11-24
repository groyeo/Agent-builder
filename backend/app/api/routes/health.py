from fastapi import APIRouter

from app.core.config import settings
from app.schemas.common import ApiResponse, HealthData

router = APIRouter(tags=["system"])


@router.get(
    "/health",
    response_model=ApiResponse[HealthData],
    summary="健康检查",
    description="用于确认后端服务存活，并为前端/部署探针提供统一探活接口。",
)
def health_check() -> ApiResponse[HealthData]:
    return ApiResponse[HealthData](
        message="service is healthy",
        data=HealthData(
            status="ok",
            project=settings.app_name,
            version=settings.app_version,
            stage=settings.stage,
        ),
    )
