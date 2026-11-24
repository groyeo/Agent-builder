"""Code Generator —— 组件清单 → 可运行的 Python 项目脚手架。

为什么是模板而不是"让模型写代码"：
1. 成本：模型生成 10+ 个文件意味着数万 token，而本平台的每一步已经在花钱；
2. 可测：模板产出的结构稳定，能在生成后立刻做 AST 校验、甚至直接跑 pytest；
3. 可改：用户要接手的是"有注释、有来源、结构一致"的工程骨架，
   而不是一段来路不明的生成片段。

模型在这里的唯一可选项是 prompt 正文（`prompt_body_from_llm`），默认关闭。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from app.engine.clarify_plan import (
    MAX_QUESTIONS,
    DomainSlots,
    SlotSpec,
    design_questions,
    plan_slots,
    sample_input,
)
from app.engine.delivery_planner import plan_delivery
from app.schemas.architecture import ArchitectureSpec
from app.schemas.codegen import CodegenResult, CodegenStats, CoverItem, GeneratedFile
from app.schemas.components import ComponentPlan, ComponentSpec
from app.schemas.delivery import DeliveryPlan
from app.schemas.requirement import RequirementSpec

_SAFE_NAME = re.compile(r"[^a-z0-9_]+")


@dataclass
class _Context:
    requirement: RequirementSpec
    architecture: ArchitectureSpec
    plan: ComponentPlan
    project: str
    delivery: DeliveryPlan | None = None
    #: 生成出来的 Agent 该先问用户什么（领域画像 + 需求里的阻塞问题）
    slots: list[SlotSpec] = field(default_factory=list)
    domain: DomainSlots | None = None
    sample: str = ""
    #: 构建期没答、影响实现方式的问题（不进运行时提问，写进 README）
    design_questions: list[tuple[str, str]] = field(default_factory=list)
    nodes: list[str] = field(default_factory=list)
    prompt_by_node: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def of_type(self, *types: str) -> list[ComponentSpec]:
        return [c for c in self.plan.components if c.type in types]

    def node_name(self, node_id: str) -> str:
        for a in self.architecture.agents:
            if a.id == node_id:
                return a.name
        return node_id


def _sanitize(name: str, fallback: str = "agent_app") -> str:
    cleaned = _SAFE_NAME.sub("_", name.strip().lower()).strip("_")
    if not cleaned or cleaned[0].isdigit():
        return fallback
    return cleaned


def _topological_order(arch: ArchitectureSpec) -> tuple[list[str], bool]:
    """返回（执行顺序, 是否成环）。成环时顺序退化为声明顺序。"""
    ids = [a.id for a in arch.agents]
    indegree = {i: 0 for i in ids}
    adjacency: dict[str, list[str]] = {i: [] for i in ids}
    for edge in arch.workflow_edges:
        if edge.source in indegree and edge.target in indegree:
            adjacency[edge.source].append(edge.target)
            indegree[edge.target] += 1

    queue = [i for i in ids if indegree[i] == 0]
    order: list[str] = []
    while queue:
        node = queue.pop(0)
        order.append(node)
        for nxt in adjacency[node]:
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                queue.append(nxt)

    if len(order) != len(ids):
        # 有环：不让图论问题阻塞生成，退回声明顺序并明确告警
        return ids, True
    return order, False


def _file(path: str, content: str, kind: str, source: str = "template") -> GeneratedFile:
    text = content.rstrip() + "\n"
    return GeneratedFile(
        path=path,
        content=text,
        kind=kind,  # type: ignore[arg-type]
        lines=text.count("\n"),
        source=source,
    )


def _py_str(value: str) -> str:
    """把任意文本安全地嵌进 Python 字符串字面量。"""
    return json.dumps(value, ensure_ascii=False)


def _doc_block(lines: list[str], indent: int = 0) -> str:
    pad = " " * indent
    return "\n".join(f"{pad}{line}" for line in lines)


# ---------------------------------------------------------------- 各文件模板


def _readme(ctx: _Context) -> str:
    req, arch = ctx.requirement, ctx.architecture
    comp_rows = "\n".join(
        f"| {c.name} | {c.type} | {c.purpose} | {', '.join(c.owner_agents) or '-'} |"
        for c in ctx.plan.components
    )
    node_rows = "\n".join(
        f"| `{a.id}` | {a.name} | {a.responsibility} |" for a in arch.agents
    )
    return f"""# {req.agent_name}

{req.goal}

> 由 Agent Builder 生成（模板生成，未调用模型写代码）。
> 生成依据：结构化需求 + 架构方案 + 组件清单，三者与代码一一对应，可回溯。

## 架构

- 类型：`{arch.architecture_type}`
- 复杂度：`{arch.complexity_level}`
- 选择理由：{arch.reasoning_summary}

| 节点 | 名称 | 职责 |
| --- | --- | --- |
{node_rows}

## 组件

| 组件 | 类型 | 解决什么问题 | 使用节点 |
| --- | --- | --- | --- |
{comp_rows}

## 快速开始

**第一份该看的文件是 [DELIVERY.md](DELIVERY.md)** —— 它逐项列出跑起来要准备什么
（模型 Key、第三方 API、业务语料、样例数据），每项去哪注册、怎么拿、填到哪个变量、
没配会怎样。下面是命令版：

```bash
python start.py --check     # 一键准备：生成 .env → 装依赖 → 自检，逐项报缺
#   按提示把 .env 里的空值填上（每项去哪注册见 DELIVERY.md）
python start.py --web       # 打开网页版：自动开浏览器，直接对话
python start.py "你的输入"  # 只想在终端跑一次
pytest -q                   # 脚手架自带测试，不需联网
```

Windows 直接双击 `run.bat` 即可：等同于 `python start.py --web`，会起一个本机网页
并自动打开浏览器。网页只监听 127.0.0.1，关掉终端窗口就停止。

## 网页版怎么用

- `python start.py --web`（或双击 `run.bat`）→ 浏览器打开 `http://127.0.0.1:8765`；
- 页面里直接说需求，缺关键信息时会把问题列出来，补答后可以重跑；
- 端口被占用会自动往后找，也可以 `--port 9000` 指定；
- 换机器/容器里跑：`python server.py --host 0.0.0.0 --no-browser`；
- 页面是 `web/index.html` 单文件，不依赖 CDN，断网可用；想改样式直接改它。

## 先提问，再生成

`agent/clarify.py` 是"缺口检查"：能从你这句话里读到的就不再问，
读不到的才问（最多 {MAX_QUESTIONS} 个）。比如：

```
你：帮我规划成都3天旅行，预算3000元，喜欢吃货和熊猫
? 你从哪座城市出发？            ← 这句话里没有
? 大概什么时候去、什么时候回？    ← 这句话里也没有
```

不提问就直接生成的代价：返程时间、住哪一片全靠模型猜，猜错不报错，只是不能用。

- **Jupyter**：打开 `notebook.ipynb`，`handle()` 会弹输入框问你；
- **终端**：`python start.py "..."` 直接问；
- **CI / 定时脚本**：不会阻塞，缺的信息放在 `result["unanswered"]` 里，你自己决定怎么处理。

## 目录

```
DELIVERY.md         交付清单：要准备什么、去哪注册、怎么启动 ← 先看这个
start.py           一条命令启动：补 .env → 装依赖 → 自检 → 跑 / 开网页
server.py          本机网页服务（标准库，零依赖）：python server.py
web/index.html     网页界面（单文件，不依赖 CDN）
run.bat / run.sh   Windows / macOS 一键启动：双击打开网页
notebook.ipynb     Jupyter 里交互式试跑（缺信息会弹输入框问你）
scripts/check_env.py 环境自检，逐项报告缺口
config.yaml        运行参数（步数上限、超时、重试）
main.py            只跑 Agent 的入口（不装依赖、不自检）
agent/clarify.py   缺口清单：先问清楚哪些信息再动手
agent/env.py       .env 载入器：运行时所有配置的唯一入口
agent/llm.py       OpenAI 兼容客户端（httpx，无额外依赖）
agent/prompts.py   每个节点的提示词
agent/tools.py     工具桩（待接入真实系统）
agent/harness.py   运行循环：步数/超时/重试/终止
agent/router.py    按架构边生成的编排逻辑
tests/             可在无网络、无 Key 条件下运行的测试
Dockerfile         部署镜像（不含 .env，密钥运行时注入）
docker-compose.yml 一条命令起起来（读同目录 .env）
.dockerignore      把 .env / 缓存 / data 挡在镜像外
```

## 配置约定（改代码前先看这条）

- **所有配置只走 `agent/env.py`**：`from .env import get` 然后 `get("OPENAI_API_KEY")`。
  不要在业务代码里直接 `os.environ[...]` —— 直接读环境变量会绕过 `.env` 载入，
  表现为"自检通过、一跑就报缺少 OPENAI_BASE_URL"。
- **启动先体检**：`python start.py --check` 会装依赖、跑 `scripts/check_env.py`、
  再按运行时路径复查模型配置（`agent/llm.py: preflight()`）。缺什么直接给出
  改哪个文件、填哪几行、去哪申请 Key。
- **提问在体检之后**：链路是 补 `.env` → 装依赖 → 自检 → 复查模型配置 → 才问澄清问题。
  不会出现"答完两个问题才发现没配 Key"。
- 新增第三方 Key（地图、邮件、数据库）时：变量写进 `.env.example` 与 DELIVERY.md，
  代码里用 `env.get("XXX")` 取；缺失时要像 `llm.diagnose()` 那样说明**去哪注册**。

## 容器化部署

```bash
docker build -t {_service_name(ctx)} .
docker run --rm -it --env-file .env {_service_name(ctx)}          # 交互跑
docker compose run --rm {_service_name(ctx)} python start.py --check   # 只体检
```

镜像里刻意不放 `.env`：密钥进了镜像层就等于进了 Git 历史，删不干净。
配置一律运行时注入（`--env-file` 或 `-e`）。

## 待办（生成时已知的空白）

{chr(10).join(f"- {c.name}：{c.purpose} —— 当前为桩，需接入真实实现" for c in ctx.of_type("tool", "mcp")) or "- 无"}

## 构建时没定的问题（影响实现，不影响每次运行）

{chr(10).join(f"- **{q}** —— {why}" for q, why in ctx.design_questions) or "- 无"}
"""


def _config_yaml(ctx: _Context) -> str:
    harness = ctx.of_type("harness")
    params = harness[0].interface if harness else {}
    max_steps = params.get("max_steps", 8)
    timeout_s = params.get("timeout_s", 120)
    nodes = "\n".join(f"  - id: {a.id}\n    name: {json.dumps(a.name, ensure_ascii=False)}" for a in ctx.architecture.agents)
    return f"""# 运行参数。改这里比改代码更安全 —— harness 会读取这些值。
project: {json.dumps(ctx.requirement.agent_name, ensure_ascii=False)}
architecture: {ctx.architecture.architecture_type}
complexity: {ctx.architecture.complexity_level}

harness:
  max_steps: {max_steps}
  timeout_s: {timeout_s}
  retry: {params.get("retry", 1)}

model:
  # 通过环境变量覆盖，不要在这里写 Key
  base_url: ${{OPENAI_BASE_URL}}
  model: ${{OPENAI_MODEL}}

nodes:
{nodes}
"""


def _env_example(ctx: _Context) -> str:
    """按交付清单生成，保证"清单里的变量"和"模板里的变量"永远一致。

    以前这里写死三个变量，结果清单里提到的地图 Key 在模板里没有位置，
    用户只能猜变量名 —— 清单与模板对齐比模板好读更重要。
    """
    header = [
        "# 本文件由 Agent Builder 按「交付清单」生成，逐项说明见 DELIVERY.md",
        "# 复制为 .env 后填写；留空的必需项会被 scripts/check_env.py 报出来",
        "",
    ]
    if ctx.delivery is None:
        return "\n".join(header + [
            "OPENAI_BASE_URL=https://api.deepseek.com/v1",
            "OPENAI_API_KEY=",
            "OPENAI_MODEL=deepseek-chat",
        ])
    body = ctx.delivery.env_template_lines()
    return "\n".join(header + body + [
        "# ---- 运行参数（一般不用改）----",
        "LOG_LEVEL=INFO",
    ])


def _requirements(ctx: _Context) -> str:
    lines = ["httpx>=0.27", "pyyaml>=6.0", "pytest>=8.0", "pytest-asyncio>=0.23"]
    if ctx.of_type("mcp"):
        lines.append("# mcp>=1.0  # 接入 MCP 时取消注释")
    return "\n".join(lines)


_ENV_BODY = r'''"""把项目根目录的 .env 载入 os.environ（已存在的环境变量优先）。

为什么需要这一层：生成物早期只在 scripts/check_env.py 里读了 .env 文件，
运行时的 agent/llm.py 直接看 os.environ —— 于是出现"自检通过、一跑就报缺少
OPENAI_BASE_URL"。现在解析只有这一份，llm / tools / main 都通过 get() 取值。
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"

_values: dict[str, str] = {}
_loaded = False


def parse(path: Path | None = None) -> dict[str, str]:
    """按 KEY=VALUE 逐行解析；支持引号、行尾注释、export 前缀、UTF-8 BOM。"""
    target = path or ENV_FILE
    if not target.exists():
        return {}
    values: dict[str, str] = {}
    for raw in target.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        values[key] = value
    return values


def load(force: bool = False) -> dict[str, str]:
    """载入 .env。已有同名环境变量时不覆盖（容器/CI 通常直接给环境变量）。"""
    global _loaded, _values
    if _loaded and not force:
        return _values
    _values = parse()
    for key, value in _values.items():
        if value:
            os.environ.setdefault(key, value)
    _loaded = True
    return _values


def get(name: str, default: str = "") -> str:
    """取一个配置项（先保证 .env 已载入）。"""
    load()
    return (os.environ.get(name) or default).strip()
'''


_LLM_BODY = r'''"""OpenAI 兼容的模型客户端。

只用 httpx，不引入 openai SDK：本项目的调用形态很窄（一次一问一答），
自己封装比多一个依赖更好控制超时与错误信息。
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from .env import ENV_FILE, get, load

# 默认值只用于"报错时告诉你该填什么"，不会替你猜
DEFAULT_BASE_URL = "__DEFAULT_BASE_URL__"
DEFAULT_MODEL = "__DEFAULT_MODEL__"
SIGNUP_URL = "__SIGNUP_URL__"


class LLMError(RuntimeError):
    pass


def preflight() -> list[str]:
    """返回还缺哪些必需变量（空列表 = 可以跑）。

    取值前会把 .env 载入环境（见 agent/env.py）。这一步以前是漏的：
    scripts/check_env.py 自己读了 .env，而运行时的 llm 只看 os.environ，
    结果自检通过、真正调用时才报"缺少 OPENAI_BASE_URL"。
    """
    load()
    missing: list[str] = []
    for name in ("OPENAI_BASE_URL", "OPENAI_MODEL", "OPENAI_API_KEY"):
        if not get(name):
            missing.append(name)
    return missing


def diagnose(missing: list[str]) -> str:
    """把"缺什么"翻译成"去哪改、改成什么"，并指出真实的 .env 路径。"""
    exists = ENV_FILE.exists()
    here = f"{ENV_FILE}（{'已存在' if exists else '不存在'}）"
    key_state = "已填" if get("OPENAI_API_KEY") else "（空）"
    return "\n".join(
        [
            "模型配置不完整：缺少 " + "、".join(missing),
            f"  读取位置：{here}",
            f"  当前值：OPENAI_BASE_URL={get('OPENAI_BASE_URL') or '（空）'}，"
            f"OPENAI_MODEL={get('OPENAI_MODEL') or '（空）'}，OPENAI_API_KEY={key_state}",
            "",
            "  怎么补（改完 .env 直接重跑，不用改代码）：",
            "    1) 没有 .env 就先复制：cp .env.example .env（Windows：copy .env.example .env）",
            "    2) 填这三行：",
            f"         OPENAI_BASE_URL={DEFAULT_BASE_URL}",
            "         OPENAI_API_KEY=sk-你的Key",
            f"         OPENAI_MODEL={DEFAULT_MODEL}",
            "    3) 复检：python start.py --check",
            "",
            f"  Key 申请：{SIGNUP_URL}",
        ]
    )


def _settings() -> tuple[str, str, str]:
    missing = preflight()
    if missing:
        raise LLMError(diagnose(missing))
    return (
        get("OPENAI_BASE_URL").rstrip("/"),
        get("OPENAI_API_KEY"),
        get("OPENAI_MODEL"),
    )


def chat(
    messages: list[dict[str, str]],
    *,
    json_mode: bool = False,
    temperature: float = 0.2,
    timeout: float = 120.0,
) -> str:
    """一次对话调用，返回文本内容。"""
    base_url, api_key, model = _settings()
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "stream": False,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    try:
        resp = httpx.post(
            f"{base_url}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
            timeout=timeout,
        )
    except httpx.TimeoutException as exc:
        raise LLMError(
            f"模型请求超时（{timeout}s）。网络不通或该服务较慢，可调大 timeout 或换服务。"
        ) from exc
    except httpx.HTTPError as exc:
        raise LLMError(f"模型请求失败：{exc}") from exc

    if resp.status_code == 401:
        raise LLMError(
            "模型返回 401：API Key 无效或已过期。\n"
            f"  当前 Key 取自 {ENV_FILE}，请重新创建后填入；申请：{SIGNUP_URL}"
        )
    if resp.status_code == 404:
        raise LLMError(
            "模型返回 404：OPENAI_BASE_URL 可能多了或少了路径。\n"
            f"  当前值：{base_url}（应形如 {DEFAULT_BASE_URL}，不要带 /chat/completions）"
        )
    if resp.status_code >= 400:
        raise LLMError(f"模型返回 {resp.status_code}：{resp.text[:300]}")

    body = resp.json()
    content = body["choices"][0]["message"].get("content") or ""
    if not content.strip():
        raise LLMError("模型返回空内容")
    return content
'''


_LLM_FALLBACK_BASE_URL = "https://api.deepseek.com/v1"
_LLM_FALLBACK_MODEL = "deepseek-chat"
_LLM_FALLBACK_SIGNUP = "https://platform.deepseek.com/api_keys"


def _llm_signup(ctx: _Context) -> str:
    """去交付清单里找大模型那条的注册地址，保证报错里的链接与 DELIVERY.md 一致。"""
    if ctx.delivery:
        for r in ctx.delivery.resources:
            if r.kind == "llm" and r.signup_url:
                return r.signup_url
    return _LLM_FALLBACK_SIGNUP


def _env_py(ctx: _Context) -> str:
    return _ENV_BODY


def _llm_py(ctx: _Context) -> str:
    base, model = _LLM_FALLBACK_BASE_URL, _LLM_FALLBACK_MODEL
    if ctx.delivery:
        for r in ctx.delivery.resources:
            if r.kind != "llm":
                continue
            defaults = dict(r.env_defaults or {})
            base = defaults.get("OPENAI_BASE_URL", base)
            model = defaults.get("OPENAI_MODEL", model)
    return (
        _LLM_BODY.replace("__DEFAULT_BASE_URL__", base)
        .replace("__DEFAULT_MODEL__", model)
        .replace("__SIGNUP_URL__", _llm_signup(ctx))
    )


def _prompts_py(ctx: _Context) -> str:
    req = ctx.requirement
    blocks: list[str] = []
    for agent in ctx.architecture.agents:
        prompt_comp = next(
            (c for c in ctx.plan.components if c.type == "prompt" and agent.id in c.owner_agents),
            None,
        )
        role = (prompt_comp.interface.get("role") if prompt_comp else None) or agent.name
        purpose = prompt_comp.purpose if prompt_comp else agent.responsibility
        criteria = "\n".join(f"- {c}" for c in req.acceptance_criteria) or "- （需求未给出验收标准，需补充）"
        blocks.append(
            f"PROMPTS[{_py_str(agent.id)}] = (\n"
            f"    {_py_str(f'你是{role}。')}\n"
            f"    + {_py_str('唯一职责：' + agent.responsibility)}\n"
            f"    + '\\n\\n验收标准（每条都要能被判定通过与否）：\\n'\n"
            f"    + {_py_str(criteria)}\n"
            f")\n"
        )

    return (
        '"""每个节点的系统提示词。\n\n'
        "生成来源：组件清单里的 prompt 组件（role + purpose）+ 架构节点的 responsibility\n"
        '+ 需求的 acceptance_criteria。改动提示词请同步更新 tests/test_prompts.py 的断言。\n'
        '"""\n\n'
        "from __future__ import annotations\n\n"
        "# 节点 id → 系统提示词\n"
        "PROMPTS: dict[str, str] = {}\n\n"
        f"{chr(10).join(blocks)}\n"
        "NODE_IDS: list[str] = list(PROMPTS)\n"
    )


def _tools_py(ctx: _Context) -> str:
    tools = ctx.of_type("tool")
    if not tools:
        body = (
            "TOOLS: dict[str, object] = {}\n\n\n"
            "def list_tools() -> list[str]:\n"
            '    """当前没有被声明为 tool 的组件。"""\n'
            "    return []\n"
        )
    else:
        chunks: list[str] = []
        for tool in tools:
            schema = tool.interface.get("input_schema", {})
            schema_text = json.dumps(schema, ensure_ascii=False, indent=4)
            chunks.append(
                f"\ndef {tool.name}(**kwargs: object) -> object:\n"
                f'    """{tool.purpose}\n\n'
                f"    输入契约（来自组件设计）：\n"
                f"{_doc_block(schema_text.splitlines(), 4)}\n\n"
                f"    输出：{tool.interface.get('output_desc', '未声明')}\n"
                f'    """\n'
                f"    raise NotImplementedError(\n"
                f"        {_py_str(tool.name + ' 尚未接入真实实现：' + tool.purpose)}\n"
                f"    )\n"
            )
        registry = "\n".join(f"    {_py_str(t.name)}: {t.name}," for t in tools)
        body = (
            f"{''.join(chunks)}\n\n"
            "TOOLS: dict[str, object] = {\n"
            f"{registry}\n"
            "}\n\n\n"
            "def list_tools() -> list[str]:\n"
            "    return list(TOOLS)\n"
        )

    return (
        '"""工具桩。\n\n'
        "确定性操作才应该是 tool；需要理解语义的事情留给模型。\n"
        "每个函数都显式 raise NotImplementedError —— 宁可启动就报错，\n"
        "也不要返回一个看似成功的假结果。\n"
        '"""\n\n'
        "from __future__ import annotations\n\n"
        f"{body}"
    )


def _mcp_py(ctx: _Context) -> str:
    servers = ctx.of_type("mcp")
    entries = "\n".join(
        f"    {_py_str(s.name)}: {{'transport': {_py_str(str(s.interface.get('transport', 'stdio')))}, "
        f"'server_ref': {_py_str(str(s.interface.get('server_ref', '')))}}},"
        for s in servers
    )
    return (
        '"""MCP 客户端配置位。\n\n'
        "跨应用的标准协议接入才用 MCP；本地函数能做的请留在 tools.py。\n"
        "这里只放配置与调用入口，真正的会话管理按你选的 SDK 补上。\n"
        '"""\n\n'
        "from __future__ import annotations\n\n"
        "MCP_SERVERS: dict[str, dict[str, str]] = {\n"
        f"{entries}\n"
        "}\n\n\n"
        "def call(server: str, tool: str, arguments: dict[str, object]) -> object:\n"
        '    """调用 MCP 服务上的工具。"""\n'
        '    transport = MCP_SERVERS[server]["transport"]\n'
        "    raise NotImplementedError(\n"
        '        f"MCP 调用尚未接入：{server}.{tool}，transport={transport}，请先建立会话"\n'
        "    )\n"
    )


def _memory_py(ctx: _Context) -> str:
    memories = ctx.of_type("memory")
    scope = memories[0].interface.get("scope", "session") if memories else "session"
    keys = memories[0].interface.get("key_fields", []) if memories else []
    return f'''"""跨步骤/跨会话的记忆。

当前最小实现：进程内字典。真正上线要换成持久化存储（Redis / 数据库），
但接口保持不变，调用方不需要改。
"""

from __future__ import annotations

from typing import Any

SCOPE = {_py_str(str(scope))}
KEY_FIELDS: list[str] = {json.dumps(keys, ensure_ascii=False)}


class Memory:
    def __init__(self) -> None:
        self._store: dict[str, Any] = {{}}

    def key_of(self, state: dict[str, Any]) -> str:
        """按 KEY_FIELDS 拼出记忆键；没有声明关键字段时退化为常量键。"""
        if not KEY_FIELDS:
            return "__default__"
        return "|".join(str(state.get(k, "")) for k in KEY_FIELDS)

    def load(self, state: dict[str, Any]) -> Any:
        return self._store.get(self.key_of(state))

    def save(self, state: dict[str, Any], value: Any) -> None:
        self._store[self.key_of(state)] = value
'''


def _rag_py(ctx: _Context) -> str:
    rags = ctx.of_type("rag")
    source = rags[0].interface.get("knowledge_source", "未声明") if rags else "未声明"
    chunk = rags[0].interface.get("chunk_strategy", "未声明") if rags else "未声明"
    return f'''"""知识检索。

知识来源：{source}
切分策略：{chunk}

生成时没有替你把检索实现掉，是因为"用什么向量库"取决于你的部署环境，
随便塞一个进去只会让后面更难改。这里给出接口与失败语义。
"""

from __future__ import annotations


def retrieve(query: str, top_k: int = 5) -> list[str]:
    """返回与 query 最相关的若干知识片段。"""
    raise NotImplementedError(
        "RAG 尚未接入：请配置知识来源（{source}）与切分策略（{chunk}）后实现本函数"
    )
'''


def _skills_py(ctx: _Context) -> str:
    skills = ctx.of_type("skill")
    if not skills:
        return ""
    chunks: list[str] = []
    for skill in skills:
        steps = skill.interface.get("steps", [])
        step_text = "\n".join(f"    {i + 1}. {s}" for i, s in enumerate(steps)) or "    1. （未声明步骤）"
        chunks.append(
            f"\n\ndef {skill.name}(context: dict[str, object]) -> dict[str, object]:\n"
            f'    """{skill.purpose}\n\n'
            f"    步骤：\n{step_text}\n"
            f'    """\n'
            f"    raise NotImplementedError({_py_str(skill.name + ' 尚未实现')})\n"
        )
    return (
        '"""可复用的专业能力封装。\n\n'
        "skill 是稳定方法论，不是一次调用；因此独立成模块，便于被多个节点复用。\n"
        '"""\n\n'
        "from __future__ import annotations\n"
        f"{''.join(chunks)}"
    )


def _harness_py(ctx: _Context) -> str:
    harness = ctx.of_type("harness")
    params = harness[0].interface if harness else {}
    max_steps = params.get("max_steps", 8)
    timeout_s = params.get("timeout_s", 120)
    retry = params.get("retry", 1)
    return f'''"""运行框架：步数、超时、重试、终止条件。

为什么要单独一层：不加约束的 Agent 循环会无限重试、超时不退出、
失败吞掉异常继续跑。这三件事都写成显式规则，出问题时能一眼定位。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

MAX_STEPS = {max_steps}
TIMEOUT_S = {timeout_s}
RETRY = {retry}


@dataclass
class RunResult:
    ok: bool
    output: Any = None
    steps: int = 0
    error: str | None = None
    trace: list[str] = field(default_factory=list)


def run(
    step_fn: Callable[[dict[str, Any]], dict[str, Any]],
    state: dict[str, Any],
    *,
    max_steps: int = MAX_STEPS,
    timeout_s: float = TIMEOUT_S,
    retry: int = RETRY,
) -> RunResult:
    """驱动一次执行。step_fn 接收并返回 state。"""
    started = time.monotonic()
    trace: list[str] = []

    for step in range(1, max_steps + 1):
        if time.monotonic() - started > timeout_s:
            return RunResult(False, state, step - 1, f"超过 {timeout_s}s 超时", trace)

        last_error: Exception | None = None
        for attempt in range(retry + 1):
            try:
                state = step_fn(state)
                trace.append(f"step {{step}} ok" + (f" (retry {{attempt}})" if attempt else ""))
                last_error = None
                break
            except Exception as exc:  # noqa: BLE001 - 交给上层判定是否致命
                last_error = exc
                trace.append(f"step {{step}} failed: {{type(exc).__name__}}: {{exc}}")

        if last_error is not None:
            return RunResult(False, state, step, f"{{type(last_error).__name__}}: {{last_error}}", trace)

        if state.get("done"):
            return RunResult(True, state.get("output"), step, None, trace)

    return RunResult(False, state, max_steps, f"达到步数上限 {{max_steps}} 仍未结束", trace)
'''


def _router_py(ctx: _Context) -> str:
    arch = ctx.architecture
    order, _cyclic = _topological_order(arch)
    conditions: dict[str, list[str]] = {}
    for edge in arch.workflow_edges:
        if edge.condition:
            conditions.setdefault(edge.target, []).append(edge.condition)

    steps: list[str] = []
    for idx, node_id in enumerate(order):
        for cond in conditions.get(node_id, []):
            steps.append(f"    # 进入条件：{cond}")
        # 注意用 current 而不是 state：在嵌套函数里对 state 赋值会变成局部变量，
        # 导致 UnboundLocalError（这个坑生成过一次，别再踩）。
        steps.append(f"    current.setdefault('trace', []).append({_py_str(node_id)})")
        steps.append(f"    current = _step_{node_id}(current, chat_fn)")
        steps.append("")

    step_funcs: list[str] = []
    for agent in arch.agents:
        step_funcs.append(
            f"""
def _step_{agent.id}(state: dict[str, Any], chat_fn: ChatFn) -> dict[str, Any]:
    \"\"\"{agent.name}：{agent.responsibility}\"\"\"
    prompt = PROMPTS[{_py_str(agent.id)}]
    messages = [{{"role": "system", "content": prompt}}]
    # 多轮对话：网页会把前几轮带过来，否则"那它呢"这种追问永远答不对
    messages.extend(state.get("history") or [])
    messages.append({{"role": "user", "content": str(state.get("input", ""))}})
    reply = chat_fn(messages)
    state.setdefault("outputs", {{}})["{agent.id}"] = reply
    return state
"""
        )

    if arch.architecture_type == "single_agent" and len(order) == 1:
        entry_note = "单节点架构：一次调用即完成。"
    else:
        entry_note = f"编排顺序（由架构边拓扑排序得出）：{' → '.join(order)}"

    return (
        '"""编排逻辑。\n\n'
        f"{entry_note}\n"
        "条件分支目前以注释形式保留：条件文本是自然语言，直接编译成 if 会变成\n"
        "看似严谨实则拍脑袋的规则。要不要让模型判定分支，是产品决策，留给你。\n"
        '"""\n\n'
        "from __future__ import annotations\n\n"
        "from typing import Any, Callable\n\n"
        "from .clarify import AskFn, collect, render\n"
        "from .harness import run as run_harness\n"
        "from .llm import chat as default_chat\n"
        "from .prompts import PROMPTS\n\n"
        "ChatFn = Callable[[list[dict[str, str]]], str]\n\n\n"
        "def handle(\n"
        "    input_text: str,\n"
        "    *,\n"
        "    chat_fn: ChatFn | None = None,\n"
        "    ask_fn: AskFn | None = None,\n"
        "    interactive: bool | None = None,\n"
        "    answers: dict[str, str] | None = None,\n"
        "    history: list[dict[str, str]] | None = None,\n"
        ") -> dict[str, Any]:\n"
        '    """先补齐缺口，再跑完整条链路。\n\n'
        "    interactive=None 时自动判断：传了 ask_fn、在 Jupyter、或在终端里就会提问；\n"
        "    其他情况（pytest、网页版、定时任务）不阻塞，把问题放进 unanswered 返回。\n"
        "    answers 是网页版已经补答的内容（slot id → 回答），直接并入 brief。\n"
        "    history 是同一会话的前几轮 [{\"role\", \"content\"}]，让追问能接上上文。\n"
        '    """\n'
        "    chat_fn = chat_fn or default_chat\n"
        "    brief, asked, unanswered = collect(\n"
        "        input_text, ask_fn=ask_fn, interactive=interactive, preset=answers\n"
        "    )\n"
        "    state: dict[str, Any] = {\n"
        '        "input": render(input_text, brief),\n'
        '        "brief": brief,\n'
        '        "asked": asked,\n'
        '        "unanswered": unanswered,\n'
        '        "history": history or [],\n'
        "    }\n\n"
        "    def one_step(current: dict[str, Any]) -> dict[str, Any]:\n"
        f"{chr(10).join('    ' + line if line else line for line in steps) or '        pass'}\n"
        '        current["done"] = True\n'
        '        current["output"] = current.get("outputs", {})\n'
        "        return current\n\n"
        "    result = run_harness(one_step, state)\n"
        "    return {\n"
        '        "ok": result.ok,\n'
        '        "output": result.output,\n'
        '        # 澄清结果要一起返回：用户得知道刚才回答的都用在哪了\n'
        '        "brief": brief,\n'
        '        "asked": asked,\n'
        '        "unanswered": unanswered,\n'
        '        "steps": result.steps,\n'
        '        "error": result.error,\n'
        '        "trace": result.trace,\n'
        "    }\n"
        f"{''.join(step_funcs)}"
    )


def _main_py(ctx: _Context) -> str:
    return '''"""命令行入口（start.py 会先帮你补 .env、装依赖、做自检，再调这里）。

    python main.py "把这条工单分到技术类"
    python main.py --no-ask "..."    # 不提问，缺的信息列在 unanswered 里
"""

from __future__ import annotations

import sys

from agent.env import load as load_env


def main(argv: list[str]) -> int:
    # 先载入 .env：不经过 start.py、直接跑 main.py 时也要能读到配置
    load_env()

    from agent.llm import LLMError, diagnose, preflight

    missing = preflight()
    if missing:
        # 在提问之前拦住：让人答完几个问题才发现"没配 Key"，等于白答
        print(diagnose(missing))
        print()
        print('配好后重跑：python main.py "你的输入"')
        return 2

    from agent.router import handle

    args = [a for a in argv[1:]]
    ask = "--no-ask" not in args
    text = " ".join(a for a in args if not a.startswith("--")).strip()
    if not text:
        print('用法：python main.py "你的输入"（缺信息会先反问你）')
        return 2

    try:
        result = handle(text, interactive=None if ask else False)
    except LLMError as exc:
        print(f"[失败] {exc}")
        return 1

    for node, output in (result.get("output") or {}).items():
        print(f"===== {node} =====")
        print(output)
        print()
    if result.get("asked"):
        detail = "、".join(f"{k}={v}" for k, v in (result.get("brief") or {}).items() if v)
        print(f"[已确认 {len(result['asked'])} 项]" + (f" {detail}" if detail else ""))
    if result.get("unanswered"):
        print("以下信息没确认，结果按缺省假设生成：")
        for item in result["unanswered"]:
            print(f"  - {item['question']}")
    if not result.get("ok"):
        print(f"[失败] {result.get('error')}")
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
'''


def _conftest_py(ctx: _Context) -> str:
    """让 pytest 能把项目根加入 sys.path，否则 `import agent` 会失败。"""
    return '''"""pytest 配置：把项目根目录加入 sys.path。

没有这一行，`import agent.router` 在 `pytest` 下会报 ModuleNotFoundError，
因为 pytest 默认只把测试文件所在目录加进 sys.path。
"""

from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
'''


def _init_py(ctx: _Context) -> str:
    return '"""由 Agent Builder 生成的 Agent 工程。"""\n\n__all__: list[str] = []\n'


def _test_prompts(ctx: _Context) -> str:
    node_ids = [a.id for a in ctx.architecture.agents]
    return f'''"""提示词契约：节点必须都有提示词，且不能是空壳。"""

from __future__ import annotations

from agent.prompts import NODE_IDS, PROMPTS

EXPECTED_NODES = {json.dumps(node_ids, ensure_ascii=False)}


def test_every_node_has_prompt() -> None:
    missing = [n for n in EXPECTED_NODES if n not in PROMPTS]
    assert not missing, f"缺少提示词：{{missing}}"


def test_no_extra_prompt() -> None:
    extra = [n for n in NODE_IDS if n not in EXPECTED_NODES]
    assert not extra, f"提示词指向了不存在的节点：{{extra}}"


def test_prompts_are_substantive() -> None:
    for node, text in PROMPTS.items():
        assert len(text.strip()) >= 20, f"{{node}} 的提示词过短，无法约束行为"
'''


def _test_tools(ctx: _Context) -> str:
    tools = ctx.of_type("tool")
    names = [t.name for t in tools]
    if not names:
        return '''"""没有声明 tool 组件，工具清单应为空。"""

from __future__ import annotations

from agent.tools import list_tools


def test_no_tools_declared() -> None:
    assert list_tools() == []
'''
    return f'''"""工具契约：未实现的工具必须显式报错，而不是返回假数据。"""

from __future__ import annotations

import pytest

from agent.tools import TOOLS, list_tools

EXPECTED = {json.dumps(names, ensure_ascii=False)}


def test_registry_matches_declaration() -> None:
    assert sorted(list_tools()) == sorted(EXPECTED)


@pytest.mark.parametrize("name", EXPECTED)
def test_unimplemented_tool_raises(name: str) -> None:
    with pytest.raises(NotImplementedError):
        TOOLS[name]()
'''


def _test_router(ctx: _Context) -> str:
    node_ids = [a.id for a in ctx.architecture.agents]
    return f'''"""编排测试：用假的 chat 函数跑通全流程，不需要网络和 Key。"""

from __future__ import annotations

from agent.router import handle

ORDER = {json.dumps(node_ids, ensure_ascii=False)}


def fake_chat(messages: list[dict[str, str]]) -> str:
    assert messages and messages[0]["role"] == "system"
    return "ok"


def test_pipeline_runs_to_completion() -> None:
    result = handle("测试输入", chat_fn=fake_chat)
    assert result["ok"] is True, result["error"]
    assert result["steps"] >= 1


def test_all_nodes_produced_output() -> None:
    result = handle("测试输入", chat_fn=fake_chat)
    produced = set(result["output"])
    assert set(ORDER) <= produced, f"未产出结果的节点：{{set(ORDER) - produced}}"
'''


def _coverage(ctx: _Context) -> list[CoverItem]:
    items: list[CoverItem] = []
    for comp in ctx.plan.components:
        target = {
            "prompt": "agent/prompts.py",
            "tool": "agent/tools.py",
            "mcp": "agent/mcp_client.py",
            "memory": "agent/memory.py",
            "rag": "agent/rag.py",
            "skill": "agent/skills.py",
            "harness": "agent/harness.py",
        }.get(comp.type, "-")
        if target == "-":
            continue
        items.append(
            CoverItem(component=comp.name, component_type=comp.type, target=target)
        )
    return items


def generate(
    requirement: RequirementSpec,
    architecture: ArchitectureSpec,
    plan: ComponentPlan,
) -> CodegenResult:
    """把设计产物渲染成一个完整工程。纯确定性，不调用模型。"""
    project = _sanitize(requirement.agent_name)
    # 交付清单先算：.env.example 与 DELIVERY.md 都以它为准，避免两处不一致
    delivery = plan_delivery(requirement, architecture, plan)
    # 缺口清单也先算：clarify / notebook / 示例输入三处共用，保证 demo 与测试同源
    slots, domain = plan_slots(requirement)
    ctx = _Context(
        requirement=requirement,
        architecture=architecture,
        plan=plan,
        project=project,
        delivery=delivery,
        slots=slots,
        domain=domain,
        sample=sample_input(requirement, domain),
        design_questions=design_questions(requirement),
    )

    files: list[GeneratedFile] = [
        _file("README.md", _readme(ctx), "markdown"),
        _file("DELIVERY.md", _delivery_md(ctx), "markdown"),
        _file("scripts/check_env.py", _check_env_py(ctx), "python"),
        _file("requirements.txt", _requirements(ctx), "text"),
        _file(".env.example", _env_example(ctx), "text"),
        _file("config.yaml", _config_yaml(ctx), "yaml"),
        _file("start.py", _start_py(ctx), "python"),
        _file("run.bat", _run_bat(ctx), "text"),
        _file("run.sh", _run_sh(ctx), "text"),
        _file("Dockerfile", _dockerfile(ctx), "text"),
        _file(".dockerignore", _dockerignore(ctx), "text"),
        _file("docker-compose.yml", _docker_compose(ctx), "yaml"),
        _file("notebook.ipynb", _notebook_ipynb(ctx), "json"),
        _file("main.py", _main_py(ctx), "python"),
        _file("server.py", _server_py(ctx), "python"),
        _file("web/index.html", _web_index(ctx), "text"),
        _file("agent/clarify.py", _clarify_py(ctx), "python"),
        _file("agent/__init__.py", _init_py(ctx), "python"),
        _file("agent/env.py", _env_py(ctx), "python"),
        _file("agent/llm.py", _llm_py(ctx), "python"),
        _file("agent/prompts.py", _prompts_py(ctx), "python"),
        _file("agent/tools.py", _tools_py(ctx), "python"),
        _file("agent/harness.py", _harness_py(ctx), "python"),
        _file("agent/router.py", _router_py(ctx), "python"),
        _file("tests/test_prompts.py", _test_prompts(ctx), "python"),
        _file("tests/test_tools.py", _test_tools(ctx), "python"),
        _file("tests/test_router.py", _test_router(ctx), "python"),
        _file("tests/test_clarify.py", _test_clarify(ctx), "python"),
        _file("conftest.py", _conftest_py(ctx), "python"),
    ]

    if ctx.of_type("mcp"):
        files.append(_file("agent/mcp_client.py", _mcp_py(ctx), "python"))
    if ctx.of_type("memory"):
        files.append(_file("agent/memory.py", _memory_py(ctx), "python"))
    if ctx.of_type("rag"):
        files.append(_file("agent/rag.py", _rag_py(ctx), "python"))
    skills = _skills_py(ctx)
    if skills:
        files.append(_file("agent/skills.py", skills, "python"))

    # 目录也要"有文件才存在"：空的 knowledge/ 在 git 里根本不会被保存
    if any(r.kind == "knowledge_doc" for r in delivery.resources):
        files.append(_file("knowledge/README.md", _knowledge_readme(ctx), "markdown"))
    if any(r.kind == "data_file" for r in delivery.resources):
        files.append(_file("data/README.md", _data_readme(ctx), "markdown"))

    # 组件 → 文件 的溯源，方便用户核对"我设计的东西都落地了吗"
    for item in _coverage(ctx):
        for f in files:
            if f.path == item.target:
                f.source = f"component:{item.component}"
                break

    warnings: list[str] = []
    if _topological_order(architecture)[1]:
        warnings.append(
            "架构图存在环，编排顺序退化为声明顺序；建议回到架构页修正后再生成。"
        )
    unanswered = [
        q for q in requirement.clarifying_questions if getattr(q, "blocking", False)
    ]
    if unanswered:
        warnings.append(
            f"需求中仍有 {len(unanswered)} 个阻塞问题未澄清，"
            "生成代码里的工具桩与提示词可能偏离真实系统。"
        )

    stats = CodegenStats(
        files=len(files),
        lines=sum(f.lines for f in files),
        python_files=sum(1 for f in files if f.kind == "python"),
        tests=sum(1 for f in files if f.path.startswith("tests/")),
    )

    return CodegenResult(
        project_name=project,
        files=files,
        tree=render_tree(files),
        stats=stats,
        coverage=_coverage(ctx),
        warnings=warnings,
        delivery=delivery,
        notes=[
            "代码由模板生成，未消耗模型 token；prompt 正文来自组件设计的 role 与需求验收标准。",
            "工具与 MCP 一律生成显式 NotImplementedError 的桩，避免假的成功结果。",
            f"交付清单列出 {len(delivery.resources)} 项要准备的东西，"
            f"其中 {len(delivery.required_blocks)} 项必需，见 DELIVERY.md。",
        ],
    )


def _delivery_md(ctx: _Context) -> str:
    """把交付清单渲染成人读的 markdown —— 用户拿到项目后第一份该看的文件。"""
    plan = ctx.delivery
    req = ctx.requirement
    if plan is None:  # pragma: no cover - generate_code 总会传
        return "# 交付清单\n\n（未生成交付清单）"

    def card(idx: int, r) -> str:
        signup = f"- **去哪里拿**：{r.signup_url}\n" if r.signup_url else ""
        steps = "\n".join(f"   {i + 1}. {s}" for i, s in enumerate(r.steps))
        envs = (
            "\n- **变量名**：" + "、".join(f"`{v}`" for v in r.env_vars)
            if r.env_vars
            else ""
        )
        return (
            f"### {idx}. {r.name}\n\n"
            f"- **为什么需要**：{r.why}\n"
            f"- **放到哪里**：{r.target}{envs}\n"
            f"{signup}"
            f"- **怎么拿到**：\n{steps}\n"
            f"- **费用**：{r.cost_hint}\n"
            f"- **没配会怎样**：{r.fallback}\n"
        )

    required = [r for r in plan.resources if r.required]
    optional = [r for r in plan.resources if not r.required]
    req_block = "\n".join(card(i + 1, r) for i, r in enumerate(required))
    opt_block = (
        "\n".join(card(i + 1, r) for i, r in enumerate(optional))
        or "_这一项没有可后补的资源 —— 清单里的东西都是硬依赖。_"
    )

    startup = "\n".join(
        f"{s.order}. **{s.title}**"
        + (f"\n\n   ```bash\n   {s.command}\n   ```" if s.command else "")
        + (f"\n\n   {s.detail}" if s.detail else "")
        for s in plan.startup_steps
    )

    stubs = [
        c for c in ctx.plan.components if c.type in ("tool", "mcp", "rag", "memory")
    ]
    stub_rows = "\n".join(
        f"| `{c.name}` | {c.type} | "
        + ("本地可用，无需外部服务" if c.type == "memory" else "需要真实 API/语料，见上面清单")
        + " |"
        for c in stubs
    )

    kb = [r for r in plan.resources if r.kind == "knowledge_doc"]
    data = [r for r in plan.resources if r.kind == "data_file"]
    dir_block = ""
    if kb or data:
        dir_block = "\n## 三、目录约定\n\n"
        if kb:
            dir_block += (
                "- `knowledge/` —— 业务语料。把手册/制度/FAQ 转成 `.md` 或 `.txt`，"
                "一个主题一个文件。**这一项最影响回答质量**。\n"
            )
        if data:
            dir_block += (
                "- `data/` —— 样例输入文件（Excel/PDF/图片）。"
                "先放 1~3 个脱敏样例跑通，再换全量。\n"
            )

    n3 = "四" if dir_block else "三"
    n4 = "五" if dir_block else "四"

    return f"""# {req.agent_name} · 交付清单

{plan.summary}

> 本文件由 Agent Builder 依据你的需求、架构与组件清单推导生成 —— 不是通用模板。
> 每一条都对应这个 Agent 真实需要的东西；按顺序做完即可跑通。

## 一、必需项（{len(required)} 项，缺了就起不来）

{req_block}

## 二、可后补项（{len(optional)} 项，先跑通再加）

{opt_block}
{dir_block}
## {n3}、从零启动（照着敲）

{startup}

> 自检脚本可以随时重复跑：`{plan.check_command}`，它会逐项告诉你还缺什么。

## {n4}、哪些地方还得你接真实实现

生成时这些地方是**显式的桩**（`raise NotImplementedError`），不会假装成功：

| 组件 | 类型 | 说明 |
| --- | --- | --- |
{stub_rows}

接完一个就补一条测试，别一次全接 —— 出问题时才好定位是哪一个。
"""


def _check_env_py(ctx: _Context) -> str:
    """生成自检脚本：把"清单"变成可执行的一步，而不是让人对着文档逐条核对。"""
    plan = ctx.delivery
    resources = plan.resources if plan else []
    env_items = [
        {
            "name": r.name,
            "vars": r.env_vars,
            "required": r.required,
            "signup": r.signup_url or "",
        }
        for r in resources
        if r.env_vars
    ]
    dir_items = [
        {"path": "knowledge", "name": r.name, "required": r.required}
        for r in resources
        if r.kind == "knowledge_doc"
    ] + [
        {"path": "data", "name": r.name, "required": False}
        for r in resources
        if r.kind == "data_file"
    ]
    return f'''"""环境自检：告诉你还差哪几项，不用对着 DELIVERY.md 逐条核对。

用法：python scripts/check_env.py
退出码 0 表示必需项齐全，1 表示还有硬缺口（此时 main.py 大概率起不来）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"

# 由 Agent Builder 按交付清单生成；改清单请回 DELIVERY.md 对照
ENV_ITEMS = {env_items!r}
DIR_ITEMS = {dir_items!r}


def load_env() -> dict[str, str]:
    """读 .env（不引第三方库，格式就按 KEY=VALUE 处理）。"""
    values: dict[str, str] = {{}}
    if not ENV_FILE.exists():
        print(f"[!] 找不到 {{ENV_FILE.name}} —— 先执行：cp .env.example .env")
        return values
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, raw = line.partition("=")
        values[key.strip()] = raw.strip().strip('"').strip("'")
    return values


def main() -> int:
    print(f"项目根目录：{{ROOT}}")
    env = load_env()
    # 真实部署时环境变量优先于 .env（容器/CI 里通常只给环境变量）
    env.update({{k: v for k, v in os.environ.items() if k in env and v}})

    missing_required: list[str] = []
    missing_optional: list[str] = []
    for item in ENV_ITEMS:
        empty = [v for v in item["vars"] if not env.get(v)]
        if not empty:
            print(f"[OK]   {{item['name']}}")
            continue
        tag = "缺失" if item["required"] else "未配（可后补）"
        print(f"[..]   {{item['name']}} —— {{tag}}：{{', '.join(empty)}}")
        if item["signup"]:
            print(f"       获取：{{item['signup']}}")
        (missing_required if item["required"] else missing_optional).append(item["name"])

    for item in DIR_ITEMS:
        target = ROOT / item["path"]
        files = (
            [p for p in target.glob("*") if p.name != "README.md"]
            if target.exists()
            else []
        )
        flag = "[OK]" if files else ("[!!]" if item["required"] else "[..]")
        state = f"{{len(files)}} 个文件" if files else "还没有内容"
        print(f"{{flag}}   {{item['name']}}（{{item['path']}}/）—— {{state}}")
        if not files and item["required"]:
            missing_required.append(item["name"])

    print()
    if missing_required:
        print(f"还缺 {{len(missing_required)}} 项必需内容：")
        for name in missing_required:
            print(f"  - {{name}}")
        print("补齐后重跑本脚本；逐项说明见 DELIVERY.md")
        return 1

    print('必需项齐全，可以跑：pytest -q && python main.py "你的输入"')
    if missing_optional:
        print("可后补项：" + "、".join(missing_optional))
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''


def _knowledge_readme(ctx: _Context) -> str:
    return """# knowledge/ —— 业务语料放这里

这个目录是 Agent 的"业务记忆"。放什么、怎么放，直接决定它答得准不准。

## 放什么

- 产品/服务说明、报价规则
- 客服话术、退换货政策、常见问题（FAQ）
- 内部制度、审批流程、SOP
- 行业条款、合规要求

## 怎么放

1. 转成纯文本：Word「另存为 → 纯文本」，PDF 直接复制正文，网页复制正文即可
2. 一个主题一个文件，文件名说人话：`退换货政策.md`、`产品报价.md`
3. 单文件建议不超过 3000 字；太长就按小节拆开
4. 不要放扫描图片（纯文本检索读不出图片里的字）

## 验证是否加载

```bash
python -c "from agent.rag import list_docs; print(list_docs())"
```
"""


def _data_readme(ctx: _Context) -> str:
    return """# data/ —— 样例输入文件放这里

先放 1~3 个**脱敏**样例跑通，再换全量数据；排错时数据越小越快。

- Excel / CSV：保留真实表头，行数可以只留几十行
- PDF：用真实版式的样例，别用空白文件
- 图片：分辨率与实际使用一致

注意：这个目录默认不进 git（含业务数据），别把个人信息、密钥放进来。
"""


def _slot_literal(slots: list[SlotSpec]) -> str:
    chunks: list[str] = []
    for s in slots:
        parts = [
            f"        id={_py_str(s.id)},",
            f"        label={_py_str(s.label)},",
            f"        question={_py_str(s.question)},",
        ]
        if s.why:
            parts.append(f"        why={_py_str(s.why)},")
        if s.patterns:
            pats = ", ".join(_py_str(p) for p in s.patterns)
            parts.append(f"        patterns=({pats},),")
        if s.options:
            opts = ", ".join(_py_str(o) for o in s.options)
            parts.append(f"        options=({opts},),")
        if not s.required:
            parts.append("        required=False,")
        chunks.append("    Slot(\n" + "\n".join(parts) + "\n    ),")
    return "\n".join(chunks)


_CLARIFY_BODY = r'''"""先问清楚，再动手。

直接按一句"帮我规划成都 3 天"就出方案，返程时间、住哪一片、预算怎么分配，
全是模型替你猜的。猜错了不会报错，只是结果不能用 —— 这比报错更浪费时间。

所以入口先做一次缺口检查：能从原话里读到的就不再问，读不到的才问，
最多 @@MAX@@ 个。非交互环境（pytest、定时脚本）不会卡住：
它把待确认的问题原样返回，你可以在任何界面上接着问。
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from typing import Callable

#: 一次最多问几个。问太多用户会直接关掉，剩下的按缺省假设继续并标出来。
MAX_QUESTIONS = @@MAX@@


@dataclass(frozen=True)
class Slot:
    """一项要跟用户确认的信息。

    patterns 是"用户已经说了"的判定规则：命中就不再问，避免重复盘问。
    """

    id: str
    label: str
    question: str
    why: str = ""
    patterns: tuple[str, ...] = ()
    options: tuple[str, ...] = ()
    required: bool = True


# 由 Agent Builder 依据需求生成（领域画像 + 需求里的阻塞问题）
SLOTS: tuple[Slot, ...] = (
@@SLOTS@@
)

AskFn = Callable[[Slot], str]


def in_notebook() -> bool:
    """判断是否在 Jupyter 里。

    不能只看 sys.stdin.isatty()：notebook 里它不是 tty，但 input() 完全可用
    （会在单元格下方弹出输入框）。只看 isatty 就会误判成"非交互"而不提问。
    """
    try:
        from IPython import get_ipython  # noqa: PLC0415 - 只在 notebook 里有
    except Exception:  # pragma: no cover - 普通环境没有 IPython
        return False
    return get_ipython() is not None


def extract(text: str) -> dict[str, str]:
    """从原话里读出已经给出的信息。"""
    found: dict[str, str] = {}
    for slot in SLOTS:
        for pattern in slot.patterns:
            match = re.search(pattern, text)
            if match:
                value = match.group(1) if match.groups() else match.group(0)
                found[slot.id] = value.strip()
                break
    return found


def pending(text: str) -> list[Slot]:
    """还缺哪些必填信息（按 SLOTS 顺序）。"""
    found = extract(text)
    return [s for s in SLOTS if s.required and s.id not in found]


def console_ask(slot: Slot) -> str:
    """默认问法：命令行和 Jupyter 都能用（Jupyter 里会弹输入框）。"""
    print(f"\n? {slot.question}")
    if slot.why:
        print(f"  为什么问：{slot.why}")
    if slot.options:
        print("  可选：" + " / ".join(slot.options))
    try:
        return input("  你的回答：").strip()
    except (EOFError, KeyboardInterrupt):
        print("  （跳过，按缺省假设继续）")
        return ""


def collect(
    text: str,
    *,
    ask_fn: AskFn | None = None,
    interactive: bool | None = None,
    max_questions: int = MAX_QUESTIONS,
    preset: dict[str, str] | None = None,
) -> tuple[dict[str, str], list[dict[str, object]], list[dict[str, object]]]:
    """补齐缺口。返回 (已确认信息, 问过的问题, 没来得及问的问题)。

    preset 是网页版用的：用户在页面上补答的内容直接并入 brief，
    不必把补答拼回原句再靠正则猜一遍（猜错比不问更糟）。
    """
    brief = extract(text)
    if preset:
        for key, value in preset.items():
            cleaned = str(value).strip()
            if cleaned:
                brief[key] = cleaned
    todo = [s for s in SLOTS if s.required and s.id not in brief]

    if interactive is None:
        interactive = ask_fn is not None or in_notebook() or sys.stdin.isatty()

    ask = ask_fn or console_ask
    asked: list[dict[str, object]] = []
    unanswered: list[dict[str, object]] = []

    for idx, slot in enumerate(todo):
        item = {
            "id": slot.id,
            "label": slot.label,
            "question": slot.question,
            "why": slot.why,
            "options": list(slot.options),
        }
        if not interactive or idx >= max_questions:
            unanswered.append(item)
            continue
        answer = (ask(slot) or "").strip()
        asked.append({**item, "answer": answer})
        if answer:
            brief[slot.id] = answer

    return brief, asked, unanswered


def render(text: str, brief: dict[str, str]) -> str:
    """把确认到的信息并进输入 —— 提示词里必须带着它，否则问了也白问。"""
    labels = {s.id: s.label for s in SLOTS}
    lines = [f"- {labels.get(k, k)}：{v}" for k, v in brief.items() if v]
    if not lines:
        return text
    return text + "\n\n【已确认的关键信息】\n" + "\n".join(lines)
'''


def _clarify_py(ctx: _Context) -> str:
    slots = ctx.slots
    literal = _slot_literal(slots) if slots else "    # 这个需求没有识别出必填缺口"
    return (
        _CLARIFY_BODY.replace("@@MAX@@", str(MAX_QUESTIONS))
        .replace("@@SLOTS@@", literal)
        .rstrip()
        + "\n"
    )


_SERVER_BODY = r'''"""本机网页入口：python server.py（或 python start.py --web）。

为什么用标准库而不是 FastAPI + uvicorn：
目标 Agent 的诉求是"点开就能说话"，为它多装两个依赖、多一个 ASGI 进程
不划算。标准库的 ThreadingHTTPServer 足够，且零依赖、离线可用。

    python server.py                 起服务并自动打开浏览器
    python server.py --port 9000     换端口（被占用会自动往后找）
    python server.py --no-browser    只起服务（容器 / 远程机器）
"""

from __future__ import annotations

import json
import socket
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

WEB_DIR = ROOT / "web"
INDEX_FILE = WEB_DIR / "index.html"

AGENT_NAME = __AGENT_NAME__
AGENT_GOAL = __AGENT_GOAL__
NODES = __NODES_JSON__
SAMPLE = __SAMPLE_JSON__

#: 请求体上限：这是聊天框，不是文件上传口
MAX_BODY = 64 * 1024
#: 同时最多几个对话。本地单机够用，也避免一口气打满模型额度
MAX_CONCURRENT = 3
_slots = threading.BoundedSemaphore(MAX_CONCURRENT)

#: 多轮上下文上限：要接得住"那它呢"，但不能把额度烧在复读上
MAX_HISTORY = 8
MAX_HISTORY_CHARS = 2000


def _clean_history(raw) -> list[dict[str, str]]:
    """网页是前端，传什么都不该照单全收：只留 user/assistant 的短文本。"""
    if not isinstance(raw, list):
        return []
    items: list[dict[str, str]] = []
    for entry in raw[-MAX_HISTORY:]:
        if not isinstance(entry, dict):
            continue
        role = str(entry.get("role") or "")
        content = str(entry.get("content") or "").strip()
        if role in ("user", "assistant") and content:
            items.append({"role": role, "content": content[:MAX_HISTORY_CHARS]})
    return items


def runtime_state() -> dict:
    """运行时就绪状态：缺哪些配置、或者根本导入不了。"""
    try:
        from agent.env import load
        from agent.llm import preflight

        load()  # .env 是配置的唯一入口（见 agent/env.py）
        return {"missing": preflight(), "error": None}
    except Exception as exc:  # noqa: BLE001 - 依赖没装也要能开网页
        return {"missing": [], "error": f"{type(exc).__name__}: {exc}"}


def handle_message(
    text: str,
    answers: dict[str, str] | None = None,
    history: list[dict[str, str]] | None = None,
) -> dict:
    """跑一次完整链路。interactive=False：网页里不能阻塞 input()。"""
    text = (text or "").strip()
    if not text:
        return {"ok": False, "error": "消息为空", "output": {}, "unanswered": []}
    from agent.router import handle

    result = handle(
        text, interactive=False, answers=answers or {}, history=history or []
    )
    return {
        "ok": bool(result.get("ok")),
        "output": result.get("output") or {},
        "unanswered": result.get("unanswered") or [],
        "asked": result.get("asked") or {},
        "brief": result.get("brief") or {},
        "trace": result.get("trace") or [],
        "error": result.get("error"),
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "agent-web/1.0"

    def log_message(self, fmt, *args):  # 安静点：不把每条请求都刷到终端
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, payload: dict) -> None:
        self._send(
            code,
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8",
        )

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 的命名约定
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            if not INDEX_FILE.exists():
                self._json(500, {"ok": False, "error": "web/index.html 丢失，重新生成项目即可"})
                return
            self._send(200, INDEX_FILE.read_bytes(), "text/html; charset=utf-8")
            return
        if path == "/api/health":
            state = runtime_state()
            self._json(200, {"ok": True, "name": AGENT_NAME, **state})
            return
        if path == "/api/meta":
            self._json(
                200,
                {
                    "name": AGENT_NAME,
                    "goal": AGENT_GOAL,
                    "nodes": NODES,
                    "sample": SAMPLE,
                },
            )
            return
        self._json(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path != "/api/chat":
            self._json(404, {"ok": False, "error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY:
            self._json(400, {"ok": False, "error": "请求体为空或过大"})
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            text = str(payload.get("message") or "")
            raw_answers = payload.get("answers") or {}
            answers = {
                str(k): str(v) for k, v in raw_answers.items() if str(v).strip()
            } if isinstance(raw_answers, dict) else {}
            history = _clean_history(payload.get("history"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._json(400, {"ok": False, "error": "请求不是合法 JSON"})
            return
        if not _slots.acquire(blocking=False):
            self._json(429, {"ok": False, "error": "同时处理的对话太多，等几秒再发"})
            return
        try:
            self._json(200, handle_message(text, answers, history))
        except Exception as exc:  # noqa: BLE001 - 把真实异常回给页面，别只显示"失败"
            self._json(500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})
        finally:
            _slots.release()


def _port_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
            return True
        except OSError:
            return False


def serve(host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True) -> int:
    for candidate in range(port, port + 20):
        if _port_free(host, candidate):
            port = candidate
            break
    else:
        print(f"[!] {port}~{port + 19} 都被占用，用 --port 换一个")
        return 2

    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    url = f"http://{host}:{port}"
    print(f"[ok] {AGENT_NAME} 已启动：{url}")
    state = runtime_state()
    if state["error"]:
        print(f"[!] 启动自检失败：{state['error']}")
        print("    先跑 python start.py --check 看缺什么。")
    elif state["missing"]:
        print(f"[!] 还没配 {'、'.join(state['missing'])}：网页能打开，但对话会报错。")
        print("    改 .env 后重启即可，或先跑 python start.py --check 看缺口。")
    print("    Ctrl+C 停止。")
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[ok] 已停止")
    finally:
        httpd.server_close()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    host, port, open_browser = "127.0.0.1", 8765, True
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in ("--port", "-p") and i + 1 < len(args):
            try:
                port = int(args[i + 1])
            except ValueError:
                print(f"[!] 端口不是数字：{args[i + 1]}")
                return 2
            i += 2
            continue
        if arg == "--host" and i + 1 < len(args):
            host = args[i + 1]
            i += 2
            continue
        if arg == "--no-browser":
            open_browser = False
            i += 1
            continue
        if arg in ("-h", "--help"):
            print(__doc__)
            return 0
        i += 1
    return serve(host=host, port=port, open_browser=open_browser)


if __name__ == "__main__":
    raise SystemExit(main())
'''


_WEB_INDEX = r'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>__AGENT_NAME__</title>
<style>
  :root{
    --bg:#f6f7f9; --panel:#ffffff; --ink:#1c1f23; --muted:#6b7280;
    --line:#e5e7eb; --brand:#2563eb; --brand-soft:#eff6ff;
    --ok:#16a34a; --warn:#b45309; --bad:#dc2626;
    --radius:14px;
  }
  @media (prefers-color-scheme: dark){
    :root{
      --bg:#0f1115; --panel:#171a20; --ink:#e8eaed; --muted:#9aa1ab;
      --line:#272b33; --brand:#60a5fa; --brand-soft:#1a2436;
      --ok:#4ade80; --warn:#fbbf24; --bad:#f87171;
    }
  }
  *{box-sizing:border-box}
  html,body{height:100%}
  body{
    margin:0;background:var(--bg);color:var(--ink);
    font:15px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
  }
  .app{max-width:860px;height:100%;margin:0 auto;display:flex;flex-direction:column}
  header{padding:18px 20px 12px;border-bottom:1px solid var(--line);background:var(--bg)}
  .head-row{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
  h1{font-size:17px;margin:0;font-weight:650;letter-spacing:.2px}
  .status{font-size:12px;padding:3px 9px;border-radius:999px;border:1px solid var(--line);color:var(--muted)}
  .status.ok{color:var(--ok);border-color:color-mix(in srgb,var(--ok) 35%,transparent)}
  .status.warn{color:var(--warn);border-color:color-mix(in srgb,var(--warn) 35%,transparent)}
  .status.bad{color:var(--bad);border-color:color-mix(in srgb,var(--bad) 35%,transparent)}
  .goal{color:var(--muted);font-size:13px;margin-top:6px}
  .banner{margin-top:10px;padding:10px 12px;border:1px solid color-mix(in srgb,var(--warn) 35%,transparent);
    background:color-mix(in srgb,var(--warn) 8%,var(--panel));border-radius:10px;font-size:13px;color:var(--ink)}
  .banner code{font-size:12px;background:var(--bg);padding:1px 5px;border-radius:5px}
  main{flex:1;overflow-y:auto;padding:20px;display:flex;flex-direction:column;gap:14px}
  .empty{margin:auto;text-align:center;color:var(--muted);max-width:460px}
  .empty .big{font-size:16px;color:var(--ink);margin-bottom:6px}
  .chips{display:flex;gap:8px;justify-content:center;flex-wrap:wrap;margin-top:14px}
  .chip{border:1px solid var(--line);background:var(--panel);color:var(--ink);border-radius:999px;
    padding:7px 13px;font-size:13px;cursor:pointer;transition:.15s}
  .chip:hover{border-color:var(--brand);color:var(--brand)}
  .msg{display:flex;gap:10px;max-width:100%}
  .msg.user{justify-content:flex-end}
  .bubble{max-width:88%;border-radius:var(--radius);padding:11px 14px;white-space:pre-wrap;word-break:break-word}
  .user .bubble{background:var(--brand);color:#fff;border-bottom-right-radius:5px}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);
    padding:14px 16px;max-width:88%;border-bottom-left-radius:5px}
  .card.error{border-color:color-mix(in srgb,var(--bad) 45%,transparent)}
  .card.error .card-title{color:var(--bad)}
  .card-title{font-size:12px;color:var(--muted);margin-bottom:6px;letter-spacing:.3px}
  .node{padding:10px 0;border-top:1px dashed var(--line)}
  .node:first-of-type{border-top:0;padding-top:0}
  .node-name{font-size:12px;color:var(--brand);margin-bottom:4px;cursor:pointer;user-select:none}
  .node-name:hover{opacity:.75}
  .node.collapsed .node-body{display:none}
  .node.collapsed .node-name::after{content:" · 已收起";color:var(--muted)}
  .node-body{white-space:pre-wrap;word-break:break-word;font-size:14px}
  .node-body strong{font-weight:650}
  .node-body code{font-family:ui-monospace,Consolas,monospace;font-size:12.5px;
    background:var(--bg);padding:1px 4px;border-radius:4px}
  .md-table{border-collapse:collapse;margin:8px 0;font-size:13px;white-space:normal}
  .md-table th,.md-table td{border:1px solid var(--line);padding:5px 9px;text-align:left;vertical-align:top}
  .md-table th{background:var(--bg);font-weight:600}
  .ask{margin-top:12px;border:1px solid color-mix(in srgb,var(--warn) 40%,transparent);
    background:color-mix(in srgb,var(--warn) 7%,var(--panel));border-radius:12px;padding:12px}
  .ask-title{font-size:13px;color:var(--warn);margin-bottom:8px}
  .ask-item{margin-bottom:8px}
  .ask-item label{display:block;font-size:12px;color:var(--muted);margin-bottom:3px}
  .ask-item input{width:100%;padding:7px 9px;border:1px solid var(--line);border-radius:8px;
    background:var(--bg);color:var(--ink);font-size:13px;font-family:inherit}
  .ask-item input:focus{outline:none;border-color:var(--brand)}
  .ask button{margin-top:2px;padding:7px 14px;border:0;border-radius:8px;background:var(--brand);
    color:#fff;font-size:13px;cursor:pointer}
  .ask button:disabled{opacity:.55;cursor:default}
  .note{font-size:12px;color:var(--muted);margin-top:8px}
  .typing{display:inline-flex;gap:4px;align-items:center;color:var(--muted);font-size:13px}
  .dot{width:5px;height:5px;border-radius:50%;background:var(--muted);animation:b 1.2s infinite}
  .dot:nth-child(2){animation-delay:.2s}.dot:nth-child(3){animation-delay:.4s}
  @keyframes b{0%,60%,100%{opacity:.25;transform:translateY(0)}30%{opacity:1;transform:translateY(-3px)}}
  footer{padding:12px 20px 18px;border-top:1px solid var(--line);background:var(--bg)}
  .composer{display:flex;gap:10px;align-items:flex-end}
  textarea{flex:1;resize:none;min-height:46px;max-height:160px;padding:12px 14px;border-radius:12px;
    border:1px solid var(--line);background:var(--panel);color:var(--ink);font:inherit;font-size:14px}
  textarea:focus{outline:none;border-color:var(--brand)}
  .send{height:46px;padding:0 20px;border:0;border-radius:12px;background:var(--brand);color:#fff;
    font-size:14px;cursor:pointer}
  .send:disabled{opacity:.5;cursor:default}
  .hint{font-size:11px;color:var(--muted);margin-top:7px}
</style>
</head>
<body>
<div class="app">
  <header>
    <div class="head-row">
      <h1 id="name">__AGENT_NAME__</h1>
      <span id="status" class="status">正在检查配置…</span>
    </div>
    <div id="goal" class="goal"></div>
    <div id="banner" class="banner" hidden></div>
  </header>
  <main id="feed">
    <div class="empty" id="empty">
      <div class="big">直接说你要做什么</div>
      <div id="empty-hint">缺关键信息时我会先问你，不会闷头猜。</div>
      <div class="chips" id="chips"></div>
    </div>
  </main>
  <footer>
    <div class="composer">
      <textarea id="input" rows="1" placeholder="输入一句话，Enter 发送，Shift+Enter 换行"></textarea>
      <button class="send" id="send">发送</button>
    </div>
    <div class="hint">服务只监听本机；关掉终端窗口即停止。</div>
  </footer>
</div>
<script>
(function () {
  var meta = { name: "__AGENT_NAME__", goal: "", nodes: [], sample: "" };
  var health = { missing: [], error: null };
  var busy = false;
  //: 最近几轮对话。没有它，"那它呢"这种追问每一轮都从零开始
  var conversation = [];
  var MAX_TURNS = 8;

  var feed = document.getElementById("feed");
  var input = document.getElementById("input");
  var sendBtn = document.getElementById("send");
  var statusEl = document.getElementById("status");
  var banner = document.getElementById("banner");
  var empty = document.getElementById("empty");

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  }

  function nodeName(id) {
    for (var i = 0; i < (meta.nodes || []).length; i++) {
      if (meta.nodes[i].id === id) return meta.nodes[i].name || id;
    }
    return id;
  }

  /**
   * 极简富文本：标题（#）、**粗体**、markdown 表格。
   * 模型输出常带这些记号，裸露的 ** 和 |---| 看着很粗糙；
   * 但也不能整段当 HTML 插（那是 XSS 入口），所以逐段建文本节点。
   */
  function inlineRich(node, text, forceBold) {
    String(text == null ? "" : text).split(/(\*\*[^*]+\*\*)/g).forEach(function (part) {
      if (!part) return;
      var bold = !!forceBold;
      if (part.length > 4 && part.slice(0, 2) === "**" && part.slice(-2) === "**") {
        part = part.slice(2, -2);
        bold = true;
      }
      if (bold) {
        var strong = document.createElement("strong");
        strong.textContent = part;
        node.appendChild(strong);
      } else {
        node.appendChild(document.createTextNode(part));
      }
    });
  }

  function isTableRow(line) {
    return /^\s*\|.*\|\s*$/.test(line);
  }

  function isTableSep(line) {
    return /^\s*\|[\s:|-]+\|\s*$/.test(line);
  }

  function splitRow(line) {
    return line.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map(function (cell) {
      return cell.trim();
    });
  }

  function buildTable(header, rows) {
    var table = document.createElement("table");
    table.className = "md-table";
    var thead = document.createElement("thead");
    var headRow = document.createElement("tr");
    header.forEach(function (cell) {
      var th = document.createElement("th");
      inlineRich(th, cell, true);
      headRow.appendChild(th);
    });
    thead.appendChild(headRow);
    table.appendChild(thead);
    var tbody = document.createElement("tbody");
    rows.forEach(function (row) {
      var tr = document.createElement("tr");
      row.forEach(function (cell) {
        var td = document.createElement("td");
        inlineRich(td, cell, false);
        tr.appendChild(td);
      });
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    return table;
  }

  function fillRich(node, text) {
    var lines = String(text == null ? "" : text).split("\n");
    var i = 0;
    while (i < lines.length) {
      // 表格：表头 + 分隔行 + 若干数据行，整块交给 <table>
      if (isTableRow(lines[i]) && i + 1 < lines.length && isTableSep(lines[i + 1])) {
        var header = splitRow(lines[i]);
        var rows = [];
        i += 2;
        while (i < lines.length && isTableRow(lines[i])) {
          rows.push(splitRow(lines[i]));
          i += 1;
        }
        node.appendChild(buildTable(header, rows));
        node.appendChild(document.createTextNode("\n"));
        continue;
      }
      var line = lines[i];
      var heading = line.match(/^#{1,4}\s+(.*)$/);
      inlineRich(node, heading ? heading[1] : line, !!heading);
      if (i < lines.length - 1) node.appendChild(document.createTextNode("\n"));
      i += 1;
    }
  }

  function renderHealth() {
    if (health.error) {
      statusEl.className = "status bad";
      statusEl.textContent = "启动异常";
      showBanner("启动自检失败：" + health.error + "。先运行 python start.py --check 看缺什么。");
      return;
    }
    if ((health.missing || []).length) {
      statusEl.className = "status warn";
      statusEl.textContent = "缺配置";
      showBanner("还缺 " + health.missing.join("、") + "。网页能打开，但对话会失败：在项目根目录的 .env 里填好后重启，详见 DELIVERY.md。");
      return;
    }
    statusEl.className = "status ok";
    statusEl.textContent = "模型已就绪";
  }

  function showBanner(text) {
    banner.hidden = false;
    banner.textContent = text;
  }

  function scrollDown() {
    feed.scrollTop = feed.scrollHeight;
  }

  function hideEmpty() {
    if (empty && empty.parentNode) empty.parentNode.removeChild(empty);
    empty = null;
  }

  function addUser(text, note) {
    hideEmpty();
    conversation.push({ role: "user", content: text });
    trimHistory();
    var wrap = el("div", "msg user");
    var bubble = el("div", "bubble", text);
    if (note) bubble.title = note;
    wrap.appendChild(bubble);
    feed.appendChild(wrap);
    scrollDown();
  }

  function trimHistory() {
    if (conversation.length > MAX_TURNS) {
      conversation = conversation.slice(-MAX_TURNS);
    }
  }

  /** 取最后一个节点的输出当"这轮说了什么"：只留摘要，不把整份 JSON 塞回上下文 */
  function summarize(data) {
    if (!data || !data.ok) return "";
    var output = data.output || {};
    var text = "";
    (meta.nodes || []).forEach(function (n) {
      if (output[n.id] != null && String(output[n.id]).trim()) text = String(output[n.id]);
    });
    if (!text) {
      Object.keys(output).forEach(function (k) {
        if (output[k] != null && String(output[k]).trim()) text = String(output[k]);
      });
    }
    return text.trim().slice(0, 800);
  }

  function addTyping() {
    hideEmpty();
    var wrap = el("div", "msg");
    var card = el("div", "card");
    var t = el("div", "typing");
    t.appendChild(el("span", "dot"));
    t.appendChild(el("span", "dot"));
    t.appendChild(el("span", "dot"));
    t.appendChild(el("span", null, " 正在处理，可能要十几秒"));
    card.appendChild(t);
    wrap.appendChild(card);
    feed.appendChild(wrap);
    scrollDown();
    return wrap;
  }

  function addResult(data, originalText) {
    hideEmpty();
    var wrap = el("div", "msg");
    var card = el("div", "card" + (data && data.ok ? "" : " error"));

    if (!data) {
      card.appendChild(el("div", "card-title", "出错了"));
      card.appendChild(el("div", "node-body", "服务没有返回内容，检查终端里的日志。"));
    } else if (!data.ok) {
      card.appendChild(el("div", "card-title", "没有跑通"));
      card.appendChild(el("div", "node-body", data.error || "未知错误"));
      if ((health.missing || []).length) {
        var tip = el("div", "note", "这通常是模型配置缺失导致的：在 .env 里补上 OPENAI_BASE_URL / OPENAI_API_KEY / OPENAI_MODEL 后重启。");
        card.appendChild(tip);
      }
    } else {
      var output = data.output || {};
      var keys = Object.keys(output);
      if (!keys.length) {
        card.appendChild(el("div", "node-body", "（这次没有产生输出）"));
      }
      var ordered = (meta.nodes || []).map(function (n) { return n.id; }).filter(function (id) {
        return keys.indexOf(id) >= 0;
      });
      keys.forEach(function (id) { if (ordered.indexOf(id) < 0) ordered.push(id); });
      ordered.forEach(function (id) {
        var block = el("div", "node");
        var head = el("div", "node-name", nodeName(id));
        head.title = "点击收起 / 展开";
        head.addEventListener("click", function () { block.classList.toggle("collapsed"); });
        var body = el("div", "node-body");
        fillRich(body, output[id]);
        block.appendChild(head);
        block.appendChild(body);
        card.appendChild(block);
      });
    }

    if (data && data.unanswered && data.unanswered.length) {
      card.appendChild(buildAsk(data.unanswered, originalText));
    }

    wrap.appendChild(card);
    feed.appendChild(wrap);
    scrollDown();

    var reply = summarize(data);
    if (reply) {
      conversation.push({ role: "assistant", content: reply });
      trimHistory();
    }
  }

  function buildAsk(items, originalText) {
    var box = el("div", "ask");
    box.appendChild(el("div", "ask-title", "这些信息没拿到，我按默认假设跑了。补充后可以重跑一遍："));
    var inputs = [];
    items.forEach(function (item) {
      var row = el("div", "ask-item");
      var label = el("label", null, item.question || item.label || item.id);
      var inp = document.createElement("input");
      inp.placeholder = (item.options && item.options.length) ? ("可选：" + item.options.join(" / ")) : "填你的回答（可留空）";
      inp.setAttribute("data-id", item.id);
      row.appendChild(label);
      row.appendChild(inp);
      box.appendChild(row);
      inputs.push(inp);
    });
    var btn = el("button", null, "用这些信息重跑");
    btn.addEventListener("click", function () {
      var answers = {};
      var any = false;
      inputs.forEach(function (inp) {
        var v = (inp.value || "").trim();
        if (v) { answers[inp.getAttribute("data-id")] = v; any = true; }
      });
      if (!any) return;
      btn.disabled = true;
      var summary = Object.keys(answers).map(function (k) { return k + "=" + answers[k]; }).join("，");
      addUser("补充：" + summary);
      submit(originalText, answers);
    });
    box.appendChild(btn);
    return box;
  }

  function setBusy(value) {
    busy = value;
    sendBtn.disabled = value;
    sendBtn.textContent = value ? "处理中" : "发送";
  }

  function post(url, payload) {
    return fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    }).then(function (resp) {
      return resp.text().then(function (text) {
        try { return JSON.parse(text); }
        catch (e) { return { ok: false, error: "服务返回了非 JSON（HTTP " + resp.status + "）：" + text.slice(0, 200) }; }
      });
    });
  }

  function submit(text, answers) {
    if (busy) return;
    setBusy(true);
    var typing = addTyping();
    // 去掉最后一条：它就是这次的 message，带进 history 会被当成两轮
    var history = conversation.slice(0, -1);
    post("/api/chat", { message: text, answers: answers || {}, history: history })
      .then(function (data) { addResult(data, text); })
      .catch(function (err) { addResult({ ok: false, error: String(err) }, text); })
      .then(function () {
        if (typing && typing.parentNode) typing.parentNode.removeChild(typing);
        setBusy(false);
      });
  }

  function sendFromInput() {
    var text = (input.value || "").trim();
    if (!text || busy) return;
    input.value = "";
    input.style.height = "auto";
    addUser(text);
    submit(text, {});
  }

  sendBtn.addEventListener("click", sendFromInput);
  input.addEventListener("keydown", function (e) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      sendFromInput();
    }
  });
  input.addEventListener("input", function () {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 160) + "px";
  });

  fetch("/api/meta").then(function (r) { return r.json(); }).then(function (data) {
    meta = data || meta;
    document.title = meta.name || document.title;
    document.getElementById("name").textContent = meta.name || "Agent";
    document.getElementById("goal").textContent = meta.goal || "";
    document.getElementById("empty-hint").textContent =
      (meta.goal ? meta.goal + "。" : "") + "缺关键信息时我会先问你，不会闷头猜。";
    var chips = document.getElementById("chips");
    var samples = [];
    if (meta.sample) samples.push(meta.sample);
    samples.slice(0, 3).forEach(function (s) {
      var chip = el("button", "chip", s.length > 42 ? s.slice(0, 42) + "…" : s);
      chip.title = s;
      chip.addEventListener("click", function () {
        input.value = s;
        sendFromInput();
      });
      chips.appendChild(chip);
    });
  }).catch(function () {});

  fetch("/api/health").then(function (r) { return r.json(); }).then(function (data) {
    health = data || health;
    renderHealth();
  }).catch(function () {});

  input.focus();
})();
</script>
</body>
</html>
'''


def _server_py(ctx: _Context) -> str:
    """渲染 server.py：把名称、目标、节点写死，避免运行时再去读设计产物。"""
    nodes = [
        {"id": a.id, "name": a.name, "responsibility": a.responsibility}
        for a in ctx.architecture.agents
    ]
    return (
        _SERVER_BODY.replace("__AGENT_NAME__", _py_str(ctx.requirement.agent_name))
        .replace("__AGENT_GOAL__", _py_str(ctx.requirement.goal))
        .replace("__NODES_JSON__", json.dumps(nodes, ensure_ascii=False))
        .replace("__SAMPLE_JSON__", _py_str(ctx.sample))
    )


def _web_index(ctx: _Context) -> str:
    """单文件页面：不依赖 CDN，断网也能开。"""
    import html as _html

    return _WEB_INDEX.replace("__AGENT_NAME__", _html.escape(ctx.requirement.agent_name))


_START_BODY = r'''"""一条命令启动：python start.py

原来要手动敲五步：补 .env、装依赖、自检、跑 Agent、找不到依赖再折腾一遍。
现在合成一条，且每一步失败都直接说原因 —— 硬跑出来的报错比原本的问题更难懂。

    python start.py                 进入对话模式（缺信息会先反问你）
    python start.py "你的输入"       跑一次
    python start.py --web           打开网页版（自动开浏览器，推荐）
    python start.py --check         只做体检：补 .env + 装依赖 + 自检
    python start.py --no-ask "..."  不提问，缺的信息记在返回值的 unanswered 里
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

ENV_FILE = ROOT / ".env"
ENV_EXAMPLE = ROOT / ".env.example"
CHECK_SCRIPT = ROOT / "scripts" / "check_env.py"


def say(msg: str = "") -> None:
    print(msg, flush=True)   # 配合 subprocess 输出，避免日志顺序错乱


def ensure_env() -> bool:
    """没有 .env 就从模板复制一份 —— 让用户只做"填值"这一件事。"""
    if ENV_FILE.exists():
        return True
    if not ENV_EXAMPLE.exists():
        say("[!] 既没有 .env 也没有 .env.example，无法启动")
        return False
    ENV_FILE.write_text(ENV_EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
    say(f"[+] 已由 .env.example 生成 {ENV_FILE.name}，接着按清单填值")
    return True


def ensure_deps() -> bool:
    """缺依赖就装。只装 requirements.txt 里的东西，不做别的魔法。"""
    try:
        import httpx  # noqa: F401
        import yaml  # noqa: F401
    except ModuleNotFoundError as exc:
        say(f"[*] 缺少依赖 {exc.name}，正在安装 requirements.txt（首次会慢一点）")
        code = subprocess.call(
            [sys.executable, "-m", "pip", "install", "-r", str(ROOT / "requirements.txt")]
        )
        if code != 0:
            say("[!] 依赖安装失败。可换成国内镜像重试：")
            say(f"    {sys.executable} -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple")
            return False
        say("[+] 依赖就绪")
    return True


def run_check() -> int:
    """环境自检。缺必需项时返回非 0。"""
    if not CHECK_SCRIPT.exists():
        say("[!] 找不到 scripts/check_env.py")
        return 1
    say("\n---- 环境自检 ----")
    return subprocess.call([sys.executable, str(CHECK_SCRIPT)], cwd=str(ROOT))


def check_model_config() -> bool:
    """按"运行时真实读取路径"再确认一次模型配置。

    scripts/check_env.py 是直接读 .env 文件的，运行时要经过 agent/env.py 载入
    环境变量 —— 两边口径不一致时会出现"自检通过、一跑就报缺少 OPENAI_BASE_URL"。
    这里以运行时为准复查一次，并且放在提问之前：让人答完几个问题才发现没配
    Key，比一开始就拦住更伤。
    """
    try:
        from agent.llm import diagnose, preflight
    except Exception:  # noqa: BLE001 - 导入失败留给后面的真实报错
        return True
    missing = preflight()
    if not missing:
        return True
    say("")
    say(diagnose(missing))
    return False


def prepare() -> int:
    """装依赖 → 自检 → 模型配置复查。

    返回值：0 可以跑；1 配置有缺口（改 .env 就能好）；2 跑不起来（缺依赖/缺 Python）。
    分成两个码是为了让脚本和 CI 能区分"我的问题"和"环境的问题"。
    """
    if not ensure_deps():
        return 2
    if run_check() != 0:
        say("")
        say("[!] 必需项还没配齐，先补齐再跑。")
        return 1
    if not check_model_config():
        return 1
    return 0


def show(result: dict) -> None:
    """结果直接打印，不做花活：这是给人看的调试输出。"""
    say("")
    for node, text in (result.get("output") or {}).items():
        say(f"===== {node} =====")
        say(str(text))
        say("")
    if result.get("asked"):
        # 只报个数等于没说：把"确认成了什么"一起打出来，出问题才知道差在哪
        detail = "、".join(f"{k}={v}" for k, v in (result.get("brief") or {}).items() if v)
        say(f"[已确认 {len(result['asked'])} 项]" + (f" {detail}" if detail else ""))
    if result.get("unanswered"):
        say("[这些没确认，下面的结果按缺省假设做的]")
        for item in result["unanswered"]:
            say(f"  - {item['question']}")
    if not result.get("ok"):
        say(f"[失败] {result.get('error')}")


def run_once(text: str, *, ask: bool = True) -> int:
    from agent.router import handle  # 延迟导入：保证 --check 不需要装依赖也能跑

    result = handle(text, interactive=None if ask else False)
    show(result)
    return 0 if result.get("ok") else 1


def chat_loop() -> int:
    """多轮对话：每轮都先判断缺口，缺了才问。"""
    from agent.router import handle

    say("对话模式：直接说要做什么，空行或 exit 退出。")
    while True:
        try:
            text = input("\n你：").strip()
        except (EOFError, KeyboardInterrupt):
            say("")
            return 0
        if not text or text.lower() in {"exit", "quit", "q"}:
            return 0
        show(handle(text))


def main(argv: list[str]) -> int:
    args = argv[1:]
    check_only = "--check" in args
    ask = "--no-ask" not in args
    web = "--web" in args
    port = 8765
    if "--port" in args:
        idx = args.index("--port")
        if idx + 1 < len(args):
            try:
                port = int(args[idx + 1])
            except ValueError:
                say(f"[!] 端口不是数字：{args[idx + 1]}")
                return 2

    # --port 的值不是"要处理的那句话"，不能混进 text
    words: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--port":
            i += 2
            continue
        if arg not in {"--check", "--no-ask", "--web"}:
            words.append(arg)
        i += 1
    text = " ".join(words).strip()

    if not ensure_env():
        return 2

    if check_only:
        code = prepare()
        say("")
        say("[ok] 体检完成，可以跑了：python start.py --web" if code == 0
            else "[!] 还有缺口，照上面的提示补；补完再跑一次 python start.py --check")
        return code

    code = prepare()
    if code != 0:
        say("")
        say("[!] 先按上面的提示补好 .env，再跑一次：python start.py --check")
        say("    只想跑通流程可以先看 DELIVERY.md 的「可后补项」说明。")
        return code

    if web:
        # 网页模式：同一套后端，换一个人人都会用的入口
        from server import serve

        say("")
        return serve(port=port)

    if text:
        return run_once(text, ask=ask)
    return chat_loop()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
'''


def _start_py(ctx: _Context) -> str:
    return _START_BODY


_RUN_BAT = r'''@echo off
rem 双击即用：启动本机网页并自动打开浏览器（等价于 python start.py --web）
rem 想在终端里对话：python start.py
chcp 65001 >nul
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
  echo [!] python not found. Install Python 3.10+ and check "Add to PATH".
  pause
  exit /b 1
)
python start.py --web %*
if errorlevel 1 pause
'''


def _run_bat(ctx: _Context) -> str:
    return _RUN_BAT


_RUN_SH = r'''#!/usr/bin/env bash
# 一键启动（macOS / Linux）：./run.sh 打开网页；./run.sh "你的输入" 终端跑一次
set -e
cd "$(dirname "$0")"
if [ "$#" -eq 0 ]; then
  exec python3 start.py --web
fi
exec python3 start.py "$@"
'''


def _run_sh(ctx: _Context) -> str:
    return _RUN_SH



def _service_name(ctx: _Context) -> str:
    """compose 的服务名：只能是 [a-z0-9-]，中文项目名要有个兜底。"""
    raw = (ctx.project or "").strip().lower()
    slug = "".join(ch if (ch.isalnum() and ch.isascii()) else "-" for ch in raw)
    slug = "-".join(part for part in slug.split("-") if part)
    return (slug or "agent")[:32].strip("-") or "agent"


def _dockerfile(ctx: _Context) -> str:
    """部署用的镜像。刻意不含 .env：密钥不该被烤进镜像层。"""
    return f"""# 由 Agent Builder 生成：把本项目打成可运行镜像
# 构建：docker build -t {_service_name(ctx)} .
# 运行：docker run --rm --env-file .env {_service_name(ctx)}
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \\
    PYTHONUNBUFFERED=1 \\
    PIP_NO_CACHE_DIR=1 \\
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# 先只拷依赖清单：改业务代码时这一层还能命中缓存
COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY . .

# .env 在 .dockerignore 里被排除，配置必须运行时注入
EXPOSE 8765
# 默认起网页服务；容器里没人看浏览器，所以 --no-browser
ENTRYPOINT ["python"]
CMD ["server.py", "--host", "0.0.0.0", "--no-browser"]
"""


def _dockerignore(ctx: _Context) -> str:
    return """# 不进入镜像的东西
.env
.env.local
__pycache__/
*.pyc
.pytest_cache/
.git/
data/
*.zip
"""


def _docker_compose(ctx: _Context) -> str:
    name = _service_name(ctx)
    return f"""# 由 Agent Builder 生成：一条命令起起来
#   docker compose up --build        起网页版（默认，浏览器开 http://localhost:8765）
#   docker compose run --rm {name} python start.py --check   只体检
#   docker compose run --rm {name} python start.py "你的输入"  终端跑一次
# 密钥从 .env 注入（compose 会读同目录的 .env），不要写进镜像
services:
  {name}:
    build: .
    image: {name}:local
    env_file:
      - .env
    ports:
      - "8765:8765"
    stdin_open: true
    tty: true
    # 默认起网页；要换成自己的服务化入口，改这一行即可
    command: ["python", "server.py", "--host", "0.0.0.0", "--no-browser"]
"""

def _test_clarify(ctx: _Context) -> str:
    if not ctx.slots:
        return '''"""这个需求没有识别出必填缺口，clarify 应保持空转。"""

from __future__ import annotations

from agent.clarify import SLOTS, collect


def test_no_slots_means_no_questions() -> None:
    brief, asked, unanswered = collect("随便说点什么", interactive=True, ask_fn=lambda s: "x")
    assert SLOTS == ()
    assert asked == [] and unanswered == [] and brief == {}
'''
    sample = ctx.sample
    has = sorted(set(ctx.domain.sample_has)) if ctx.domain else []
    missing = sorted(set(ctx.domain.sample_missing)) if ctx.domain else []
    return f'''"""提问逻辑的回归测试 —— 对应"用户只说了半句话"这种最常见的情况。

用真实的输入样本，不联网、不需要 Key。
"""

from __future__ import annotations

from agent.clarify import MAX_QUESTIONS, collect, extract, pending, render

SAMPLE = {json.dumps(sample, ensure_ascii=False)}


def test_reads_what_user_already_said() -> None:
    """already given 的不再问，否则会变成盘问。"""
    found = extract(SAMPLE)
    for slot_id in {json.dumps(has, ensure_ascii=False)}:
        assert slot_id in found, f"{{slot_id}} 明明说了却没读出来"


def test_missing_info_is_detected() -> None:
    ids = {{s.id for s in pending(SAMPLE)}}
    for slot_id in {json.dumps(missing, ensure_ascii=False)}:
        assert slot_id in ids, f"{{slot_id}} 没说却没被列入待确认"


def test_non_interactive_never_blocks() -> None:
    """pytest / 定时脚本里绝不能等 input()。"""
    brief, asked, unanswered = collect(SAMPLE, interactive=False)
    assert asked == []
    assert brief == extract(SAMPLE)
    assert len(unanswered) == len(pending(SAMPLE))


def test_answers_are_recorded() -> None:
    brief, asked, unanswered = collect(
        SAMPLE, interactive=True, ask_fn=lambda slot: "已确认"
    )
    assert len(asked) == min(len(pending(SAMPLE)), MAX_QUESTIONS)
    for item in asked:
        assert brief[item["id"]] == "已确认"
    assert unanswered == [] or len(asked) == MAX_QUESTIONS


def test_render_carries_answers_into_input() -> None:
    brief, _, _ = collect(SAMPLE, interactive=True, ask_fn=lambda slot: "已确认")
    merged = render(SAMPLE, brief)
    assert SAMPLE in merged
    assert "已确认的关键信息" in merged
'''


_IPYNB_CELL_HEAD = (
    "# {name} —— 交互式试跑\n\n"
    "这个 notebook 用 `agent.router.handle()`，**它会先检查你这句话缺什么信息**：\n\n"
    "- 原话里已经有的（天数、预算、偏好…）不再问；\n"
    "- 缺的会弹输入框问你（最多 {max_q} 个），答完才生成；\n"
    "- 在 CI / 定时脚本里不会卡住，缺的信息会列成 `unanswered`。\n\n"
    "跑第 2 个单元格就会看到提问。"
)


def _notebook_ipynb(ctx: _Context) -> str:
    sample = ctx.sample
    sample_lit = json.dumps(sample, ensure_ascii=False)
    intro = (
        f"# {ctx.requirement.agent_name} —— 交互式试跑\n\n"
        "这个 notebook 调 `agent.router.handle()`，**它会先检查你这句话缺什么信息**：\n\n"
        "- 原话里已经有的（天数、预算、偏好…）不再问；\n"
        f"- 缺的会弹输入框问你（最多 {MAX_QUESTIONS} 个），答完才开始生成；\n"
        "- 非交互环境（pytest / 定时脚本）不会卡住，缺的信息列在 `unanswered` 里。\n\n"
        "跑第 2 个单元格就会看到提问。"
    )
    code_import = (
        "from agent.clarify import pending\n"
        "from agent.router import handle\n\n"
        "print('这个 Agent 会先确认这些信息（原话里已给出的会自动跳过）：')\n"
        "for slot in pending(SAMPLE):\n"
        "    print(f'  - {slot.label}：{slot.question}')"
    )
    code_run = (
        "import json\n\n"
        "SAMPLE = " + sample_lit + "\n"
        "result = handle(SAMPLE)   # 缺信息时会在这里弹输入框\n"
        "print('ok:', result['ok'])"
    )
    code_show = (
        "for node, text in (result['output'] or {}).items():\n"
        "    print(f'\\n===== {node} =====')\n"
        "    print(text)\n\n"
        "print('\\n已确认：', json.dumps(result['brief'], ensure_ascii=False))"
    )
    code_noask = (
        "# 不提问模式：CI 里跑回归用这个，缺的信息会原样返回\n"
        "dry = handle(SAMPLE, interactive=False)\n"
        "for item in dry['unanswered']:\n"
        "    print('待确认：', item['question'])"
    )
    cells = [
        {"cell_type": "markdown", "metadata": {}, "source": intro.splitlines(True)},
        {"cell_type": "code", "execution_count": None, "metadata": {},
         "outputs": [], "source": code_import.splitlines(True)},
        {"cell_type": "code", "execution_count": None, "metadata": {},
         "outputs": [], "source": code_run.splitlines(True)},
        {"cell_type": "code", "execution_count": None, "metadata": {},
         "outputs": [], "source": code_show.splitlines(True)},
        {"cell_type": "code", "execution_count": None, "metadata": {},
         "outputs": [], "source": code_noask.splitlines(True)},
    ]
    notebook = {
        "cells": cells,
        "metadata": {
            "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
            "language_info": {"name": "python"},
        },
        "nbformat": 4,
        "nbformat_minor": 5,
    }
    return json.dumps(notebook, ensure_ascii=False, indent=1)


def render_tree(files: list[GeneratedFile]) -> str:
    """把文件列表渲染成树形文本（前端与 README 都用它）。"""
    tree: dict[str, object] = {}
    for f in files:
        parts = f.path.split("/")
        cursor = tree
        for part in parts[:-1]:
            cursor = cursor.setdefault(part + "/", {})  # type: ignore[assignment]
        cursor[parts[-1]] = None  # type: ignore[index]

    lines: list[str] = []

    def walk(node: dict, prefix: str = "") -> None:
        keys = sorted(node, key=lambda k: (not k.endswith("/"), k))
        for i, key in enumerate(keys):
            last = i == len(keys) - 1
            lines.append(prefix + ("└── " if last else "├── ") + key)
            child = node[key]
            if isinstance(child, dict):
                walk(child, prefix + ("    " if last else "│   "))

    walk(tree)
    return "\n".join(lines)
