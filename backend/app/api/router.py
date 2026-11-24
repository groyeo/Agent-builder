from fastapi import APIRouter

from app.api.routes import (
    architecture,
    codegen,
    components,
    health,
    pipeline,
    preview,
    projects,
    requirements,
    sandbox as sandbox_routes,
    settings as settings_routes,
)

# 汇总所有路由。后续阶段在此注册 builds 等模块。
api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(settings_routes.router)
api_router.include_router(requirements.router)
api_router.include_router(architecture.router)
api_router.include_router(components.router)
api_router.include_router(codegen.router)
api_router.include_router(pipeline.router)
api_router.include_router(projects.router)
api_router.include_router(preview.router)
api_router.include_router(sandbox_routes.router)
