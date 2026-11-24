import type {
  AnalyzeResult,
  ApiResponse,
  ArchitectureSpec,
  ComponentPlanResult,
  HealReport,
  JobSnapshot,
  LLMSettings,
  PlanResult,
  PreviewStatus,
  ProjectDetail,
  ProjectFileContent,
  ProjectSummary,
  RequirementSpec,
  RestoreResult,
  RunPipelineResult,
  SandboxStatus,
  TestReport,
  VersionDiff,
  VersionInfo,
} from '../types'

const BASE = ''

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...init,
  })

  const text = await resp.text()
  let body: unknown = null
  try {
    body = text ? JSON.parse(text) : null
  } catch {
    // 后端正常都回 JSON；纯文本多半是反向代理/框架兜底的 500，把原文带出来才查得动
    throw new Error(
      `服务端返回了非 JSON 响应（HTTP ${resp.status}）：${text.slice(0, 160) || '空响应'}`,
    )
  }

  if (!resp.ok) {
    const detail =
      (body as { detail?: string } | null)?.detail ?? `HTTP ${resp.status}`
    throw new Error(detail)
  }

  const parsed = body as ApiResponse<T>
  if (parsed.code !== 0) {
    throw new Error(parsed.message || '接口返回业务错误')
  }
  return parsed.data as T
}

export const api = {
  analyzeRequirement(raw_text: string, answers: Record<string, string> = {}) {
    return request<AnalyzeResult>('/api/requirements/analyze', {
      method: 'POST',
      body: JSON.stringify({ raw_text, answers }),
    })
  },

  planArchitecture(requirement: RequirementSpec) {
    return request<PlanResult>('/api/architecture/plan', {
      method: 'POST',
      body: JSON.stringify({ requirement }),
    })
  },

  designComponents(requirement: RequirementSpec, architecture: ArchitectureSpec) {
    return request<ComponentPlanResult>('/api/components/design', {
      method: 'POST',
      body: JSON.stringify({ requirement, architecture }),
    })
  },

  // ---- 异步流水线（推荐路径，不再有 90s 同步等待） ----
  runPipeline(raw_text: string, opts: { requireArchitectureApproval?: boolean } = {}) {
    return request<RunPipelineResult>('/api/pipeline/run', {
      method: 'POST',
      body: JSON.stringify({
        raw_text,
        require_architecture_approval: opts.requireArchitectureApproval ?? null,
      }),
    })
  },

  /** 确认架构，从组件设计继续（不重跑需求与架构） */
  approveJob(jobId: string, note = '') {
    return request<RunPipelineResult>(`/api/pipeline/${jobId}/approve`, {
      method: 'POST',
      body: JSON.stringify({ note }),
    })
  },

  rejectJob(jobId: string, note = '') {
    return request<RunPipelineResult>(`/api/pipeline/${jobId}/reject`, {
      method: 'POST',
      body: JSON.stringify({ note }),
    })
  },

  getSandboxStatus() {
    return request<SandboxStatus>('/api/sandbox')
  },

  setSandboxDriver(driver: 'auto' | 'docker' | 'local') {
    return request<SandboxStatus>('/api/sandbox', {
      method: 'PUT',
      body: JSON.stringify({ driver }),
    })
  },

  warmSandbox() {
    return request<SandboxStatus>('/api/sandbox/warm', { method: 'POST' })
  },

  getProjectHistory(projectId: string) {
    return request<{ versions: VersionInfo[]; max_keep: number }>(
      `/api/projects/${projectId}/history`,
    )
  },

  getVersionDiff(projectId: string, version: string) {
    return request<VersionDiff>(`/api/projects/${projectId}/history/${version}/diff`)
  },

  restoreVersion(projectId: string, version: string) {
    return request<RestoreResult>(
      `/api/projects/${projectId}/history/${version}/restore`,
      { method: 'POST' },
    )
  },

  snapshotProject(projectId: string, note = '') {
    return request<VersionInfo>(
      `/api/projects/${projectId}/snapshot?note=${encodeURIComponent(note)}`,
      { method: 'POST' },
    )
  },

  getJob(jobId: string, since = 0, includeResult = true) {
    const qs = `?since=${since}&include_result=${includeResult}`
    return request<JobSnapshot>(`/api/pipeline/${jobId}${qs}`)
  },

  answerJob(jobId: string, answers: Record<string, string>, forceContinue = false) {
    return request<RunPipelineResult>(`/api/pipeline/${jobId}/answers`, {
      method: 'POST',
      body: JSON.stringify({ answers, force_continue: forceContinue }),
    })
  },

  cancelJob(jobId: string) {
    return request<RunPipelineResult>(`/api/pipeline/${jobId}/cancel`, {
      method: 'POST',
    })
  },

  // ---- 项目库（结果持久化）----
  listProjects(includeArchived = true) {
    return request<ProjectSummary[]>(
      `/api/projects?include_archived=${includeArchived}`,
    )
  },

  getProject(projectId: string) {
    return request<ProjectDetail>(`/api/projects/${projectId}`)
  },

  createProject(jobId: string, name?: string) {
    return request<ProjectSummary>('/api/projects', {
      method: 'POST',
      body: JSON.stringify({ job_id: jobId, name: name ?? null }),
    })
  },

  updateProject(
    projectId: string,
    patch: {
      name?: string
      description?: string
      status?: 'active' | 'archived'
      tags?: string[]
    },
  ) {
    return request<ProjectSummary>(`/api/projects/${projectId}`, {
      method: 'PATCH',
      body: JSON.stringify(patch),
    })
  },

  deleteProject(projectId: string) {
    return request<{ id: string; deleted: boolean; files_removed: number }>(
      `/api/projects/${projectId}`,
      { method: 'DELETE' },
    )
  },

  refreshProject(projectId: string) {
    return request<{
      id: string
      files_written: number
      files_kept: number
      written: string[]
      stats: { files: number; lines: number; tests: number }
      validation_passed: boolean
    }>(`/api/projects/${projectId}/refresh`, { method: 'POST' })
  },

  /** 真跑一遍 pytest。有用例失败也返回 200，看返回的 ok 字段 */
  runProjectTests(projectId: string) {
    return request<TestReport>(`/api/projects/${projectId}/tests`, { method: 'POST' })
  },

  /** 读上次结果；没跑过时接口 404，这里折成 null 让界面显示"还没跑过" */
  async getLastProjectTests(projectId: string): Promise<TestReport | null> {
    try {
      return await request<TestReport>(`/api/projects/${projectId}/tests`)
    } catch {
      return null
    }
  },

  /** 让 Debug Agent 修一轮（默认最多 2 次，禁改 tests/ 与 .env） */
  healProject(projectId: string, maxAttempts = 2) {
    return request<HealReport>(
      `/api/projects/${projectId}/heal?max_attempts=${maxAttempts}`,
      { method: 'POST' },
    )
  },

  /** 起本机网页（已在跑则复用），返回可直接打开的地址 */
  startPreview(projectId: string) {
    return request<PreviewStatus>(`/api/projects/${projectId}/preview`, {
      method: 'POST',
    })
  },

  stopPreview(projectId: string) {
    return request<{ project_id: string; running: boolean; stopped: boolean }>(
      `/api/projects/${projectId}/preview`,
      { method: 'DELETE' },
    )
  },

  getPreview(projectId: string) {
    return request<PreviewStatus>(`/api/projects/${projectId}/preview`)
  },

  async getLastHeal(projectId: string): Promise<HealReport | null> {
    try {
      return await request<HealReport>(`/api/projects/${projectId}/heal`)
    } catch {
      return null
    }
  },

  getLlmSettings() {
    return request<LLMSettings>('/api/settings/llm')
  },

  updateLlmSettings(patch: { thinking?: string; temperature?: number }) {
    return request<LLMSettings>('/api/settings/llm', {
      method: 'PUT',
      body: JSON.stringify(patch),
    })
  },

  getProjectFile(projectId: string, path: string) {
    return request<ProjectFileContent>(
      `/api/projects/${projectId}/files?path=${encodeURIComponent(path)}`,
    )
  },

  saveProjectFile(projectId: string, path: string, content: string) {
    return request<ProjectFileContent>(
      `/api/projects/${projectId}/files?path=${encodeURIComponent(path)}`,
      { method: 'PUT', body: JSON.stringify({ content }) },
    )
  },

  getProjectDesign(
    projectId: string,
    stage: 'requirement' | 'architecture' | 'components' | 'delivery',
  ) {
    return request<Record<string, unknown>>(
      `/api/projects/${projectId}/design/${stage}`,
    )
  },

  projectZipUrl(projectId: string) {
    return `/api/projects/${projectId}/zip`
  },
}
