/** 与后端 app/schemas 一一对应的类型定义。改动后端 Schema 时同步这里。 */

export type ArchitectureType =
  | 'single_agent'
  | 'multi_agent'
  | 'workflow'
  | 'hybrid'

export type ComponentType =
  | 'prompt'
  | 'skill'
  | 'tool'
  | 'mcp'
  | 'harness'
  | 'memory'
  | 'rag'

export type ComplexityLevel = 'simple' | 'moderate' | 'complex'

export interface ClarifyingQuestion {
  id: string
  question: string
  why: string
  options: string[]
  blocking: boolean
}

export interface RequirementSpec {
  agent_name: string
  goal: string
  target_users: string[]
  inputs: string[]
  outputs: string[]
  features: string[]
  constraints: string[]
  quality_requirements: string[]
  external_dependencies: string[]
  acceptance_criteria: string[]
  architecture_hint: ArchitectureType | 'undecided'
  complexity_level: ComplexityLevel
  clarifying_questions: ClarifyingQuestion[]
  confidence: number
}

export interface AnalyzeResult {
  spec: RequirementSpec
  provider: string
  model: string
  latency_ms: number
  attempts: number
  warnings: string[]
}

export interface AgentNode {
  id: string
  name: string
  responsibility: string
  inputs: string[]
  outputs: string[]
  tools: string[]
  skills: string[]
}

export interface WorkflowEdge {
  source: string
  target: string
  condition: string | null
  description: string | null
}

export interface ComponentNeed {
  type: ComponentType
  name: string
  purpose: string
  depends_on: string[]
}

export interface ArchitectureSpec {
  architecture_type: ArchitectureType
  reasoning_summary: string
  decision_factors: string[]
  agents: AgentNode[]
  workflow_edges: WorkflowEdge[]
  required_components: ComponentNeed[]
  optional_components: ComponentNeed[]
  risks: string[]
  complexity_level: ComplexityLevel
  confidence: number
}

export interface PreJudgement {
  suggested_type: ArchitectureType
  complexity_score: number
  signals: string[]
}

export interface ValidationIssue {
  level: 'error' | 'warning' | 'info'
  code: string
  message: string
  target: string | null
}

export interface ValidationReport {
  passed: boolean
  issues: ValidationIssue[]
}

export interface PlanResult {
  spec: ArchitectureSpec
  pre_judgement: PreJudgement
  validation: ValidationReport
  provider: string
  model: string
  latency_ms: number
  attempts: number
  warnings: string[]
}

export interface ComponentSpec {
  type: ComponentType
  name: string
  purpose: string
  priority: 'required' | 'optional'
  owner_agents: string[]
  interface: Record<string, unknown>
  depends_on: string[]
  test_requirements: string[]
}

export interface ComponentPlan {
  components: ComponentSpec[]
  summary: string
  notes: string[]
}

export interface ComponentPlanResult {
  plan: ComponentPlan
  validation: ValidationReport
  provider: string
  model: string
  latency_ms: number
  attempts: number
  warnings: string[]
}

export interface ApiResponse<T> {
  code: number
  message: string
  data: T | null
}

// ---------- 异步流水线 ----------

export type JobStatus =
  | 'idle'
  | 'queued'
  | 'running'
  | 'waiting_input'
  | 'waiting_approval'
  | 'done'
  | 'failed'
  | 'cancelled'

export type StepStatus = 'pending' | 'running' | 'done' | 'failed' | 'skipped'

export interface StepState {
  key: string
  label: string
  status: StepStatus
  duration: number | null
  attempts: number
  error: string | null
}

export interface JobEvent {
  seq: number
  ts: number
  step: string | null
  level: 'info' | 'progress' | 'warn' | 'error'
  message: string
}

export interface JobSnapshot {
  job_id: string
  status: JobStatus
  raw_text: string
  created_at: number
  updated_at: number
  finished_at: number | null
  elapsed: number
  steps: StepState[]
  events: JobEvent[]
  last_seq: number
  pending_questions: ClarifyingQuestion[]
  /** 架构审批：status=waiting_approval 时的方案摘要 */
  pending_approval: ApprovalGate | null
  approval: { stage: string; at: number; note: string } | null
  answers: Record<string, string>
  error: string | null
  result: {
    requirement?: AnalyzeResult
    architecture?: PlanResult
    components?: ComponentPlanResult
    codegen?: CodegenResult
    /** 自动/手动保存为项目后回填 */
    project_id?: string
  }
}

export interface RunPipelineResult {
  job_id: string
  status: JobStatus
  poll_url: string
}

// ---------- 代码生成 ----------

export interface GeneratedFile {
  path: string
  content: string
  kind: 'python' | 'markdown' | 'yaml' | 'text' | 'json'
  lines: number
  source: string
}

export interface CodegenStats {
  files: number
  lines: number
  python_files: number
  tests: number
}

export interface CoverItem {
  component: string
  component_type: string
  target: string
}

export interface CodegenResult {
  project_name: string
  files: GeneratedFile[]
  tree: string
  stats: CodegenStats
  coverage: CoverItem[]
  validation: ValidationReport
  warnings: string[]
  notes: string[]
  generator: string
  /** 交付清单：跑起来还要准备什么、去哪注册、怎么启动 */
  delivery?: DeliveryPlan | null
}

// ---------- 项目（持久化）----------

export type ProjectStatus = 'active' | 'archived'

export interface ProjectFileMeta {
  path: string
  kind: string
  lines: number
  bytes: number
}

export interface ProjectSummary {
  id: string
  name: string
  description: string
  status: ProjectStatus
  tags: string[]
  job_id: string | null
  raw_text: string
  agent_name: string | null
  architecture_type: string | null
  complexity_level: string | null
  created_at: number
  updated_at: number
  files: number
  lines: number
  tests: number
  validation_passed: boolean
  /** 上次真跑 pytest 的结论；null 表示还没跑过（注意和 tests 区分：那是生成的用例数） */
  tests_ok: boolean | null
  tests_total: number | null
  tests_failed: number | null
  tests_ran_at: number | null
}

export interface ProjectDetail extends ProjectSummary {
  tree: string
  file_list: ProjectFileMeta[]
  warnings: string[]
  notes: string[]
  has_requirement: boolean
  has_architecture: boolean
  has_components: boolean
  has_delivery: boolean
  has_tests: boolean
}

export interface ProjectFileContent {
  path: string
  content: string
  kind: string
  lines: number
  editable: boolean
}

// ---------- 交付清单 ----------

export type ResourceKind =
  | 'llm'
  | 'api_key'
  | 'credential'
  | 'knowledge_doc'
  | 'data_file'
  | 'account'
  | 'infra'

export interface ResourceItem {
  key: string
  name: string
  kind: ResourceKind
  required: boolean
  why: string
  env_vars: string[]
  target: string
  signup_url: string | null
  steps: string[]
  cost_hint: string
  fallback: string
  example: string
  env_defaults: Record<string, string>
}

export interface StartupStep {
  order: number
  title: string
  command: string | null
  detail: string
}

export interface DeliveryPlan {
  summary: string
  resources: ResourceItem[]
  startup_steps: StartupStep[]
  blockers: string[]
  check_command: string
}

export interface ThinkingOption {
  value: string
  label: string
  description: string
}

export interface LLMSettings {
  provider: string
  model: string
  /** true 表示未配置 Key、当前走离线假模型 */
  mock: boolean
  thinking: string
  thinking_label: string
  options: ThinkingOption[]
  temperature: number
  /** 真正会发到 /chat/completions 的附加字段 */
  request_body: Record<string, unknown>
  persisted: boolean
  note: string
}

/** 一条测试用例的执行结果（来自 pytest 的 junit xml） */
export interface TestCaseResult {
  name: string
  status: 'passed' | 'failed' | 'error' | 'skipped'
  duration: number
  message: string
}

/** 一次「真跑 pytest」的完整结果，落盘在 design/tests.json */
export interface TestReport {
  project_id: string
  project_name: string
  ran_at: number
  ok: boolean
  timed_out: boolean
  exit_code: number
  duration_s: number
  command: string
  targets: string[]
  total: number
  passed: number
  failed: number
  error: number
  skipped: number
  cases: TestCaseResult[]
  output_tail: string
  notes: string[]
}

/** Debug Agent 的一轮尝试 */
export interface HealAttempt {
  attempt: number
  diagnosis: string
  confidence: 'high' | 'medium' | 'low' | ''
  edits: { path: string; reason: string; lines: number }[]
  rejected: string[]
  result: TestBrief
  error?: string
  note?: string
}

export interface TestBrief {
  ok: boolean
  total: number
  passed: number
  failed: number
  error: number
  duration_s: number
  failing_cases: string[]
}

/** 一次自动修复的完整过程，落盘在 design/debug.json */
export interface HealReport {
  project_id: string
  ran_at: number
  elapsed_s: number
  max_attempts: number
  initial: TestBrief
  final: TestBrief
  attempts: HealAttempt[]
  changed_files: string[]
  reverted: boolean
  needs_human: boolean
  note: string
}


/** 停下等人确认的架构摘要（后端 pipeline._architecture_summary） */
export interface ApprovalGate {
  stage: string
  summary: {
    architecture_type: string
    complexity_level: string
    confidence: number
    reasoning_summary: string
    agents: { id: string; name: string; responsibility: string; tools: string[]; outputs: string[] }[]
    edges: { from: string; to: string; condition: string | null }[]
    required_components: string[]
    optional_components: string[]
    risks: string[]
  }
}

/** 执行隔离状态（容器 / 本机子进程） */
export interface SandboxStatus {
  driver: 'docker' | 'local' | 'none'
  isolated: boolean
  reason: string
  image?: string
  image_built?: boolean
  docker?: {
    docker_binary: string | null
    docker_server: string | null
    docker_error?: string
  }
  limits: Record<string, string | number>
}

/** 一个版本快照 */
export interface VersionInfo {
  version: string
  created_at: number
  reason: string
  note: string
  files: number
  lines: number
}

export interface VersionDiff {
  version: string
  identical: boolean
  added: number
  removed: number
  modified: number
  changes: { path: string; status: 'added' | 'removed' | 'modified'; lines_delta: number }[]
}

export interface RestoreResult {
  id: string
  restored_from: string
  restored: string[]
  kept: string[]
  backup_version: string
  files: number
  lines: number
  note: string
}

export interface PreviewStatus {
  project_id: string
  running: boolean
  url: string | null
  port: number | null
  started_at: number | null
  log_tail: string
  reused?: boolean
}
