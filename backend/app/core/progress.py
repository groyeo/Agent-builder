"""进度上报通道。

设计动机：Agent 内部（重试、校验反馈）发生的中间过程，对判断"这 60 秒到底在
干什么"极其关键，但把 on_event 回调一路透传到每个 Agent 会污染所有函数签名。

这里用 ContextVar 承载"当前任务的进度接收器"：
- 调用方在进入一个步骤前 `with bind_progress(sink):`，步骤内的 `emit()` 自动落到该 sink；
- 未绑定时 `emit()` 是空操作，因此 Agent 在单测和 CLI 里可以照常运行；
- 通过 asyncio.to_thread 进入工作线程时上下文会自动复制，所以同步的 Agent 代码
  无需感知线程问题。
"""

from __future__ import annotations

import contextlib
import contextvars
from collections.abc import Callable, Iterator

ProgressSink = Callable[[str], None]

_current_sink: contextvars.ContextVar[ProgressSink | None] = contextvars.ContextVar(
    "agent_builder_progress_sink", default=None
)


def emit(message: str) -> None:
    """上报一条人类可读的进度消息；没有接收器时静默丢弃。"""
    sink = _current_sink.get()
    if sink is not None:
        sink(message)


@contextlib.contextmanager
def bind_progress(sink: ProgressSink | None) -> Iterator[None]:
    """在该上下文内把 emit() 定向到给定接收器。"""
    token = _current_sink.set(sink)
    try:
        yield
    finally:
        _current_sink.reset(token)
