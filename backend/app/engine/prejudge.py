"""架构模式的规则预判。

方案 5.2 要求"规则预判 + LLM 解释评估 + 图结构校验"，本模块负责第一层：
不调用模型，仅凭需求里的客观特征给出一个有依据的倾向和复杂度粗评分。

这么做的理由：模型对"选哪种架构"容易给出漂亮的空话，而特征计数是稳定、可复现的。
规则不负责最终定案，但它能在模型明显跑偏时提供对照，也保证模型不可用时系统仍给得出结论。
"""

from __future__ import annotations

from app.schemas.architecture import ArchitectureType, PreJudgement
from app.schemas.requirement import RequirementSpec

# 出现这些词，说明存在"专业分工"，是 Multi-Agent 的主要信号
_ROLE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "检索": ("检索", "搜索", "召回", "找资料"),
    "分析": ("分析", "归因", "诊断", "评估"),
    "生成": ("生成", "撰写", "起草", "输出报告"),
    "审核": ("审核", "复核", "校验", "质检", "校对"),
    "分类": ("分类", "分流", "标记", "归类"),
}

# 出现这些词，说明流程是确定性的，更适合 Workflow
_FLOW_KEYWORDS = ("先", "然后", "再", "最后", "之后", "依次", "按流程", "步骤如下")
_PARALLEL_KEYWORDS = ("同时", "并行", "分别", "各自")
_RAG_KEYWORDS = ("知识库", "FAQ", "常见问题", "文档检索", "语料", "帮助中心", "制度")
_MEMORY_KEYWORDS = ("记住", "记忆", "历史偏好", "跨会话", "长期", "上次")


def _hits(text: str, keywords: tuple[str, ...]) -> bool:
    return any(k in text for k in keywords)


def _requirement_text(spec: RequirementSpec) -> str:
    """把需求里所有文本字段拼成一段，供关键词扫描。"""
    parts = [
        spec.agent_name,
        spec.goal,
        *spec.features,
        *spec.inputs,
        *spec.outputs,
        *spec.constraints,
        *spec.external_dependencies,
    ]
    return "\n".join(parts)


def prejudge(spec: RequirementSpec) -> PreJudgement:
    text = _requirement_text(spec)
    signals: list[str] = []
    score = 0

    # 1) 功能规模
    feature_count = len(spec.features)
    if feature_count >= 9:
        score += 2
        signals.append(f"功能点较多（{feature_count} 个），单 Agent 上下文压力大")
    elif feature_count >= 5:
        score += 1
        signals.append(f"功能点中等（{feature_count} 个）")

    # 2) 外部依赖
    dep_count = len(spec.external_dependencies)
    if dep_count >= 2:
        score += 2
        signals.append(f"依赖 {dep_count} 个外部系统，需要工具层与容错设计")
    elif dep_count == 1:
        score += 1
        signals.append("依赖 1 个外部系统")

    # 3) 专业角色分工
    roles = [name for name, kws in _ROLE_KEYWORDS.items() if _hits(text, kws)]
    if len(roles) >= 3:
        score += 2
        signals.append(f"存在 {len(roles)} 类专业动作（{'、'.join(roles)}），倾向按角色拆分")
    elif len(roles) == 2:
        score += 1
        signals.append(f"存在 2 类专业动作（{'、'.join(roles)}）")

    # 4) 流程确定性
    has_flow = _hits(text, _FLOW_KEYWORDS)
    if has_flow:
        signals.append("描述中出现明确的先后顺序词汇，流程具有确定性")
    if _hits(text, _PARALLEL_KEYWORDS):
        score += 1
        signals.append("描述中出现并行/分别处理的诉求")

    # 5) 检索与记忆诉求
    needs_rag = _hits(text, _RAG_KEYWORDS)
    if needs_rag:
        score += 1
        signals.append("需求涉及知识库/FAQ 检索，可能需要 RAG 组件")
    needs_memory = _hits(text, _MEMORY_KEYWORDS)
    if needs_memory:
        score += 1
        signals.append("需求涉及跨会话记忆")

    # 6) 输入形态多样
    if len(spec.inputs) >= 2:
        score += 1
        signals.append(f"存在 {len(spec.inputs)} 类输入")

    score = min(score, 10)
    suggested = _decide(score, roles, has_flow, signals)

    return PreJudgement(
        suggested_type=suggested,
        complexity_score=score,
        signals=signals,
    )


def _decide(
    score: int, roles: list[str], has_flow: bool, signals: list[str]
) -> ArchitectureType:
    """决策顺序即优先级：先排除简单，再看流程确定性，最后才考虑角色拆分。"""
    if score <= 2:
        signals.append("整体特征简单，规则倾向 Single Agent")
        return "single_agent"

    if has_flow and len(roles) >= 2:
        signals.append("流程确定 + 多专业角色，规则倾向 Hybrid")
        return "hybrid"

    if has_flow:
        signals.append("流程确定且角色单一，规则倾向 Workflow")
        return "workflow"

    if len(roles) >= 3:
        signals.append("多专业角色且无固定流程，规则倾向 Multi-Agent")
        return "multi_agent"

    if score >= 7:
        signals.append("复杂度评分较高，规则倾向 Multi-Agent")
        return "multi_agent"

    signals.append("特征不足以支撑拆分，规则倾向 Single Agent")
    return "single_agent"
