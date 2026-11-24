from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import Response

from app.agents.code_generator import generate_code, to_zip
from app.schemas.codegen import CodegenRequest, CodegenResult
from app.schemas.common import ApiResponse

router = APIRouter(prefix="/codegen", tags=["codegen"])


@router.post(
    "/generate",
    response_model=ApiResponse[CodegenResult],
    summary="生成项目脚手架",
    description=(
        "把需求 + 架构 + 组件清单渲染成可运行的多文件工程。"
        "**纯模板生成，不调用模型**，因此不消耗 token、响应在毫秒级。"
        "生成后会做 AST/YAML/覆盖度自检，validation.passed=false 表示存在跑不起来的硬问题。"
    ),
)
def generate(payload: CodegenRequest) -> ApiResponse[CodegenResult]:
    result = generate_code(payload)
    return ApiResponse[CodegenResult](
        message="项目已生成" if result.validation.passed else "生成结果存在硬问题",
        data=result,
    )


@router.post(
    "/zip",
    summary="下载生成的项目 zip",
    description="返回 application/zip，顶层目录为项目名。",
    response_class=Response,
)
def download_zip(payload: CodegenRequest) -> Response:
    result = generate_code(payload)
    if not result.files:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="生成结果为空"
        )
    return Response(
        content=to_zip(result),
        media_type="application/zip",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{result.project_name}.zip"'
            )
        },
    )
