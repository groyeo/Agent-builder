from typing import Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class ApiResponse(BaseModel, Generic[T]):
    """统一响应格式（方案第 9 章要求）。

    code=0 表示成功；非 0 表示业务错误。HTTP 状态码仍表达传输层语义。
    """

    code: int = Field(default=0, description="0=成功，非 0=业务错误码")
    message: str = Field(default="ok", description="人类可读的结果说明")
    data: T | None = Field(default=None, description="业务数据")


class HealthData(BaseModel):
    status: str = Field(description="ok / degraded / error")
    project: str
    version: str
    stage: str
