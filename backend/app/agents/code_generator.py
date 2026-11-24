"""代码生成入口：渲染 + 自检 + 打包。

这里刻意不提供"直接写入你指定目录"的能力：
路径来自网络请求，写盘是最容易出事的一步。产出通过下载 zip 交付，
由用户自己决定放哪里 —— 少一个误删别人目录的机会。
"""

from __future__ import annotations

import io
import zipfile

from app.engine.code_generator import generate as render_project
from app.schemas.codegen import CodegenRequest, CodegenResult
from app.validators.codegen_validator import validate


class CodeGenerator:
    def generate(self, payload: CodegenRequest) -> CodegenResult:
        result = render_project(
            payload.requirement, payload.architecture, payload.components
        )
        report = validate(result, payload.architecture, payload.components)
        result.validation = report

        # 校验发现的问题要同时进入 warnings，避免前端只看一个面板
        for issue in report.errors:
            result.warnings.append(f"[{issue.code}] {issue.message}")
        return result


def generate_code(payload: CodegenRequest) -> CodegenResult:
    return CodeGenerator().generate(payload)


def to_zip(result: CodegenResult) -> bytes:
    """把生成结果打成 zip，顶层目录为项目名。"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in result.files:
            zf.writestr(f"{result.project_name}/{f.path}", f.content)
    return buffer.getvalue()
