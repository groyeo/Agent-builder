import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.router import api_router
from app.core.config import settings
from app.api.routes import sandbox as sandbox_routes
from pathlib import Path

from app.engine import llm_config, preview, sandbox
from app.engine.job_store import job_store

logger = logging.getLogger("agent_builder")


@asynccontextmanager
async def lifespan(_: FastAPI):
    # 把上次选的思考强度灌回内存配置，避免每次重启都回到 .env 的默认档
    llm_config.load()
    sandbox_routes.load()
    # 探测一次是为了把"能不能用容器"尽早打出来：等用户点"运行测试"才发现
    # docker 没起，体验很差。探测本身很轻（一次 docker version）。
    # 任务与事件落库：重启后还能查到昨天那次构建的过程，而不是 404
    db_path = settings.jobs_db or str(Path(settings.data_dir) / "jobs.db")
    job_store.attach_repo(db_path)
    stats = job_store.restore()
    if stats["restored"]:
        logger.info(
            "已恢复 %s 个历史任务（其中 %s 个因重启中断被标记为失败）",
            stats["restored"],
            stats["interrupted"],
        )

    info = sandbox.probe()
    logger.info(
        "执行隔离：%s（docker=%s）",
        "容器" if info.get("docker_server") else "本机子进程",
        info.get("docker_server") or info.get("docker_error") or "未安装",
    )
    yield

    # 关服务时把预览子进程一起收掉：它们是本机网页，留着只会占端口
    preview.stop_all()


app = FastAPI(
    title=settings.app_name,
    description=settings.app_description,
    version=settings.app_version,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(Exception)
async def unhandled_error(request: Request, exc: Exception) -> JSONResponse:
    """任何未预期异常都回 JSON，而不是 Starlette 默认的纯文本 "Internal Server Error"。

    前端统一按 {code, message, data} 解析，回纯文本只会得到
    "响应不是合法 JSON"，真正的原因（哪个文件、哪一步）就丢了。
    """
    logger.exception("未处理异常：%s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={
            "code": 500,
            "message": f"服务器内部错误：{type(exc).__name__}: {exc}",
            "data": None,
        },
    )

# 全部业务接口挂在 /api 下；/health 额外在根路径暴露一份，方便部署探针直连。
app.include_router(api_router, prefix="/api")
app.include_router(api_router, include_in_schema=False)


@app.get("/", tags=["system"], summary="服务入口")
def home() -> dict[str, str]:
    return {"message": "Welcome to Agent Builder!"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug,
    )
