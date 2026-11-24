# Agent Builder

**用一句自然语言，生成一个能直接打开网页使用的 AI Agent。**

输入「做一个客服工单智能分流的 Agent，按技术/账单/退款分类，情绪激动时转人工」，
输出一套可运行的 Python 工程：分层架构、各组件的提示词与编排、可执行的测试、启动脚本、
一份说明"跑起来还缺什么"的交付清单，以及一个打开就能对话的网页。

技术栈：Python 3.11 · FastAPI · Pydantic v2 · Vue 3 · TypeScript · SQLite（任务与项目元数据）

---

## 它解决什么问题

想做 Agent 的人，卡点通常不在想法，而在想法和能跑的系统之间那道工程坎：
搭脚手架、配密钥、装依赖、写提示词、补测试——一个人上手要几个小时，
而且中途任何一步失败，报错信息都比原本的问题更难懂。

而"让模型直接写代码"这条路看着最省事，实际最不稳：
同样的需求跑两次，文件结构不一样；模型还会悄悄编出不存在的 API，
写出一堆"看起来对、跑起来炸"的代码。

Agent Builder 的选择是：**把确定性工作交给模板，只把判断题交给模型。**

| | 让模型写全部 | 本项目 |
|---|---|---|
| 代码生成 | 几万 token，结构每次不同 | 模板渲染，**0 token**，结构固定 |
| 校验 | 靠人肉 review | 三层自动校验，不过不往下传 |
| 成本 | 每次生成都烧钱 | 只有需求/架构/组件三步调模型 |
| 失败 | 生成完才发现不对 | 生成物自带测试，真跑，跑挂能自修 |

---

## 一条需求进去，出来什么

```
一句话自然语言需求
      │  缺信息？先反问（最多 4 个），答完才动手，不猜
      ▼
① 需求分析      产出 RequirementSpec
                守门：字段完整性 + 阻塞性澄清
      ▼
② 架构规划      产出 ArchitectureSpec
                守门：规则预判 + 模型评估 + 图结构
                （可选）人工审批，确认后才继续
      ▼
③ 组件设计      产出 ComponentPlan
                守门：接口契约 / 依赖成环 / 过度设计
      ▼
④ 代码生成      完整工程 + 自带测试 + 网页 + 交付清单
                （模板渲染）守门：AST / YAML / 组件覆盖度
      ▼
项目库 · 打包下载 · 起网页 · 版本回滚
```

产出物不是一段代码片段，是一个**项目**：落在磁盘上、能列表能编辑、能打包下载、
能一键起网页、能看版本历史、改坏了能回滚。

---

## 架构

### 分层

```
┌ 前端工作台   Vue 3 + Vite
│     需求输入 / 事件流 / 架构图 / 组件清单 / 项目库
│
├ 接口层   app/api/routes/
│     只做参数校验与响应封装，不含业务逻辑
│
├ 编排层   engine/pipeline.py
│     后台任务 · 进度事件 · 澄清与审批暂停
│     状态：job_store（内存）+ job_repo（SQLite）
│
└ 编排层只调两个部分
      ├ agents/                会思考：需求分析 / 架构规划 / 组件设计
      └ engine/ + validators/  不调模型：预判 / 模板 / 落盘 / 沙箱 / 校验
```

一条贯穿全局的约定：**模型的输出永远只是候选，必须过校验才能进入下一步。**
校验不通过时把错误反馈给模型重试，重试仍不过就如实失败，不降级放行。

### 四段流水线

| 阶段 | 做什么 | 产出 | 怎么守 |
|---|---|---|---|
| ① 需求分析 | 把一句话拆成可执行规格：目标、输入输出、功能点、约束、验收标准；读出阻塞性缺口并提问 | `RequirementSpec` | 字段完整性 + 澄清问题阻塞判断 |
| ② 架构规划 | 决定用单 Agent / 流水线 / 多 Agent 协作，划节点分工与边界 | `ArchitectureSpec` | 规则预判 + 模型评估 + 图结构校验 |
| ③ 组件设计 | 把架构里的类型级占位细化成可直接生成的组件：接口契约、依赖、使用者、测试要求 | `ComponentPlan` | 契约字段 + 依赖成环 + 过度设计检测 |
| ④ 代码生成 | 模板渲染成完整工程（**不调模型**） | 可运行工程 | AST / YAML / 组件覆盖度自检 |

### 架构决策的三层机制

架构是整条链路里最贵的错误——拆错了，后面 1000 多行代码和所有 token 都白花。
所以这里不信任任何单一来源：

1. **规则预判**（`engine/prejudge.py`）：按功能点数量、外部依赖数、专业角色关键词、
   流程确定性词汇打分，给出倾向和依据。不调模型，结果可复现。
2. **模型评估**（`agents/architecture_planner.py`）：可以反对规则结论，
   但必须在 `decision_factors` 里写清理由，否则视为无效意见。
3. **图结构校验**（`validators/architecture_validator.py`）：节点数与架构类型是否自洽、
   边是否指向不存在的节点、有无入口、必需组件是否齐全、是否过度拆分、有没有死循环。

三者不一致时，前端会把分歧摊开给人看，而不是悄悄取一个。

---

## 三个关键设计决定

### 1. 代码不让模型写

模型写 10 个文件要几万 token，且结构每次都不一样；而结构化输出的东西，模板比模型更稳。

`engine/code_generator.py` 把组件清单渲染成完整工程，**0 token**。
提示词正文由组件设计里的 `interface.role` 加需求验收标准拼成，不是模型现编。

代价是灵活性：模板只覆盖 7 类组件（prompt / harness / tool / mcp / rag / memory / skills）。
但这个取舍换来的是——同一份需求，跑多少次产出的结构都一样，可测试、可 diff、可回滚。

两条硬规矩：

- **不许假成功**：tool / mcp / rag 一律生成 `raise NotImplementedError` 的桩。
  宁可在接入时立刻炸，也不要返回一个看起来对的结果。
- **生成后自检**：`validators/codegen_validator.py` 检查语法、YAML 可解析、
  每个节点有提示词与编排函数、每个组件有落地文件、没有空文件、启动脚本齐全。
  任一项不过就是 `passed=false`，不往下传。

### 2. 在架构处停下来问人

```
需求分析 → 架构规划 → [等待审批] → 组件设计 → 代码生成
                        ↑ 确认后从这里续跑，前两步不重跑
```

为什么停在这里：**架构是最便宜的纠错点**。等组件和上千行代码都生成完才发现"拆错了"，
代价大一个数量级，而且用户已经付过 token。

两个容易忽略的细节：

- 确认后**不重跑**需求与架构。模型每次输出都会飘，重跑等于把用户刚点头的方案换掉。
- 驳回会带原因把任务标为失败，**不留半成品**——半成品比没有更糟，用户不知道能不能用。

### 3. 缺信息先问，不猜

`agent/clarify.py` 是生成物的入口契约：能从这句话读到的就不再问，读不到的才问，最多 4 个。

```
你：帮我规划成都 3 天旅行，预算 3000 元，喜欢吃货和熊猫
? 你从哪座城市出发？            ← 缺
? 大概什么时候去、什么时候回？    ← 缺
（天数 / 预算 / 偏好原话里有，不再问）
```

槽位来自领域画像（旅行 / 报表 / 客服 / 审核 / 招聘）加正则提取，0 token。

三种环境三种行为：终端和 Jupyter 直接弹问题；网页版渲染成补答表单；
CI 与定时任务不阻塞，把缺口放进 `unanswered` 返回——**绝不偷偷猜**。

设计期的问题（"渠道用对话还是 API"）不进运行时提问——那在构建时就该问完，运行中再问是噪音。

---

## 快速开始

```bash
# 0. 环境：Python 3.11（conda 环境名 agent-builder）+ Node 18+
git clone <repo-url> && cd AgentBuilder

# 1. 后端
cd backend && cp .env.example .env     # 填 LLM_API_KEY（任意 OpenAI 兼容服务）
pip install -r requirements.txt && cd ..

# 2. 前端
cd frontend && npm install && cd ..

# 3. 一条命令起前后端
python start.py        # 后端 8000 + 前端 5173，Ctrl+C 一起停
```

Windows 双击 `start.bat` 等价。打开 http://localhost:5173 就是工作台。

**不填 Key 也能跑**：会自动降级为离线 Mock 模型，响应里 `provider=mock` 并给出警告——
链路可验证，但不代表真实生成质量。

---

## 生成物长什么样

每次生成的工程大致是这样（文件数随组件类型浮动）：

```
README.md  DELIVERY.md  requirements.txt  .env.example  config.yaml
start.py  run.bat  run.sh  notebook.ipynb  server.py  conftest.py
Dockerfile  docker-compose.yml  .dockerignore
agent/   llm.py  prompts.py  tools.py  harness.py  router.py  clarify.py
         [mcp_client.py | memory.py | rag.py | skills.py]   按需生成
tests/   test_prompts.py  test_tools.py  test_router.py
web/     index.html          单文件页面，不引 CDN，断网可开
knowledge/  data/            语料与样例数据目录（空目录，自检会提醒）
```

四件事值得单独说：

**一条命令启动。** `python start.py` 把原来五步（补 `.env` → 装依赖 → 自检 → 跑 → 再排查）
合成一条。`--check` 只体检；`--web` 起网页。每一步失败都直接说原因，不往后硬跑。

**每个 Agent 自带网页。** `server.py` 只用标准库（`ThreadingHTTPServer`），零新增依赖，
只监听 `127.0.0.1`；页面单文件、不依赖外网。缺 Key 时顶部直接显示缺哪个变量、去哪填，
而不是白屏或 500。缺信息时不当场阻塞，而是返回问题清单，页面渲染成补答表单。

**交付清单。** `DELIVERY.md` 逐项回答：为什么需要、去哪注册、怎么拿、填到哪个变量、
大概多少钱、没配会怎样。`scripts/check_env.py` 把清单变成可执行的一步，缺什么报什么、非 0 退出。
清单来自服务注册表加规则推断（有 `rag` 组件就必须准备知识库），**不让模型现编 URL**——
编出来的链接看起来对、点进去 404，比没有更浪费时间。

**自带可执行测试。** 用注入的假 chat 函数，不需要网络和 Key。
所以"能跑"不是形容词，是可验证的：平台的测试会把生成物写到临时目录并真的执行 `pytest`。

---

## 工程实现要点

| 能力 | 做法 | 为什么这么做 |
|---|---|---|
| **异步流水线** | 提交任务立即返回 `job_id`，后台跑，前端按 `since=<last_seq>` 拉增量事件 | 完整链路 90~100 秒，同步请求既容易超时，用户也只能干等 |
| **进度事件** | ContextVar 通道，Agent 内部调 `emit()` | 不用把回调一路透传到每个函数签名 |
| **任务持久化** | 任务与事件写穿 SQLite（WAL），启动时 `restore()` 装回最近任务 | 重启后旧任务仍可查；跑一半的标 failed 并说明原因，而不是悄悄消失 |
| **真跑测试** | 拿项目 `files/` 当工作区起 pytest 子进程，junit xml 解析 | 扒 stdout 不稳；逐条给出通过/失败/原因/耗时 |
| **自动修复** | Debug Agent：读失败证据 → 提最小补丁 → 重跑，默认 2 轮 | 规则写在代码里：禁改 `tests/`（改断言等于作弊）、禁改 `.env`、补丁必须过 `ast.parse`、越修越糟整轮回滚、修不好标 `needs_human` |
| **执行隔离** | 有 docker 就在容器里跑（`--network=none` + 限额 + `--cap-drop=ALL`）；没有就退回子进程并**如实标注** | 界面显示 `isolation.driver`，不假装有隔离 |
| **版本历史** | 生成 / 更新代码前 / 自动修复前 / 回滚前自动快照，最多 10 个 | 回滚只覆盖不回删，`.env` 与 `knowledge/` 不动，回滚前先备份（所以回滚本身可撤销） |
| **彻底删除** | 直接删项目目录，校验路径穿越 | 用户说删就是要删干净，不是丢进回收站还要自己清 |

### 为什么进度事件用 ContextVar

Agent 函数是同步的，经 `asyncio.to_thread` 进工作线程时上下文会自动复制。
如果改成显式传 callback，从路由到最内层的每个函数签名都要多一个参数，
而这个参数和业务逻辑毫无关系——它只是观测手段，不该污染接口。

### 数据落在哪

```
backend/data/projects/<project_id>/
├── project.json     管理元信息：名字 / 描述 / 标签 / 状态 / 来源 job / 测试统计
├── codegen.json     生成元数据：文件清单、组件覆盖度、校验报告（不含正文）
├── files/<path>     真实工程文件 —— 浏览、编辑、打包都以这里为准
├── design/*.json    需求 / 架构 / 组件三份设计产物，回答"代码为啥长这样"
└── history/vNNN/    版本快照
```

正文只存一份：`files/` 是唯一事实来源，`codegen.json` 剔掉了 content，
详情页的文件数与行数按磁盘重算——用户改完文件不会和元信息里的旧数字打架。

路径必须收口：`safe_rel_path()` 拒绝绝对路径、`..`、盘符和反斜杠穿越，
否则 `?path=../../.env` 就能读到项目之外的密钥文件。

---

## 成本实测

同一条客服工单需求、完整三步链路（`backend/scripts/bench_models.py`）：

| 配置 | 耗时 | tokens | 说明 |
|---|---|---|---|
| DeepSeek-V4.1-Flash（**开**思考） | 146.8s | 47276 | 原始配置 |
| DeepSeek-V4.1-Flash（**关**思考） | 26.7s | **11313** | 当前默认，−76% token / −82% 时间 |
| Qwen3.8-Flash-Next（关思考） | 44.6s | 7799 | 单次更省，但复跑涨到 23525 且多 2 次重试，不稳定 |
| Qwen3.5-27B（关思考） | 20.8s | — | 架构 JSON 连续 2 次不合规，不可用 |
| Qwen3.5-35B-A3B / Qwen3.8-27B | 48.5s / 45.2s | 5026 / 5398 | 单步对比时输出 token 反而更多 |

结论：**token 大头是思考过程，不是模型选择。** `LLM_DISABLE_THINKING=true` 是主要杠杆，
换模型只是次要变量，且小模型在严格 JSON 上不稳定。

代码生成这一步 0 token，所以整套流程的成本基本等于"三步设计"的成本。

模型层是适配器（OpenAI 兼容协议 + Mock），换服务方只改配置；
思考强度做成四档运行时开关，不用改业务代码。

---

## 项目结构

```
backend/app/
├── api/routes/      health / requirements / architecture / components / codegen
│                    pipeline / projects / preview / sandbox / settings
├── agents/          会思考的三段：requirement_analyzer / architecture_planner / component_designer
├── engine/          不给模型做的部分
│                    prejudge（规则预判）· pipeline（编排）· code_generator（模板渲染）
│                    project_store（落盘/快照/回滚）· job_store + job_repo（任务与持久化）
│                    service_catalog + delivery_planner（交付清单）· clarify_plan（该问什么）
│                    sandbox（执行隔离）· preview（把目标 Agent 起成本机网页）· debug_agent（自动修复）
├── validators/      architecture_validator / component_validator / codegen_validator
├── schemas/         各阶段的数据契约（Pydantic v2）
└── core/            config + llm（base / openai_compatible / mock / factory）+ progress

frontend/src/
├── App.vue          工作台编排：提交任务 + 轮询 + 澄清/审批中断 + 结果面板
├── api/ types.ts    接口封装与后端 Schema 对齐的类型
└── components/      ArchitectureGraph / RequirementCard / ComponentList / ValidationList
                     RunTimeline / ClarifyPanel / CodeViewer / ProjectSpace / DeliveryPlanView
```

---

## 测试

```bash
cd backend && python -m pytest -q      # 164 passed
```

覆盖的不只是工具函数，还包括几条容易出事的路径：

- 生成物的**契约**：模板产物必须能 `ast.parse`、必须有测试、必须能真跑通；
- 预览进程的**状态机**：起 → 复用 → 停 → 再起，进程死了要能发现；
- 删除、回滚、快照的**边界**：路径穿越、`.env` 不被回滚覆盖、回滚本身可撤销；
- 自动修复的**禁令**：改测试要拒收、改 `.env` 要拒收、越修越糟要回滚。

### 已知边界（写在这里，而不是藏起来）

- 任务存储是单机 SQLite + 单 worker，**不支持多实例部署**。换 Postgres 只需换 `job_repo.py` 的实现。
- **容器隔离这条路径没在真机上端到端验证过**（开发机没装 docker）。命令构造、
  超时清理、资源限额都有单测覆盖，但"真的在容器里跑通"要等有 docker 的机器。
- 项目库是文件系统不是数据库：没有事务、没有并发写保护。单用户本地工具够用，
  多用户时要换成 DB + 对象存储。
- 模板覆盖 7 类组件，超出范围的架构会被校验器拒绝，而不是勉强生成一个四不像。

---

## 接口

<details>
<summary>展开完整 API 表（40+ 个端点）</summary>

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` `/api/health` | 健康检查 |
| POST | `/api/requirements/analyze` | 自然语言需求 → 结构化 RequirementSpec |
| POST | `/api/architecture/plan` | RequirementSpec → 架构方案 + 预判依据 + 校验报告 |
| POST | `/api/components/design` | 需求 + 架构 → 组件清单 + 校验报告 |
| GET | `/api/components/registry` | 组件类型注册表（7 类组件的适用边界） |
| POST | `/api/codegen/generate` | 需求+架构+组件 → 多文件工程（毫秒级、不调模型） |
| POST | `/api/codegen/zip` | 直接下载工程 zip |
| POST | `/api/pipeline/run` | 提交构建任务，立即返回 job_id（202） |
| GET | `/api/pipeline/{job_id}?since=N` | 查询状态与增量事件 |
| POST | `/api/pipeline/{job_id}/answers` | 提交澄清回答并续跑 |
| POST | `/api/pipeline/{job_id}/cancel` | 取消任务 |
| POST | `/api/pipeline/{job}/approve` | 确认架构，从组件设计续跑 |
| POST | `/api/pipeline/{job}/reject` | 驳回架构（本次构建结束） |
| GET | `/api/pipeline/{job_id}/zip` | 下载该任务生成的工程 zip |
| GET | `/api/pipeline/{job_id}/download` | 下载完整结果 JSON（含事件流） |
| POST | `/api/projects` | 把任务结果保存为项目（同一 job 幂等） |
| GET | `/api/projects?include_archived=` | 项目列表，按更新时间倒序 |
| GET | `/api/projects/{id}` | 项目详情 + 文件清单 |
| PATCH | `/api/projects/{id}` | 改名 / 改描述 / 归档 / 打标签 |
| DELETE | `/api/projects/{id}` | 彻底删除（删目录，不可恢复） |
| POST | `/api/projects/{id}/refresh` | 用最新模板重新生成（0 token，保留 `.env` / `knowledge/` / `data/`） |
| POST | `/api/projects/{id}/tests?timeout=` | 真跑 pytest，返回逐条用例结果 |
| GET | `/api/projects/{id}/tests` | 读上次测试结果 |
| POST | `/api/projects/{id}/heal?max_attempts=` | Debug Agent 自动修复 |
| GET | `/api/projects/{id}/heal` | 读上次修复过程 |
| GET | `/api/projects/{id}/history` | 版本快照列表（最多 10 个） |
| GET | `/api/projects/{id}/history/{v}/diff` | 版本与当前文件的逐文件差异 |
| POST | `/api/projects/{id}/history/{v}/restore` | 回滚 |
| POST | `/api/projects/{id}/snapshot` | 手动打一个版本 |
| POST | `/api/projects/{id}/preview` | 起目标 Agent 的网页，返回可打开地址 |
| GET | `/api/projects/{id}/preview` | 网页运行状态（端口、启动时间、日志尾部） |
| DELETE | `/api/projects/{id}/preview` | 停掉本机网页进程 |
| GET | `/api/projects/{id}/files?path=` | 读文件正文 |
| PUT | `/api/projects/{id}/files?path=` | 写回文件正文（在线编辑） |
| GET | `/api/projects/{id}/design/{stage}` | 读设计产物：requirement / architecture / components / delivery |
| GET | `/api/projects/{id}/zip` | 从磁盘打包下载（不重新生成） |
| GET | `/api/sandbox` | 当前执行隔离档位与原因 |
| PUT | `/api/sandbox` | 切换 `auto` / `docker` / `local` |
| POST | `/api/sandbox/warm` | 预构建沙箱镜像（首次较慢） |
| GET | `/api/settings/llm` | 当前模型配置（不含密钥） |
| PUT | `/api/settings/llm` | 运行时切换模型与思考强度 |

</details>

---

## 许可

采用 [PolyForm Noncommercial License 1.0.0](https://polyformproject.org/licenses/noncommercial/1.0.0)。

**可以**：阅读、学习、修改，用于个人项目或非商业场景（教学、研究、公益与政府机构）。

**不可以**：任何商业用途——包括销售本软件或其衍生品、集成进付费产品或服务、用于对外提供收费服务。

如需商业授权，请通过仓库 Issue 联系作者。
