"""Component Registry —— 组件类型工程知识库（方案 6.3）。

这里沉淀的是"什么情况该用什么组件、接口必须有哪些字段、至少要怎么测"。
它有两个用途：
1. 作为 Prompt 的一部分注入模型，减少模型对组件边界的误解；
2. 作为校验器的判据，接口缺字段能被机器发现，而不是靠人看。

新增组件类型时只改这里，Prompt 与校验器自动跟进。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ComponentMeta:
    type: str
    label: str
    when_to_use: str
    when_not_to_use: str
    required_interface_fields: tuple[str, ...] = ()
    default_tests: tuple[str, ...] = ()


REGISTRY: dict[str, ComponentMeta] = {
    "prompt": ComponentMeta(
        type="prompt",
        label="提示词",
        when_to_use="需要约束模型的行为边界、输出格式或判断标准时。每个有 LLM 参与的节点至少一条。",
        when_not_to_use="纯确定性步骤（如字段解析、写库）不需要 prompt。",
        required_interface_fields=("role",),
        default_tests=("对典型输入输出格式正确", "越界输入有兜底回复"),
    ),
    "skill": ComponentMeta(
        type="skill",
        label="技能",
        when_to_use="同一套方法论要在多个节点或多个项目复用，且有稳定的步骤与判定标准。",
        when_not_to_use="只用一次的逻辑不要包装成 skill，直接写在节点里更清楚。",
        required_interface_fields=("steps",),
        default_tests=("按步骤产出符合预期的中间结果",),
    ),
    "tool": ComponentMeta(
        type="tool",
        label="工具",
        when_to_use="确定性操作：读写数据库、调外部 HTTP、解析文件、发消息。不依赖模型判断。",
        when_not_to_use="需要理解语义才能完成的事不要做成 tool。",
        required_interface_fields=("input_schema", "output_desc"),
        default_tests=("正常入参返回预期结构", "异常入参返回明确错误", "超时有降级"),
    ),
    "mcp": ComponentMeta(
        type="mcp",
        label="MCP 服务",
        when_to_use="需要以标准协议接入跨应用的外部工具服务，且该服务已有或应有 MCP Server。",
        when_not_to_use="本地函数就能完成的事，优先做普通 tool（方案 6.6 明确要求）。",
        required_interface_fields=("transport", "server_ref"),
        default_tests=("连接可用", "工具清单与权限符合预期"),
    ),
    "harness": ComponentMeta(
        type="harness",
        label="运行框架",
        when_to_use="任何需要多步循环、状态维护、超时与重试的场景。每个项目至少一个。",
        when_not_to_use="单次一问一答、无状态的一次性调用可以省。",
        required_interface_fields=("max_steps", "timeout_s"),
        default_tests=("达到 max_steps 能安全终止", "超时能中断并记录", "失败可重试且不重复副作用"),
    ),
    "memory": ComponentMeta(
        type="memory",
        label="记忆",
        when_to_use="信息需要跨步骤或跨会话保留（如用户偏好、已处理过的工单 id）。",
        when_not_to_use="本次运行内的中间变量放状态里即可，不需要独立记忆组件。",
        required_interface_fields=("scope", "key_fields"),
        default_tests=("写入后可读回", "过期与冲突有处理策略"),
    ),
    "rag": ComponentMeta(
        type="rag",
        label="知识检索",
        when_to_use="回答必须基于外部知识库，且知识规模超出可直接塞进上下文。",
        when_not_to_use="知识量很小（几段话）时直接放进 prompt；没有知识来源时不要凭空造 RAG。",
        required_interface_fields=("knowledge_source", "chunk_strategy"),
        default_tests=("命中已知问题能召回正确条目", "无命中时明确返回未命中而非编造"),
    ),
}


def get_meta(comp_type: str) -> ComponentMeta | None:
    return REGISTRY.get(comp_type)


def describe_for_prompt() -> str:
    """把注册表压成 Prompt 里的一段说明。"""
    lines = []
    for meta in REGISTRY.values():
        fields = (
            f"；interface 必填字段：{', '.join(meta.required_interface_fields)}"
            if meta.required_interface_fields
            else ""
        )
        lines.append(
            f"- {meta.type}（{meta.label}）：该用时——{meta.when_to_use}"
            f" 不该用时——{meta.when_not_to_use}{fields}"
        )
    return "\n".join(lines)
