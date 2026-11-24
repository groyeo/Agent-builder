from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """全局配置。所有配置项均可通过 .env 或环境变量覆盖。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # 应用元信息
    app_name: str = "Agent Builder"
    app_description: str = "AI Agent 自动构建平台"
    app_version: str = "0.1.0"
    stage: str = "stage-3: components & workbench"

    # 服务监听
    host: str = "127.0.0.1"
    port: int = 8000
    debug: bool = True

    # 前端跨域来源（Vite 默认端口）
    cors_origins: list[str] = [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ]

    # ---- 模型适配层 ----
    # provider=mock 时强制使用离线假模型；否则走 OpenAI 兼容协议
    llm_provider: str = "deepseek"
    llm_api_key: str = ""
    llm_base_url: str = "https://api.deepseek.com/v1"
    llm_model: str = "deepseek-chat"
    llm_timeout: float = 120.0
    llm_temperature: float = 0.2
    # ---- 思考强度 ----
    # 四档：off / low / medium / high（见 core/llm/thinking.py）。
    # 空字符串表示"未显式设置"，此时回落到旧的 llm_disable_thinking 开关。
    # 前端「思考强度」下拉框改的就是这个值，会落到 data/llm_settings.json 持久化。
    llm_thinking: str = ""
    # 旧开关（兼容已有 .env）：思考 token 往往占输出的 60~70%，而本平台每一步
    # 都要求输出严格 JSON，思考对结构合规帮助有限、对成本影响巨大。
    llm_disable_thinking: bool = True
    # 各家字段名不同，统一用 llm_extra_body 透传并覆盖上面的映射。
    llm_extra_body: dict = {}
    # 未配置 Key 时自动降级为 Mock，保证链路可跑通（响应中会标明 provider=mock）
    llm_allow_mock_fallback: bool = True

    # ---- 需求分析器 ----
    analyzer_max_retries: int = 2

    # ---- 项目持久化 ----
    # 生成结果落到本地磁盘（data/projects/<id>/），脱离进程内存，重启不丢。
    data_dir: str = "data"
    # 流水线跑完代码生成后自动保存为项目；关掉则只能手动保存
    auto_save_project: bool = True

    # ---- 执行隔离 ----
    # auto=有 docker 就用容器，没有就退回子进程；docker=强制容器（没有就报错）；
    # local=强制本机子进程（调试用，会失去隔离）。
    sandbox_driver: str = "auto"
    # 容器限额：跑的是脚手架测试，给太多反而掩盖资源问题
    sandbox_memory: str = "768m"
    sandbox_cpus: str = "1"
    sandbox_pids: int = 256

    # ---- 架构人工审批 ----
    # true 时流水线在架构规划后暂停（waiting_approval），用户确认后才继续生成组件
    require_architecture_approval: bool = False

    # ---- 任务持久化 ----
    # 任务与事件写穿到 SQLite，重启后仍能查；空字符串表示用 data/jobs.db
    jobs_db: str = ""


@lru_cache
def get_settings() -> Settings:
    """配置单例，避免重复读取 .env。"""
    return Settings()


settings = get_settings()
