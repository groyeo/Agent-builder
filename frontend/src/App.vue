<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { api } from './api'
import type {
  AnalyzeResult,
  ApprovalGate as ApprovalGateData,
  CodegenResult,
  ComponentPlanResult,
  JobEvent,
  JobStatus,
  LLMSettings,
  PlanResult,
  StepState,
  ValidationIssue,
} from './types'
import ArchitectureGraph from './components/ArchitectureGraph.vue'
import ApprovalGate from './components/ApprovalGate.vue'
import ClarifyPanel from './components/ClarifyPanel.vue'
import CodeViewer from './components/CodeViewer.vue'
import ComponentList from './components/ComponentList.vue'
import DeliveryPlanView from './components/DeliveryPlanView.vue'
import ProjectSpace from './components/ProjectSpace.vue'
import RequirementCard from './components/RequirementCard.vue'
import RunTimeline from './components/RunTimeline.vue'
import ValidationList from './components/ValidationList.vue'

type Tab =
  | 'requirement'
  | 'architecture'
  | 'components'
  | 'delivery'
  | 'code'
  | 'validation'
type View = 'build' | 'projects'

const EXAMPLES = [
  {
    label: '客服工单分流',
    text: '做一个客服工单智能分流 Agent，读取我们工单系统里的新工单，判断属于技术/账单/物流哪一类，并自动回复常见问题，处理不了的转人工。',
  },
  {
    label: '销售数据分析',
    text: '做一个销售数据分析 Agent，用户上传 Excel 销售明细，需要清洗数据、按月份和区域统计趋势、找出异常订单并生成图表报告。',
  },
  {
    label: '招聘简历初筛',
    text: '做一个简历初筛 Agent，读取候选人 PDF 简历和岗位 JD，判断匹配度并给出理由，匹配度低的自动归档并回复邮件。',
  },
  {
    label: '合同条款审核',
    text: '做一个合同条款审核 Agent，读取上传的合同 PDF，对照公司制度知识库检查风险条款，输出风险清单和修改建议。',
  },
]

const STORAGE_KEY = 'agent-builder:last-job'
const POLL_MS = 900

const rawText = ref('')
const errorMsg = ref('')
const activeTab = ref<Tab>('requirement')
// 支持 #projects 深链：刷新/分享都能直接落在项目库
const view = ref<View>(
  window.location.hash.replace('#', '').startsWith('projects') ? 'projects' : 'build',
)

function setView(next: View) {
  view.value = next
  const target = next === 'projects' ? '#projects' : ''
  if (window.location.hash !== target) {
    window.history.replaceState(null, '', target || window.location.pathname)
  }
}

/** 当前任务落成的项目 id（后端自动保存后回填） */
const projectId = ref('')
const savingProject = ref(false)
const projectHint = ref('')

const jobId = ref('')
const jobStatus = ref<JobStatus>('idle')
const liveSteps = ref<StepState[]>([])
const events = ref<JobEvent[]>([])
const pendingQuestions = ref<AnalyzeResult['spec']['clarifying_questions']>([])
const submitting = ref(false)
const elapsed = ref(0)
const startedAt = ref(0)

const analyzeResult = ref<AnalyzeResult | null>(null)
const planResult = ref<PlanResult | null>(null)
const componentResult = ref<ComponentPlanResult | null>(null)
const codegenResult = ref<CodegenResult | null>(null)

let since = 0
let pollTimer: number | null = null
let tickTimer: number | null = null
let polling = false

const running = computed(
  () => jobStatus.value === 'queued' || jobStatus.value === 'running',
)
const waiting = computed(() => jobStatus.value === 'waiting_input')
/** 停在架构审批：这时既不是 running 也不是 waiting_input，要单独一层 */
const awaitingApproval = computed(() => jobStatus.value === 'waiting_approval')
const approvalGate = ref<ApprovalGateData | null>(null)
const approving = ref(false)
/** 「生成前先确认架构」开关，存本地，下次进来还记得 */
const REQUIRE_APPROVAL_KEY = 'ab_require_approval'
const requireApproval = ref(window.localStorage.getItem(REQUIRE_APPROVAL_KEY) === '1')

function toggleRequireApproval() {
  requireApproval.value = !requireApproval.value
  window.localStorage.setItem(REQUIRE_APPROVAL_KEY, requireApproval.value ? '1' : '0')
}
const hasResult = computed(() => !!analyzeResult.value)

const allIssues = computed<ValidationIssue[]>(() => [
  ...(planResult.value?.validation.issues ?? []),
  ...(componentResult.value?.validation.issues ?? []),
  ...(codegenResult.value?.validation.issues ?? []),
])
const errorCount = computed(() => allIssues.value.filter((i) => i.level === 'error').length)
const warnCount = computed(() => allIssues.value.filter((i) => i.level === 'warning').length)

const allWarnings = computed(() => [
  ...(analyzeResult.value?.warnings ?? []),
  ...(planResult.value?.warnings ?? []),
  ...(componentResult.value?.warnings ?? []),
  ...(codegenResult.value?.warnings ?? []),
])

const requiredNeeds = computed(
  () => (codegenResult.value?.delivery?.resources ?? []).filter((r) => r.required).length,
)

const providerLabel = computed(() => {
  const r = analyzeResult.value
  if (r) return `${r.provider} · ${r.model}`
  if (llm.value) return `${llm.value.provider} · ${llm.value.model}`
  return ''
})

/* ---------------- 思考强度 ----------------
   存在后端（data/llm_settings.json），刷新/重启都记得；切换后下一次构建生效。 */
const llm = ref<LLMSettings | null>(null)
const savingThinking = ref(false)
const thinkingHint = ref('')
let hintTimer: number | null = null

function flashHint(text: string) {
  thinkingHint.value = text
  if (hintTimer !== null) window.clearTimeout(hintTimer)
  hintTimer = window.setTimeout(() => (thinkingHint.value = ''), 4000)
}

async function loadLlmSettings() {
  try {
    llm.value = await api.getLlmSettings()
  } catch {
    // 设置读不到不该挡住主流程：顶栏少一个开关而已
    llm.value = null
  }
}

async function setThinking(level: string) {
  if (!llm.value || llm.value.thinking === level || savingThinking.value) return
  savingThinking.value = true
  try {
    llm.value = await api.updateLlmSettings({ thinking: level })
    flashHint(`思考强度：${llm.value.thinking_label}，下次构建生效`)
  } catch (err) {
    flashHint(err instanceof Error ? err.message : String(err))
  } finally {
    savingThinking.value = false
  }
}

function thinkingTip(): string {
  if (!llm.value) return ''
  const cur = llm.value.options.find((o) => o.value === llm.value!.thinking)
  const base = cur ? cur.description : ''
  const body = llm.value.request_body
  return `${base}\n\n实际请求字段：${JSON.stringify(body)}`
}

function clearTimers() {
  if (pollTimer !== null) window.clearTimeout(pollTimer)
  if (tickTimer !== null) window.clearInterval(tickTimer)
  pollTimer = null
  tickTimer = null
}

function startTicking() {
  if (tickTimer !== null) window.clearInterval(tickTimer)
  tickTimer = window.setInterval(() => {
    if (running.value) elapsed.value = (Date.now() - startedAt.value) / 1000
  }, 200)
}

function applySnapshot(d: {
  status: JobStatus
  steps: StepState[]
  events: JobEvent[]
  last_seq: number
  elapsed: number
  pending_questions: AnalyzeResult['spec']['clarifying_questions']
  pending_approval: ApprovalGateData | null
  error: string | null
  result: {
    requirement?: AnalyzeResult
    architecture?: PlanResult
    components?: ComponentPlanResult
    codegen?: CodegenResult
    project_id?: string
  }
}) {
  jobStatus.value = d.status
  liveSteps.value = d.steps
  if (d.events.length) events.value.push(...d.events)
  since = d.last_seq
  pendingQuestions.value = d.pending_questions
  if (d.status === 'done') {
    elapsed.value = d.elapsed
    analyzeResult.value = d.result.requirement ?? null
    planResult.value = d.result.architecture ?? null
    componentResult.value = d.result.components ?? null
    codegenResult.value = d.result.codegen ?? null
    projectId.value = d.result.project_id ?? ''
    if (activeTab.value === 'requirement' && planResult.value) {
      // 有代码就优先展示代码，否则退到架构
      // 有交付清单就落在这里：用户最先要知道的是"我还差什么"
      const needs = codegenResult.value?.delivery?.resources.filter((r) => r.required) ?? []
      activeTab.value = codegenResult.value
        ? needs.length
          ? 'delivery'
          : 'code'
        : 'architecture'
    }
  } else if (d.status === 'waiting_input') {
    // 暂停时已产出结构化需求，先让用户看到"系统理解成了什么"
    analyzeResult.value = d.result.requirement ?? null
  } else if (d.status === 'waiting_approval') {
    // 审批态：需求和架构都已产出，直接摊在架构页上让人判断
    analyzeResult.value = d.result.requirement ?? null
    planResult.value = d.result.architecture ?? null
    componentResult.value = null
    codegenResult.value = null
    approvalGate.value = d.pending_approval
    activeTab.value = 'architecture'
  } else if (d.status === 'failed') {
    errorMsg.value = d.error ?? '构建失败'
  } else if (d.status === 'cancelled') {
    errorMsg.value = '任务已取消'
  }
}

function schedulePoll() {
  if (!polling) return
  pollTimer = window.setTimeout(pollOnce, POLL_MS)
}

async function pollOnce() {
  if (!polling || !jobId.value) return
  try {
    const d = await api.getJob(jobId.value, since, true)
    applySnapshot(d)
    if (d.status === 'queued' || d.status === 'running') {
      schedulePoll()
    } else {
      polling = false
      clearTimers()
      if (d.status === 'done') window.localStorage.removeItem(STORAGE_KEY)
      // 等确认 / 等回答都要留着任务号，刷新页面回来还能接上
      if (d.status !== 'waiting_input' && d.status !== 'waiting_approval') {
        window.localStorage.removeItem(STORAGE_KEY)
      }
    }
  } catch (err) {
    polling = false
    clearTimers()
    errorMsg.value = err instanceof Error ? err.message : String(err)
  }
}

function beginPolling() {
  polling = true
  clearTimers()
  startTicking()
  schedulePoll()
}

async function run() {
  const text = rawText.value.trim()
  if (text.length < 5) {
    errorMsg.value = '请先描述你的需求（至少 5 个字）'
    return
  }

  errorMsg.value = ''
  analyzeResult.value = null
  planResult.value = null
  componentResult.value = null
  codegenResult.value = null
  projectId.value = ''
  projectHint.value = ''
  liveSteps.value = []
  events.value = []
  pendingQuestions.value = []
  approvalGate.value = null
  since = 0
  elapsed.value = 0
  jobStatus.value = 'queued'
  startedAt.value = Date.now()

  try {
    const res = await api.runPipeline(text, {
      requireArchitectureApproval: requireApproval.value,
    })
    jobId.value = res.job_id
    window.localStorage.setItem(STORAGE_KEY, res.job_id)
    beginPolling()
  } catch (err) {
    jobStatus.value = 'failed'
    errorMsg.value = err instanceof Error ? err.message : String(err)
  }
}

async function submitAnswers(answers: Record<string, string>) {
  if (!jobId.value) return
  submitting.value = true
  errorMsg.value = ''
  try {
    await api.answerJob(jobId.value, answers, false)
    pendingQuestions.value = []
    jobStatus.value = 'running'
    beginPolling()
  } catch (err) {
    errorMsg.value = err instanceof Error ? err.message : String(err)
  } finally {
    submitting.value = false
  }
}

async function skipClarify() {
  if (!jobId.value) return
  submitting.value = true
  errorMsg.value = ''
  try {
    await api.answerJob(jobId.value, {}, true)
    pendingQuestions.value = []
    jobStatus.value = 'running'
    beginPolling()
  } catch (err) {
    errorMsg.value = err instanceof Error ? err.message : String(err)
  } finally {
    submitting.value = false
  }
}

async function approveArchitecture(note: string) {
  if (!jobId.value || approving.value) return
  approving.value = true
  errorMsg.value = ''
  try {
    await api.approveJob(jobId.value, note)
    approvalGate.value = null
    jobStatus.value = 'running'
    beginPolling()
  } catch (err) {
    errorMsg.value = err instanceof Error ? err.message : String(err)
  } finally {
    approving.value = false
  }
}

async function rejectArchitecture(note: string) {
  if (!jobId.value || approving.value) return
  approving.value = true
  errorMsg.value = ''
  try {
    await api.rejectJob(jobId.value, note)
    approvalGate.value = null
    const d = await api.getJob(jobId.value, since, true)
    applySnapshot(d)
    polling = false
    clearTimers()
  } catch (err) {
    errorMsg.value = err instanceof Error ? err.message : String(err)
  } finally {
    approving.value = false
  }
}

async function cancel() {
  if (!jobId.value) return
  try {
    await api.cancelJob(jobId.value)
    if (waiting.value) {
      // 暂停态本来没有轮询在跑，取消后手动拉一次让界面落到"已取消"
      const d = await api.getJob(jobId.value, since, true)
      applySnapshot(d)
      polling = false
      clearTimers()
    }
  } catch (err) {
    errorMsg.value = err instanceof Error ? err.message : String(err)
  }
}

function useExample(text: string) {
  rawText.value = text
}

/** 自动保存失败或配置关闭时的手动兜底 */
async function saveProjectManually() {
  if (!jobId.value || savingProject.value) return
  savingProject.value = true
  projectHint.value = ''
  try {
    const p = await api.createProject(jobId.value)
    projectId.value = p.id
    projectHint.value = `已保存为项目「${p.name}」`
  } catch (err) {
    projectHint.value = err instanceof Error ? err.message : String(err)
  } finally {
    savingProject.value = false
  }
}

function openProjects() {
  setView('projects')
}

/** `#build/<job_id>` 深链：任务已落库，换个浏览器/发给别人也能打开同一次构建 */
function jobFromHash(): string {
  const m = window.location.hash.match(/^#build\/([0-9a-z]+)$/i)
  return m ? m[1] : ''
}

/** 刷新页面后接回上一次未完成的任务（任务已落库，刷新与重启都不丢） */
async function restore() {
  const fromHash = jobFromHash()
  const saved = fromHash || window.localStorage.getItem(STORAGE_KEY)
  if (!saved) return
  jobId.value = saved
  try {
    const d = await api.getJob(saved, 0, true)
    startedAt.value = Date.now() - d.elapsed * 1000
    applySnapshot(d)
    if (d.status === 'queued' || d.status === 'running') beginPolling()
    else if (d.status === 'waiting_input' || d.status === 'waiting_approval') {
      window.localStorage.setItem(STORAGE_KEY, saved)
    }
    else window.localStorage.removeItem(STORAGE_KEY)
  } catch {
    window.localStorage.removeItem(STORAGE_KEY)
    jobId.value = ''
  }
}

onMounted(() => {
  restore()
  loadLlmSettings()
})
onUnmounted(() => {
  clearTimers()
  if (hintTimer !== null) window.clearTimeout(hintTimer)
})

const ARCH_LABEL: Record<string, string> = {
  single_agent: 'Single Agent',
  multi_agent: 'Multi-Agent',
  workflow: 'Workflow',
  hybrid: 'Hybrid',
}
</script>

<template>
  <div class="app-shell">
    <header class="topbar">
      <div class="brand">
        <span class="brand-name">Agent Builder</span>
        <span class="brand-sub">需求 → 架构 → 组件</span>
      </div>

      <nav class="view-switch">
        <button :class="{ on: view === 'build' }" @click="setView('build')">构建</button>
        <button :class="{ on: view === 'projects' }" @click="setView('projects')">
          项目库
          <span v-if="projectId" class="dot-mark"></span>
        </button>
      </nav>

      <div class="steps">
        <div
          v-for="s in liveSteps"
          :key="s.key"
          class="step"
          :class="{ active: s.status === 'running', done: s.status === 'done' }"
        >
          <span class="dot"></span>
          {{ s.label }}
          <span
            v-if="s.status === 'running' || s.duration"
            style="font-variant-numeric: tabular-nums"
          >
            {{ s.status === 'running' ? '…' : `${s.duration}s` }}
          </span>
        </div>
      </div>

      <div class="topbar-right">
        <span v-if="thinkingHint" class="think-hint">{{ thinkingHint }}</span>
        <div v-if="llm" class="think-switch" :title="thinkingTip()">
          <span class="think-label">思考</span>
          <button
            v-for="o in llm.options"
            :key="o.value"
            :class="{ on: llm.thinking === o.value }"
            :disabled="savingThinking"
            :title="o.description"
            @click="setThinking(o.value)"
          >
            {{ o.label }}
          </button>
        </div>
        <span v-if="providerLabel" class="model-chip" :title="llm?.mock ? '未配置 Key，当前走离线 Mock' : ''">
          {{ providerLabel }}{{ llm?.mock ? ' · mock' : '' }}
        </span>
        <span v-else class="model-chip">未连接</span>
      </div>
    </header>

    <ProjectSpace v-if="view === 'projects'" @open-build="setView('build')" />

    <div v-else class="workspace">
      <!-- 左：输入 -->
      <aside class="input-pane">
        <div class="pane-title">
          需求描述
          <span class="hint">自然语言，越具体越好</span>
        </div>

        <textarea
          v-model="rawText"
          class="req-input"
          placeholder="例：做一个客服工单分流 Agent，读取工单系统的工单，判断类别，自动回复常见问题，处理不了的转人工……"
        ></textarea>

        <div>
          <div class="pane-title" style="margin-bottom: 8px; font-weight: 400">
            <span class="hint">试试示例</span>
          </div>
          <div class="example-list">
            <span
              v-for="ex in EXAMPLES"
              :key="ex.label"
              class="example-chip"
              @click="useExample(ex.text)"
            >
              {{ ex.label }}
            </span>
          </div>
        </div>

        <label class="require-approval" :class="{ on: requireApproval }">
          <input type="checkbox" :checked="requireApproval" @change="toggleRequireApproval" />
          <span>生成前先让我确认架构</span>
          <span class="hint">多一次判断，少一次白跑（约省 60% token）</span>
        </label>

        <div class="btn-row">
          <button class="run-btn" :disabled="running || waiting || awaitingApproval" @click="run">
            {{ running ? '构建中…' : '开始构建' }}
          </button>
          <button
            v-if="running || waiting || awaitingApproval"
            class="cancel-btn"
            title="当前模型调用无法中断，会在该步骤结束后停止"
            @click="cancel"
          >
            取消
          </button>
        </div>

        <div v-if="errorMsg" class="error-box">{{ errorMsg }}</div>

        <RunTimeline
          v-if="jobId && liveSteps.length"
          :steps="liveSteps"
          :events="events"
          :running="running"
          :elapsed="elapsed"
          :started-at="startedAt"
        />

        <div v-else class="pane-hint">
          提交后立即返回任务号，下面会实时显示每一步在做什么、重试了几次、耗时多少。
          耗时长的步骤是模型在做 reasoning，不是卡住了。
        </div>
      </aside>

      <!-- 右：输出 -->
      <main class="output-pane">
        <!-- 审批优先：架构已产出、代码还没生成，先把判断权交回用户 -->
        <ApprovalGate
          v-if="awaitingApproval && approvalGate"
          :gate="approvalGate"
          :submitting="approving"
          @approve="approveArchitecture"
          @reject="rejectArchitecture"
        />

        <!-- 澄清优先：这时还没有最终结果，但已能展示结构化需求 -->
        <template v-else-if="waiting">
          <ClarifyPanel
            :questions="pendingQuestions"
            :submitting="submitting"
            @submit="submitAnswers"
            @skip="skipClarify"
          />
          <div v-if="analyzeResult" class="tab-body" style="padding: 0">
            <div class="card-sub" style="margin: 14px 0 8px">
              暂停前已产出的结构化需求（回答后会被重新生成，不是最终结果）：
            </div>
            <RequirementCard :spec="analyzeResult.spec" />
          </div>
        </template>

        <div v-else-if="!hasResult" class="empty-state">
          <div class="big">
            {{ running ? '正在构建…' : '还没有生成结果' }}
          </div>
          <div class="small">
            <template v-if="running">
              左侧是实时进度：规则预判、模型调用次数、结构校验结果都会逐条出现。
              第一步通常会在 10 秒左右停顿一次，等你确认关键问题。
            </template>
            <template v-else>
              在左侧描述你想要的 Agent，系统会先做需求结构化与澄清，
              再规划架构（规则预判 + 模型评估 + 图结构校验），最后细化到可生成的组件清单。
            </template>
          </div>
        </div>

        <template v-else>
          <div class="tab-bar">
            <div
              class="tab"
              :class="{ active: activeTab === 'requirement' }"
              @click="activeTab = 'requirement'"
            >
              结构化需求
            </div>
            <div
              class="tab"
              :class="{ active: activeTab === 'architecture' }"
              @click="activeTab = 'architecture'"
            >
              架构方案
              <span v-if="planResult" class="badge">{{ planResult.spec.agents.length }}</span>
            </div>
            <div
              class="tab"
              :class="{ active: activeTab === 'components' }"
              @click="activeTab = 'components'"
            >
              组件清单
              <span v-if="componentResult" class="badge">
                {{ componentResult.plan.components.length }}
              </span>
            </div>
            <div
              class="tab"
              :class="{ active: activeTab === 'delivery' }"
              @click="activeTab = 'delivery'"
            >
              交付清单
              <span v-if="requiredNeeds" class="badge warn">{{ requiredNeeds }}</span>
            </div>
            <div
              class="tab"
              :class="{ active: activeTab === 'code' }"
              @click="activeTab = 'code'"
            >
              生成代码
              <span v-if="codegenResult" class="badge">{{ codegenResult.stats.files }}</span>
            </div>
            <div
              class="tab"
              :class="{ active: activeTab === 'validation' }"
              @click="activeTab = 'validation'"
            >
              校验报告
              <span v-if="errorCount" class="badge err">{{ errorCount }}</span>
              <span v-else-if="warnCount" class="badge warn">{{ warnCount }}</span>
            </div>
          </div>

          <div class="tab-body">
            <!-- 结构化需求 -->
            <template v-if="activeTab === 'requirement' && analyzeResult">
              <div v-if="analyzeResult.warnings.length" class="warn-strip">
                <span>⚠</span>
                <span>{{ analyzeResult.warnings.join(' ') }}</span>
              </div>
              <RequirementCard :spec="analyzeResult.spec" />
              <div class="card" style="margin-bottom: 0">
                <div class="card-sub">
                  元信息：model {{ analyzeResult.model }} · 耗时
                  {{ (analyzeResult.latency_ms / 1000).toFixed(1) }}s ·
                  {{ analyzeResult.attempts }} 次调用
                </div>
              </div>
            </template>

            <!-- 架构方案 -->
            <template v-else-if="activeTab === 'architecture' && planResult">
              <div class="arch-meta">
                <span class="meta-pill">
                  架构类型 <strong>{{ ARCH_LABEL[planResult.spec.architecture_type] }}</strong>
                </span>
                <span class="meta-pill">
                  复杂度 <strong>{{ planResult.spec.complexity_level }}</strong>
                </span>
                <span class="meta-pill pre">
                  规则预判 {{ ARCH_LABEL[planResult.pre_judgement.suggested_type] }}
                  · {{ planResult.pre_judgement.complexity_score }}/10 分
                </span>
                <span class="meta-pill">
                  置信度
                  <strong>{{ Math.round(planResult.spec.confidence * 100) }}%</strong>
                </span>
              </div>

              <div
                v-if="planResult.pre_judgement.suggested_type !== planResult.spec.architecture_type"
                class="warn-strip"
              >
                <span>⇄</span>
                <span>
                  模型结论与规则预判不一致（规则倾向
                  {{ ARCH_LABEL[planResult.pre_judgement.suggested_type] }}）。
                  模型的反对理由见下方"决策依据"。
                </span>
              </div>

              <ArchitectureGraph :spec="planResult.spec" />

              <div class="card" style="margin-top: 12px">
                <div class="card-head">
                  <div class="card-title">选择理由</div>
                  <span class="tag blue">{{
                    ARCH_LABEL[planResult.spec.architecture_type]
                  }}</span>
                </div>
                <div style="font-size: 13px; line-height: 1.85; color: var(--text)">
                  {{ planResult.spec.reasoning_summary }}
                </div>
              </div>

              <div class="card">
                <div class="card-head">
                  <div class="card-title">决策依据</div>
                  <span class="card-sub">规则预判与模型判断的因子</span>
                </div>
                <div
                  v-for="f in [
                    ...planResult.pre_judgement.signals,
                    ...planResult.spec.decision_factors,
                  ]"
                  :key="f"
                  class="list-item"
                >
                  <span class="bullet">·</span><span>{{ f }}</span>
                </div>
              </div>

              <div v-if="planResult.spec.risks.length" class="card">
                <div class="card-head">
                  <div class="card-title">风险</div>
                </div>
                <div v-for="r in planResult.spec.risks" :key="r" class="list-item">
                  <span class="bullet" style="color: var(--amber)">!</span>
                  <span>{{ r }}</span>
                </div>
              </div>
            </template>

            <!-- 组件清单 -->
            <template v-else-if="activeTab === 'components' && componentResult">
              <div v-if="componentResult.warnings.length" class="warn-strip">
                <span>⚠</span>
                <span>{{ componentResult.warnings.join(' ') }}</span>
              </div>
              <ComponentList :plan="componentResult.plan" />
            </template>

            <!-- 交付清单：要准备什么、去哪注册、怎么启动 -->
            <template v-else-if="activeTab === 'delivery'">
              <DeliveryPlanView v-if="codegenResult?.delivery" :delivery="codegenResult.delivery" />
              <div v-else class="card">
                <div class="card-sub">
                  这一版生成结果没有交付清单（旧任务或早期版本）。重新构建一次即可看到。
                </div>
              </div>
            </template>

            <!-- 生成的代码 -->
            <template v-else-if="activeTab === 'code' && codegenResult">
              <div v-if="projectId" class="saved-strip">
                <span class="saved-tick">✓</span>
                <span>
                  已保存到项目库：<code>{{ projectId }}</code>
                  <span class="saved-sub">重启服务也不会丢</span>
                </span>
                <button class="ghost-btn" @click="openProjects">打开项目库</button>
              </div>
              <div v-else class="warn-strip">
                <span>⚠</span>
                <span>
                  本次结果还没保存成项目{{ projectHint ? `（${projectHint}）` : '' }}，
                  只在内存里，服务重启就没了。
                </span>
                <button class="ghost-btn" :disabled="savingProject" @click="saveProjectManually">
                  {{ savingProject ? '保存中…' : '保存为项目' }}
                </button>
              </div>
              <CodeViewer :result="codegenResult" :job-id="jobId" />
            </template>

            <!-- 校验报告 -->
            <template v-else-if="activeTab === 'validation'">
              <ValidationList
                v-if="planResult"
                :report="planResult.validation"
                title="架构结构校验"
              />
              <ValidationList
                v-if="componentResult"
                :report="componentResult.validation"
                title="组件方案校验"
              />
              <div v-if="allWarnings.length" class="card">
                <div class="card-head"><div class="card-title">重试与降级记录</div></div>
                <div v-for="w in allWarnings" :key="w" class="list-item">
                  <span class="bullet" style="color: var(--amber)">↻</span>
                  <span>{{ w }}</span>
                </div>
              </div>
            </template>

            <!-- 任务中断（取消/失败）时该 Tab 还没有内容 -->
            <template v-else>
              <div class="card">
                <div class="card-sub">
                  这一部分还没有产出。任务状态：{{ jobStatus }}。
                  如果任务被取消，重新点"开始构建"即可。
                </div>
              </div>
            </template>
          </div>
        </template>
      </main>
    </div>
  </div>
</template>
