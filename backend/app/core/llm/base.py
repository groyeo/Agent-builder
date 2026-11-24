"""模型适配层的公共契约。

所有大模型提供商的差异都收敛在这里，上层（Requirement Analyzer 等）只依赖 LLMAdapter，
这样换模型（DeepSeek / OpenAI / 通义 / 本地 Ollama）不需要改业务代码。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class Message:
    role: str  # system | user | assistant
    content: str

    def as_dict(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}


@dataclass(slots=True)
class LLMResult:
    """一次模型调用的结果。"""

    content: str
    model: str
    provider: str
    usage: dict[str, Any] = field(default_factory=dict)
    latency_ms: int = 0
    attempts: int = 1


class LLMError(RuntimeError):
    """模型调用失败（网络、鉴权、限流、返回内容不可用等）。"""

    def __init__(self, message: str, *, provider: str = "", retryable: bool = False):
        super().__init__(message)
        self.provider = provider
        self.retryable = retryable


class LLMAdapter(ABC):
    """统一模型接口。"""

    name: str = "base"

    @abstractmethod
    def chat(
        self,
        messages: list[Message],
        *,
        json_mode: bool = False,
        temperature: float | None = None,
    ) -> LLMResult:
        """发起一次对话补全。

        json_mode=True 时要求模型返回严格 JSON（结构化输出的前提）。
        """

    @property
    def available(self) -> bool:
        """是否已具备真实调用条件（如已配置密钥）。"""
        return True
