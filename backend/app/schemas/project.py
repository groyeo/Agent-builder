"""项目（Project）相关的数据结构。

与"任务（Job）"的区别，是这个模块存在的唯一理由：
- Job 是**一次构建过程**，活在内存里，跑完就只是本次会话的临时产物；
- Project 是**一份可管理的资产**，落在磁盘上，有名字、有状态、能改能删能下载，
  服务重启、换浏览器、过一周回来都还在。

因此 Project 不复制 Job 的状态机，只保存"结果 + 管理元信息"。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

ProjectStatus = Literal["active", "archived"]


class ProjectFileMeta(BaseModel):
    """文件清单条目。不带 content，列表接口才不会被正文撑爆。"""

    path: str
    kind: str = "text"
    lines: int = 0
    bytes: int = 0


class ProjectSummary(BaseModel):
    id: str = Field(description="项目目录名，也是接口里的 project_id")
    name: str
    description: str = ""
    status: ProjectStatus = "active"
    tags: list[str] = Field(default_factory=list)

    job_id: str | None = Field(default=None, description="产出该项目的构建任务")
    raw_text: str = Field(default="", description="当初的需求原文，便于回溯")

    agent_name: str | None = None
    architecture_type: str | None = None
    complexity_level: str | None = None

    created_at: float
    updated_at: float

    files: int = 0
    lines: int = 0
    tests: int = 0
    validation_passed: bool = True

    # 最近一次真跑测试的结论（没跑过就是 None，前端据此区分"没跑"和"挂了"）
    tests_ok: bool | None = Field(default=None, description="上次 pytest 是否全绿")
    tests_total: int | None = Field(default=None, description="上次跑了多少个用例")
    tests_failed: int | None = Field(default=None, description="上次失败/报错的用例数")
    tests_ran_at: float | None = Field(default=None, description="上次跑测试的时间戳")


class ProjectDetail(ProjectSummary):
    """详情页：多一棵目录树和文件清单，以及设计阶段的产物摘要。"""

    tree: str = ""
    file_list: list[ProjectFileMeta] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    has_requirement: bool = False
    has_architecture: bool = False
    has_components: bool = False
    has_delivery: bool = False
    has_tests: bool = False


class ProjectFileContent(BaseModel):
    path: str
    content: str
    kind: str = "text"
    lines: int = 0
    editable: bool = True


class ProjectCreateRequest(BaseModel):
    job_id: str = Field(description="从哪个已完成任务落库")
    name: str | None = Field(default=None, description="留空则用生成的项目名")
    description: str = ""
    tags: list[str] = Field(default_factory=list)


class ProjectUpdateRequest(BaseModel):
    """只更新传了的字段；None 表示不动。"""

    name: str | None = None
    description: str | None = None
    status: ProjectStatus | None = None
    tags: list[str] | None = None


class ProjectFileWriteRequest(BaseModel):
    content: str
