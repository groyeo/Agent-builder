"""交付清单（Delivery Plan）的数据结构。

为什么生成代码还不够：
代码只是骨架，真正让 Agent 跑起来的是"外面那堆东西" —— 模型 Key、地图 Key、
客服手册、Excel 样例、企业微信机器人地址。这些东西不在代码里，也不该由代码猜，
必须变成一份**用户能照着做的清单**：缺什么、去哪注册、填到哪个变量、没配会怎样。

因此这里的每条资源都强制带四要素：为什么需要（why）、填到哪（target）、
怎么拿到（steps + signup_url）、缺了会怎样（fallback）。
只写"需要 API Key"而不写这四样，等于没写。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

ResourceKind = Literal[
    "llm",  # 模型服务：唯一必需项，没有它什么都跑不了
    "api_key",  # 第三方服务 Key（地图、搜索、支付…）
    "credential",  # 账号类凭证（SMTP 授权码、飞书 App Secret…）
    "knowledge_doc",  # 知识库语料：客服手册、制度文档、产品说明
    "data_file",  # 运行时输入的数据文件：Excel、PDF、合同、候选人简历
    "account",  # 需要开通/授权的账号或店铺
    "infra",  # 基础设施：对象存储、向量库、MCP 服务地址
]


class ResourceItem(BaseModel):
    key: str = Field(description="稳定标识，如 amap / smtp / knowledge_base")
    name: str
    kind: ResourceKind
    required: bool = Field(description="缺了就跑不起来（true）还是能降级（false）")
    why: str = Field(description="这个 Agent 为什么需要它，关联到哪个组件/需求")
    env_vars: list[str] = Field(
        default_factory=list, description="要填进 .env 的变量名，空表示不是环境变量"
    )
    target: str = Field(description="东西放哪：.env 的哪个变量，或项目里哪个目录")
    signup_url: str | None = Field(default=None, description="注册/获取入口")
    steps: list[str] = Field(default_factory=list, description="照着做就能拿到，3~6 步")
    cost_hint: str = Field(default="", description="费用与免费额度的量级描述")
    fallback: str = Field(default="", description="没配时的行为，决定能不能先跳过")
    example: str = Field(default="", description="填成什么样算对（写成注释，不塞进变量值）")
    env_defaults: dict[str, str] = Field(
        default_factory=dict,
        description="非密钥类变量的默认值，如 OPENAI_BASE_URL / SMTP_HOST",
    )


class StartupStep(BaseModel):
    order: int
    title: str
    command: str | None = None
    detail: str = ""


class DeliveryPlan(BaseModel):
    summary: str = ""
    resources: list[ResourceItem] = Field(default_factory=list)
    startup_steps: list[StartupStep] = Field(default_factory=list)
    blockers: list[str] = Field(
        default_factory=list, description="必需但未提供的项，按优先级排列"
    )
    check_command: str = "python scripts/check_env.py"

    @property
    def required_blocks(self) -> list[ResourceItem]:
        return [r for r in self.resources if r.required]

    def env_template_lines(self) -> list[str]:
        """按清单生成 .env.example 的行，保证"清单里的变量"和"模板里的变量"一致。"""
        lines: list[str] = []
        for r in self.resources:
            if not r.env_vars:
                continue
            lines.append(f"# {r.name} —— {r.why}")
            if r.signup_url:
                lines.append(f"# 获取：{r.signup_url}")
            if r.example:
                lines.append(f"# 示例：{r.example}")
            for var in r.env_vars:
                # 密钥类留空让用户填；地址/主机名这类给默认值，少一处可填错的地方
                lines.append(f"{var}={r.env_defaults.get(var, '')}")
            lines.append("")
        return lines
