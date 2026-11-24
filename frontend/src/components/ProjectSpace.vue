<script setup lang="ts">
/**
 * 项目库：把每次构建的产物当成可管理的工程来用。
 * 列表 + 详情两栏，详情里能看文件、改文件、看设计依据、改名归档删除、打包下载。
 */
import { computed, onMounted, ref } from 'vue'
import { api } from '../api'
import DeliveryPlanView from './DeliveryPlanView.vue'
import type {
  DeliveryPlan,
  HealReport,
  ProjectDetail,
  RestoreResult,
  SandboxStatus,
  VersionDiff,
  VersionInfo,
  ProjectFileContent,
  ProjectSummary,
  PreviewStatus,
  TestReport,
} from '../types'

const emit = defineEmits<{ (e: 'open-build'): void }>()

const projects = ref<ProjectSummary[]>([])
const loading = ref(false)
const errorMsg = ref('')
/** 删除等操作的临时反馈（绿色条），区别于红色 errorMsg */
const notice = ref('')
const keyword = ref('')
const showArchived = ref(true)

const selectedId = ref('')
const detail = ref<ProjectDetail | null>(null)
const detailLoading = ref(false)

type Pane = 'files' | 'delivery' | 'tests' | 'versions' | 'design' | 'about'

/** 支持 #projects/delivery 这类深链，便于直接分享"我要看清单" */
function initialPane(): Pane {
  const seg = window.location.hash.replace('#', '').split('/')[1]
  const known: Pane[] = ['delivery', 'tests', 'versions', 'design', 'about']
  return known.includes(seg as Pane) ? (seg as Pane) : 'files'
}

/** #projects/versions/<projectId>：直接把某个项目某一页发给别人 */
function hashProject(): string {
  const seg = window.location.hash.replace('#', '').split('/')[2]
  if (!seg) return ''
  try {
    return decodeURIComponent(seg)
  } catch {
    return seg
  }
}

/** 当前 Tab + 当前项目都写进 hash，刷新和分享都能回到原处 */
function syncHash() {
  const parts = ['#projects']
  if (activePane.value !== 'files') parts.push(activePane.value)
  else if (selectedId.value) parts.push('files')
  if (selectedId.value) parts.push(encodeURIComponent(selectedId.value))
  const target = parts.join('/')
  if (window.location.hash !== target) {
    window.history.replaceState(null, '', target)
  }
}

const activePane = ref<Pane>(initialPane())
const delivery = ref<DeliveryPlan | null>(null)
const deliveryLoading = ref(false)

const filePath = ref('')
const file = ref<ProjectFileContent | null>(null)
const fileLoading = ref(false)
const editing = ref(false)
const draft = ref('')
const saving = ref(false)
/** 点「更新代码」后重新渲染模板的过渡态 */
const refreshing = ref(false)

/** 本机网页预览：起一个进程，点开就能用 */
const preview = ref<PreviewStatus | null>(null)
const previewing = ref(false)

/** 上一次测试结果；null 表示这个项目还没跑过 */
const tests = ref<TestReport | null>(null)
const testing = ref(false)
/** 已为哪个项目加载过结果，避免每次切 Tab 都打一次接口 */
const testsLoadedFor = ref('')
/** 自动修复过程中的状态与结果 */
const healing = ref(false)
const healReport = ref<HealReport | null>(null)
/** 执行隔离状态（容器 / 本机子进程），测试页顶部如实显示 */
const sandboxInfo = ref<SandboxStatus | null>(null)
const switchingSandbox = ref(false)
/** 版本历史 */
const versions = ref<VersionInfo[] | null>(null)
const versionDiffs = ref<Record<string, VersionDiff>>({})
const restoringVersion = ref('')
const restoreResult = ref<RestoreResult | null>(null)

const designStage = ref<'requirement' | 'architecture' | 'components'>('requirement')
const designJson = ref('')
const designLoading = ref(false)

const renaming = ref(false)
const nameDraft = ref('')
const descDraft = ref('')
const descEditing = ref(false)

const filtered = computed(() => {
  const kw = keyword.value.trim().toLowerCase()
  return projects.value.filter((p) => {
    if (!showArchived.value && p.status === 'archived') return false
    if (!kw) return true
    return (
      p.name.toLowerCase().includes(kw) ||
      p.id.includes(kw) ||
      p.agent_name?.toLowerCase().includes(kw) ||
      p.tags.some((t) => t.toLowerCase().includes(kw))
    )
  })
})

interface Row {
  path: string
  name: string
  depth: number
  isDir: boolean
  lines: number
}

/** 把扁平文件清单折成带缩进的树行，避免为递归再写一个组件 */
const rows = computed<Row[]>(() => {
  const files = detail.value?.file_list ?? []
  interface Node {
    name: string
    path: string | null
    lines: number
    children: Map<string, Node>
  }
  const root: Node = { name: '', path: null, lines: 0, children: new Map() }
  for (const f of files) {
    const parts = f.path.split('/')
    let cur = root
    parts.forEach((part, i) => {
      const isFile = i === parts.length - 1
      let node = cur.children.get(part)
      if (!node) {
        node = {
          name: part,
          path: isFile ? f.path : null,
          lines: isFile ? f.lines : 0,
          children: new Map(),
        }
        cur.children.set(part, node)
      }
      cur = node
    })
  }
  const out: Row[] = []
  const walk = (node: Node, depth: number) => {
    const kids = [...node.children.values()].sort((a, b) => {
      if (!!a.path !== !!b.path) return a.path ? 1 : -1
      return a.name.localeCompare(b.name)
    })
    for (const k of kids) {
      out.push({ path: k.path ?? '', name: k.name, depth, isDir: !k.path, lines: k.lines })
      if (!k.path) walk(k, depth + 1)
    }
  }
  walk(root, 0)
  return out
})

const projectKindLabel = computed(() => {
  const p = detail.value
  if (!p?.architecture_type) return ''
  return p.architecture_type.replace('_', ' ')
})

function fmtTime(epoch: number): string {
  const d = new Date(epoch * 1000)
  const now = Date.now()
  const diff = (now - d.getTime()) / 1000
  if (diff < 60) return '刚刚'
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`
  if (diff < 86400 * 7) return `${Math.floor(diff / 86400)} 天前`
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
}

async function load() {
  loading.value = true
  errorMsg.value = ''
  try {
    projects.value = await api.listProjects(true)
    if (!selectedId.value && projects.value.length) {
      await selectProject(projects.value[0].id)
    } else if (selectedId.value && !projects.value.some((p) => p.id === selectedId.value)) {
      selectedId.value = ''
      detail.value = null
    }
  } catch (err) {
    errorMsg.value = err instanceof Error ? err.message : String(err)
  } finally {
    loading.value = false
  }
}

async function selectProject(id: string) {
  selectedId.value = id
  notice.value = ''
  detailLoading.value = true
  editing.value = false
  renaming.value = false
  descEditing.value = false
  designJson.value = ''
  delivery.value = null
  tests.value = null
  healReport.value = null
  versions.value = null
  versionDiffs.value = {}
  restoreResult.value = null
  testsLoadedFor.value = ''
  preview.value = null
  errorMsg.value = ''
  try {
    const d = await api.getProject(id)
    detail.value = d
    descDraft.value = d.description
    const paths = d.file_list.map((f) => f.path)
    const preferred =
      ['agent/router.py', 'agent/prompts.py', 'README.md', 'main.py'].find((p) =>
        paths.includes(p),
      ) ?? paths[0]
    if (preferred) await openFile(preferred)
    else {
      filePath.value = ''
      file.value = null
    }
    if (activePane.value === 'design') await loadDesign(designStage.value)
    if (activePane.value === 'delivery') await loadDelivery()
    // 深链直接落在某个 Tab 上时（#projects/tests），也要把数据拉起来
    if (activePane.value === 'tests') {
      await loadTests(true)
      await loadSandbox()
    }
    if (activePane.value === 'versions') await loadVersions(true)
    await loadPreview()
    syncHash()
  } catch (err) {
    errorMsg.value = err instanceof Error ? err.message : String(err)
  } finally {
    detailLoading.value = false
  }
}

async function openFile(path: string) {
  if (!selectedId.value) return
  fileLoading.value = true
  editing.value = false
  errorMsg.value = ''
  try {
    file.value = await api.getProjectFile(selectedId.value, path)
    filePath.value = path
  } catch (err) {
    errorMsg.value = err instanceof Error ? err.message : String(err)
  } finally {
    fileLoading.value = false
  }
}

function startEdit() {
  if (!file.value) return
  draft.value = file.value.content
  editing.value = true
}

function cancelEdit() {
  editing.value = false
  draft.value = ''
}

async function saveFile() {
  if (!file.value || !selectedId.value) return
  saving.value = true
  try {
    const saved = await api.saveProjectFile(selectedId.value, file.value.path, draft.value)
    file.value = saved
    editing.value = false
    await refreshDetailOnly()
  } catch (err) {
    errorMsg.value = err instanceof Error ? err.message : String(err)
  } finally {
    saving.value = false
  }
}

/** 查一次预览状态：只是在跑就显示"打开"，不在跑也不报错 */
async function loadPreview() {
  if (!selectedId.value) return
  try {
    preview.value = await api.getPreview(selectedId.value)
  } catch {
    preview.value = null
  }
}

/** 打开网页：没起就先起，起了直接开新标签 */
async function openPreview() {
  if (!selectedId.value || previewing.value) return
  previewing.value = true
  errorMsg.value = ''
  // 必须先同步开一个空窗口：await 之后再 window.open 会被浏览器当弹窗拦掉，
  // 表现为"点了没反应"，而接口其实已经成功了。
  const win = window.open('', '_blank')
  try {
    const res = await api.startPreview(selectedId.value)
    preview.value = res
    if (res.url) {
      if (win) win.location.href = res.url
      else window.open(res.url, '_blank', 'noopener')
    } else if (win) {
      win.close()
    }
  } catch (err) {
    if (win) win.close()
    errorMsg.value = err instanceof Error ? err.message : String(err)
  } finally {
    previewing.value = false
  }
}

async function closePreview() {
  if (!selectedId.value) return
  try {
    await api.stopPreview(selectedId.value)
    await loadPreview()
  } catch (err) {
    errorMsg.value = err instanceof Error ? err.message : String(err)
  }
}

/** 改完文件后行数会变，只刷详情不动左侧选中项 */
async function refreshDetailOnly() {
  if (!selectedId.value) return
  try {
    const d = await api.getProject(selectedId.value)
    detail.value = d
    const idx = projects.value.findIndex((p) => p.id === d.id)
    if (idx >= 0) projects.value[idx] = { ...projects.value[idx], ...d }
  } catch {
    /* 详情刷新失败不影响保存结果本身 */
  }
}

async function loadDesign(stage: 'requirement' | 'architecture' | 'components') {
  if (!selectedId.value) return
  designStage.value = stage
  designLoading.value = true
  try {
    const data = await api.getProjectDesign(selectedId.value, stage)
    designJson.value = JSON.stringify(data, null, 2)
  } catch (err) {
    designJson.value = `// ${err instanceof Error ? err.message : String(err)}`
  } finally {
    designLoading.value = false
  }
}

async function loadDelivery() {
  if (!selectedId.value) return
  deliveryLoading.value = true
  try {
    delivery.value = (await api.getProjectDesign(selectedId.value, 'delivery')) as unknown as DeliveryPlan
  } catch (err) {
    errorMsg.value = err instanceof Error ? err.message : String(err)
  } finally {
    deliveryLoading.value = false
  }
}

async function loadTests(force = false) {
  if (!selectedId.value) return
  if (!force && testsLoadedFor.value === selectedId.value) return
  tests.value = await api.getLastProjectTests(selectedId.value)
  healReport.value = await api.getLastHeal(selectedId.value)
  testsLoadedFor.value = selectedId.value
}

async function healTests() {
  if (!selectedId.value) return
  healing.value = true
  errorMsg.value = ''
  healReport.value = null
  try {
    const report = await api.healProject(selectedId.value)
    healReport.value = report
    notice.value = report.final.ok
      ? `修好了：${report.note}（改了 ${report.changed_files.length} 个文件）`
      : report.note
    tests.value = await api.getLastProjectTests(selectedId.value)
    await refreshDetailOnly()
  } catch (err) {
    errorMsg.value = err instanceof Error ? err.message : String(err)
  } finally {
    healing.value = false
  }
}

async function runTests() {
  if (!selectedId.value) return
  testing.value = true
  errorMsg.value = ''
  try {
    const report = await api.runProjectTests(selectedId.value)
    tests.value = report
    testsLoadedFor.value = selectedId.value
    notice.value = report.ok
      ? `测试通过：${report.passed} 个用例，耗时 ${report.duration_s}s`
      : report.timed_out
        ? '测试超时，已强制结束'
        : `测试未通过：${report.failed + report.error} 个用例失败（共 ${report.total} 个）`
    await refreshDetailOnly()
  } catch (err) {
    errorMsg.value = err instanceof Error ? err.message : String(err)
  } finally {
    testing.value = false
  }
}

async function loadSandbox() {
  if (sandboxInfo.value) return
  try {
    sandboxInfo.value = await api.getSandboxStatus()
  } catch {
    sandboxInfo.value = null
  }
}

async function switchSandbox(driver: 'auto' | 'docker' | 'local') {
  switchingSandbox.value = true
  errorMsg.value = ''
  try {
    sandboxInfo.value = await api.setSandboxDriver(driver)
    notice.value = `执行方式：${sandboxInfo.value.reason}`
  } catch (err) {
    errorMsg.value = err instanceof Error ? err.message : String(err)
  } finally {
    switchingSandbox.value = false
  }
}

async function loadVersions(force = false) {
  if (!selectedId.value) return
  if (!force && versions.value) return
  try {
    versions.value = (await api.getProjectHistory(selectedId.value)).versions
    versionDiffs.value = {}
  } catch (err) {
    errorMsg.value = err instanceof Error ? err.message : String(err)
  }
}

async function loadVersionDiff(v: string) {
  if (!selectedId.value || versionDiffs.value[v]) return
  try {
    const d = await api.getVersionDiff(selectedId.value, v)
    versionDiffs.value = { ...versionDiffs.value, [v]: d }
  } catch (err) {
    errorMsg.value = err instanceof Error ? err.message : String(err)
  }
}

async function restoreTo(v: string) {
  if (!selectedId.value || restoringVersion.value) return
  const ok = window.confirm(
    `回滚到版本 ${v}？\n\n` +
      '会用该版本覆盖同名文件（不会删除现在多出来的文件），' +
      '.env / knowledge / data 不受影响。\n' +
      '回滚前会自动给当前状态存一份快照，反悔还能再滚回来。',
  )
  if (!ok) return
  restoringVersion.value = v
  errorMsg.value = ''
  try {
    restoreResult.value = await api.restoreVersion(selectedId.value, v)
    notice.value = `已回滚到 ${v}（回滚前状态存为 ${restoreResult.value.backup_version}）`
    versions.value = null
    await loadVersions(true)
    await refreshDetailOnly()
    await load()
  } catch (err) {
    errorMsg.value = err instanceof Error ? err.message : String(err)
  } finally {
    restoringVersion.value = ''
  }
}

async function saveManualSnapshot() {
  if (!selectedId.value) return
  try {
    const v = await api.snapshotProject(selectedId.value, '手动保存')
    notice.value = `已保存版本 ${v.version}`
    versions.value = null
    await loadVersions(true)
  } catch (err) {
    errorMsg.value = err instanceof Error ? err.message : String(err)
  }
}

function switchPane(pane: Pane) {
  activePane.value = pane
  syncHash()
  if (pane === 'design' && !designJson.value) loadDesign(designStage.value)
  if (pane === 'delivery' && !delivery.value) loadDelivery()
  if (pane === 'tests') {
    loadTests()
    loadSandbox()
  }
  if (pane === 'versions') loadVersions()
}

function startRename() {
  if (!detail.value) return
  nameDraft.value = detail.value.name
  renaming.value = true
}

async function commitRename() {
  const name = nameDraft.value.trim()
  if (!detail.value || !name) {
    renaming.value = false
    return
  }
  try {
    await api.updateProject(detail.value.id, { name })
    renaming.value = false
    await refreshDetailOnly()
  } catch (err) {
    errorMsg.value = err instanceof Error ? err.message : String(err)
  }
}

async function commitDescription() {
  if (!detail.value) return
  try {
    await api.updateProject(detail.value.id, { description: descDraft.value })
    descEditing.value = false
    await refreshDetailOnly()
  } catch (err) {
    errorMsg.value = err instanceof Error ? err.message : String(err)
  }
}

async function toggleArchive() {
  if (!detail.value) return
  const next = detail.value.status === 'archived' ? 'active' : 'archived'
  try {
    await api.updateProject(detail.value.id, { status: next })
    await refreshDetailOnly()
  } catch (err) {
    errorMsg.value = err instanceof Error ? err.message : String(err)
  }
}

async function refreshCode() {
  if (!detail.value) return
  const ok = window.confirm(
    `用最新模板重新生成「${detail.value.name}」的代码？\n\n` +
      `· 纯模板渲染，不调模型、不花 token，几秒钟完成\n` +
      `· 会覆盖同名代码文件（你手改过的也会被覆盖）\n` +
      `· .env、knowledge/、data/ 里的东西不会动`,
  )
  if (!ok) return
  refreshing.value = true
  errorMsg.value = ''
  try {
    const info = await api.refreshProject(detail.value.id)
    notice.value = `已按最新模板重新生成：写入 ${info.files_written} 个文件，跳过 ${info.files_kept} 个`
    await refreshDetailOnly()
  } catch (err) {
    errorMsg.value = err instanceof Error ? err.message : String(err)
  } finally {
    refreshing.value = false
  }
}

async function removeProject() {
  if (!detail.value) return
  const name = detail.value.name
  const files = detail.value.files
  const ok = window.confirm(
    `彻底删除项目「${name}」？\n\n` +
      `会连同 ${files} 个文件一起从磁盘删除，不能撤销、也没有回收站。\n` +
      `如果还想留一份，请先「下载 zip」。`,
  )
  if (!ok) return
  try {
    const info = await api.deleteProject(detail.value.id)
    detail.value = null
    selectedId.value = ''
    file.value = null
    notice.value = `已彻底删除「${name}」（移除 ${info.files_removed} 个文件）`
    await load()
  } catch (err) {
    errorMsg.value = err instanceof Error ? err.message : String(err)
  }
}

function downloadZip() {
  if (!detail.value) return
  window.open(api.projectZipUrl(detail.value.id), '_blank')
}

async function copyPath() {
  if (!file.value) return
  try {
    await navigator.clipboard.writeText(file.value.content)
  } catch {
    /* 剪贴板不可用时静默失败，不打断操作 */
  }
}

onMounted(async () => {
  // 先把深链里的项目号记下来：load() 会顺手把第一个项目写进 hash，
  // 晚一步读就只能读到它，深链就失效了。
  const want = hashProject()
  await load()
  if (want && projects.value.some((p) => p.id === want)) {
    await selectProject(want)
  }
})

defineExpose({ load })
</script>

<template>
  <div class="project-space">
    <!-- 左：项目列表 -->
    <aside class="project-list-pane">
      <div class="project-list-head">
        <div class="pane-title" style="margin: 0">
          项目库
          <span class="hint">{{ projects.length }} 个</span>
        </div>
        <button class="ghost-btn" title="刷新" @click="load">刷新</button>
      </div>

      <input v-model="keyword" class="proj-search" placeholder="搜索名称 / 标签" />

      <label class="proj-filter">
        <input v-model="showArchived" type="checkbox" />
        显示已归档
      </label>

      <div v-if="errorMsg" class="error-box" style="margin: 0 12px 8px">{{ errorMsg }}</div>
      <div v-if="notice" class="notice-strip" style="margin: 0 12px 8px">{{ notice }}</div>

      <div class="project-list">
        <div v-if="loading && !projects.length" class="pane-hint">正在读取…</div>
        <div v-else-if="!filtered.length" class="pane-hint">
          还没有项目。构建完成后会自动保存到这里。
        </div>
        <div
          v-for="p in filtered"
          :key="p.id"
          class="project-item"
          :class="{ active: p.id === selectedId, archived: p.status === 'archived' }"
          @click="selectProject(p.id)"
        >
          <div class="project-item-top">
            <span class="project-name">{{ p.name }}</span>
            <span v-if="p.status === 'archived'" class="tag gray">已归档</span>
            <span v-else-if="!p.validation_passed" class="tag red">自检未过</span>
            <span
              v-if="p.tests_ok === true"
              class="tag green"
              :title="`真跑 pytest：${p.tests_total} 个用例全通过`"
            >测试通过</span>
            <span
              v-else-if="p.tests_ok === false"
              class="tag red"
              :title="`真跑 pytest：${p.tests_failed} 个用例失败`"
            >测试失败</span>
          </div>
          <div class="project-item-meta">
            {{ p.files }} 文件 · {{ p.lines }} 行 · {{ fmtTime(p.updated_at) }}
          </div>
          <div class="project-item-id">{{ p.id }}</div>
        </div>
      </div>

      <button class="run-btn" style="margin: 10px 12px" @click="emit('open-build')">
        去构建新项目
      </button>
    </aside>

    <!-- 右：项目详情 -->
    <main class="project-detail">
      <div v-if="!detail" class="empty-state">
        <div class="big">选择一个项目</div>
        <div class="small">
          左侧列表来自磁盘上的 data/projects/，构建流水线跑完会自动落一个项目，重启浏览器也不会丢。
        </div>
      </div>

      <template v-else>
        <div class="project-head">
          <div class="project-head-main">
            <div class="project-title-row">
              <template v-if="renaming">
                <input
                  v-model="nameDraft"
                  class="proj-name-input"
                  @keyup.enter="commitRename"
                  @keyup.esc="renaming = false"
                />
                <button class="btn-primary" style="padding: 5px 10px" @click="commitRename">
                  保存
                </button>
                <button class="ghost-btn" @click="renaming = false">取消</button>
              </template>
              <template v-else>
                <span class="project-title">{{ detail.name }}</span>
                <button class="ghost-btn" @click="startRename">重命名</button>
              </template>
            </div>

            <div class="project-tags">
              <span class="meta-pill">id <strong>{{ detail.id }}</strong></span>
              <span class="meta-pill">{{ detail.files }} 文件 · {{ detail.lines }} 行</span>
              <span v-if="projectKindLabel" class="meta-pill">
                架构 <strong>{{ projectKindLabel }}</strong>
              </span>
              <span class="meta-pill">更新于 {{ fmtTime(detail.updated_at) }}</span>
              <span
                class="tag"
                :class="detail.validation_passed ? 'green' : 'red'"
              >
                {{ detail.validation_passed ? '自检通过' : '自检未通过' }}
              </span>
            </div>

            <div v-if="descEditing" class="desc-edit">
              <input v-model="descDraft" class="proj-search" placeholder="一句话说明这个项目" />
              <button class="btn-primary" style="padding: 5px 10px" @click="commitDescription">
                保存
              </button>
              <button class="ghost-btn" @click="descEditing = false">取消</button>
            </div>
            <div v-else class="project-desc">
              {{ detail.description || '还没有描述' }}
              <button class="ghost-btn" @click="descEditing = true">
                {{ detail.description ? '改' : '添加' }}
              </button>
            </div>
          </div>

          <div class="project-actions">
            <button
              class="btn-primary"
              style="padding: 6px 12px"
              :disabled="previewing"
              title="在本机起一个网页，点开就能直接对话（复用平台已配的模型）"
              @click="openPreview"
            >
              {{ previewing ? '启动中…' : preview?.running ? '打开网页' : '启动网页' }}
            </button>
            <button
              v-if="preview?.running"
              class="ghost-btn"
              title="停掉本机网页进程"
              @click="closePreview"
            >
              停止网页
            </button>
            <button class="ghost-btn" @click="downloadZip">下载 zip</button>
            <button
              class="ghost-btn"
              :disabled="refreshing"
              title="用最新模板重新渲染代码（0 token，不动 .env / knowledge / data）"
              @click="refreshCode"
            >
              {{ refreshing ? '生成中…' : '更新代码' }}
            </button>
            <button class="ghost-btn" @click="toggleArchive">
              {{ detail.status === 'archived' ? '取消归档' : '归档' }}
            </button>
            <button class="ghost-btn danger" @click="removeProject">删除</button>
          </div>
        </div>

        <div v-if="preview?.running && preview.url" class="preview-bar">
          <span class="live-dot"></span>
          <span class="preview-label">网页运行中</span>
          <a class="preview-link" :href="preview.url" target="_blank" rel="noopener">
            {{ preview.url }}
          </a>
          <span class="preview-hint">复用平台模型配置 · 只监听本机</span>
        </div>
        <div v-else-if="preview?.log_tail" class="preview-bar bad">
          <span class="preview-label">上次启动没成功</span>
          <span class="preview-hint">{{ preview.log_tail.split('\n').slice(-3).join(' / ') }}</span>
        </div>

        <div class="tab-bar" style="padding-left: 0">
          <div class="tab" :class="{ active: activePane === 'files' }" @click="switchPane('files')">
            文件
            <span class="badge">{{ detail.file_list.length }}</span>
          </div>
          <div
            v-if="detail.has_delivery"
            class="tab"
            :class="{ active: activePane === 'delivery' }"
            @click="switchPane('delivery')"
          >
            交付清单
          </div>
          <div
            class="tab"
            :class="{ active: activePane === 'tests' }"
            @click="switchPane('tests')"
          >
            测试
            <span
              v-if="tests"
              class="badge"
              :class="tests.ok ? 'badge-ok' : 'badge-bad'"
            >{{ tests.ok ? tests.passed : tests.failed + tests.error }}</span>
          </div>
          <div
            class="tab"
            :class="{ active: activePane === 'versions' }"
            @click="switchPane('versions')"
          >
            版本
            <span v-if="versions" class="badge">{{ versions.length }}</span>
          </div>
          <div class="tab" :class="{ active: activePane === 'design' }" @click="switchPane('design')">
            设计依据
          </div>
          <div class="tab" :class="{ active: activePane === 'about' }" @click="switchPane('about')">
            概览
          </div>
        </div>

        <!-- 文件 -->
        <div v-if="activePane === 'files'" class="code-body project-code">
          <aside class="file-tree">
            <div class="tree-root">{{ detail.id }}/</div>
            <div
              v-for="row in rows"
              :key="(row.isDir ? 'd:' : 'f:') + row.depth + row.name + row.path"
              class="tree-file"
              :class="{ active: row.path && row.path === filePath, dir: row.isDir }"
              :style="{ paddingLeft: `${12 + row.depth * 12}px` }"
              @click="row.isDir ? null : openFile(row.path)"
            >
              {{ row.name }}<span v-if="row.isDir" class="tree-slash">/</span>
            </div>
          </aside>

          <section class="code-view">
            <div class="code-bar">
              <span class="code-path">{{ filePath || '未选择文件' }}</span>
              <span v-if="file" class="code-kind">{{ file.kind.toUpperCase() }}</span>
              <span v-if="file" class="code-lines">{{ editing ? draft.split('\n').length : file.lines }} 行</span>
              <span style="flex: 1"></span>
              <template v-if="editing">
                <button class="copy-btn" :disabled="saving" @click="saveFile">
                  {{ saving ? '保存中…' : '保存' }}
                </button>
                <button class="copy-btn" @click="cancelEdit">取消</button>
              </template>
              <template v-else>
                <button class="copy-btn" @click="copyPath">复制</button>
                <button class="copy-btn" :disabled="!file" @click="startEdit">编辑</button>
              </template>
            </div>

            <div v-if="fileLoading" class="card-sub" style="padding: 16px">读取中…</div>
            <textarea
              v-else-if="editing"
              v-model="draft"
              class="code-editor"
              spellcheck="false"
            ></textarea>
            <pre v-else-if="file" class="code-pre"><code>{{ file.content }}</code></pre>
            <div v-else class="card-sub" style="padding: 16px">左侧选择一个文件查看内容。</div>
          </section>
        </div>

        <!-- 交付清单 -->
        <div v-else-if="activePane === 'delivery'">
          <DeliveryPlanView v-if="delivery" :delivery="delivery" />
          <div v-else class="card-sub" style="padding: 16px">
            {{ deliveryLoading ? '读取中…' : '这个项目没有保存交付清单（旧版本生成的项目）。' }}
          </div>
        </div>

        <!-- 设计依据 -->
        <!-- 测试：真跑 pytest，不是静态检查 -->
        <div v-else-if="activePane === 'tests'" class="tests-pane">
          <div v-if="sandboxInfo" class="sandbox-strip" :class="sandboxInfo.driver">
            <span class="sb-dot"></span>
            <span class="sb-text">
              <b>{{ sandboxInfo.isolated ? '容器内执行' : '本机子进程执行' }}</b>
              —— {{ sandboxInfo.reason }}
            </span>
            <span v-if="sandboxInfo.isolated && sandboxInfo.limits.network" class="sb-limit">
              网络 {{ sandboxInfo.limits.network }} · 内存 {{ sandboxInfo.limits.memory }} ·
              {{ sandboxInfo.limits.cpus }} CPU
            </span>
            <span class="sb-switch">
              <button
                v-for="d in (['auto', 'docker', 'local'] as const)"
                :key="d"
                :class="{ on: sandboxInfo.driver === d || (d === 'auto' && false) }"
                :disabled="switchingSandbox"
                @click="switchSandbox(d)"
              >{{ d === 'auto' ? '自动' : d === 'docker' ? '容器' : '本机' }}</button>
            </span>
          </div>

          <div class="tests-head">
            <button class="btn-primary" style="padding: 6px 14px" :disabled="testing" @click="runTests">
              {{ testing ? '跑着呢…' : tests ? '再跑一次' : '运行测试' }}
            </button>
            <button
              v-if="tests && !tests.ok"
              class="btn-primary heal-btn"
              style="padding: 6px 14px"
              :disabled="healing || testing"
              @click="healTests"
            >
              {{ healing ? '修着呢…' : '让 AI 修一下' }}
            </button>
            <span class="card-sub">
              在项目目录里执行 <code>pytest -q</code>，跑的是真实代码；结果会存下来，刷新页面还在
            </span>
            <span v-if="healing" class="card-sub">
              正在读失败原因 → 改代码 → 重跑，最多 2 轮，可能要一两分钟
            </span>
          </div>

          <div v-if="!tests" class="empty-hint">
            还没跑过测试。点「运行测试」会真的启动 pytest —— 静态检查过关不等于跑得起来。
          </div>

          <template v-else>
            <div class="tests-summary" :class="tests.ok ? 'ok' : 'bad'">
              <div class="tests-verdict">
                {{ tests.ok ? '通过' : tests.timed_out ? '超时' : '未通过' }}
              </div>
              <div class="tests-nums">
                <span><b>{{ tests.total }}</b> 个用例</span>
                <span class="num-ok">{{ tests.passed }} 通过</span>
                <span v-if="tests.failed + tests.error" class="num-bad">
                  {{ tests.failed + tests.error }} 失败
                </span>
                <span v-if="tests.skipped" class="num-skip">{{ tests.skipped }} 跳过</span>
                <span>{{ tests.duration_s }}s</span>
                <span class="tests-when">{{ new Date(tests.ran_at * 1000).toLocaleString() }}</span>
              </div>
            </div>

            <div v-for="n in tests.notes" :key="n" class="error-box" style="margin: 10px 0">
              {{ n }}
            </div>

            <!-- 自动修复过程：每轮诊断 + 改了什么 + 拒收原因 -->
            <div v-if="healReport" class="heal-report" :class="healReport.final.ok ? 'ok' : 'bad'">
              <div class="heal-head">
                <b>{{ healReport.final.ok ? '自动修复成功' : '自动修复未成功' }}</b>
                <span class="heal-note">{{ healReport.note }}</span>
                <span class="tests-when">
                  {{ healReport.elapsed_s }}s · 改了 {{ healReport.changed_files.length }} 个文件
                </span>
              </div>
              <div v-for="a in healReport.attempts" :key="a.attempt" class="heal-attempt">
                <div class="heal-line">
                  <span class="heal-tag">第 {{ a.attempt }} 轮</span>
                  <span v-if="a.confidence" class="heal-conf">把握 {{ a.confidence }}</span>
                  <span class="heal-diag">{{ a.diagnosis || a.error || a.note }}</span>
                </div>
                <div v-for="e in a.edits" :key="e.path" class="heal-edit">
                  改 <code>{{ e.path }}</code> · {{ e.lines }} 行<span v-if="e.reason"> —— {{ e.reason }}</span>
                </div>
                <div v-for="x in a.rejected" :key="x" class="heal-reject">未采纳：{{ x }}</div>
                <div class="heal-result">
                  结果：{{ a.result.ok ? '通过' : `${a.result.failed + a.result.error} 个失败` }}
                </div>
              </div>
              <div v-if="healReport.reverted" class="heal-reject">
                补丁让情况变糟（失败数变多或整个模块跑不起来），已全部回滚
              </div>
              <div class="heal-foot">
                规则：不许改 tests/（改断言等于作弊）、不许改 .env、每轮都要重跑测试拿证据
              </div>
            </div>

            <div class="case-list">
              <div
                v-for="c in tests.cases"
                :key="c.name"
                class="case-row"
                :class="c.status"
              >
                <span class="case-dot"></span>
                <span class="case-name">{{ c.name }}</span>
                <span class="case-dur">{{ c.duration.toFixed(3) }}s</span>
                <div v-if="c.message" class="case-msg">{{ c.message }}</div>
              </div>
            </div>

            <details class="tests-output">
              <summary>pytest 原始输出</summary>
              <pre class="code-pre">{{ tests.output_tail }}</pre>
            </details>

            <div class="tests-cmd">执行的命令：<code>{{ tests.command }}</code></div>
          </template>
        </div>

        <div v-else-if="activePane === 'versions'" class="versions-pane">
          <div class="tests-head">
            <button class="ghost-btn" :disabled="!versions" @click="saveManualSnapshot">
              给当前状态存一份
            </button>
            <span class="card-sub">
              初始生成、更新代码前、AI 自动修复前、回滚前都会自动留档，最多保留 10 个
            </span>
          </div>

          <div v-if="restoreResult" class="notice-strip">
            已回滚到 {{ restoreResult.restored_from }}：覆盖 {{ restoreResult.restored.length }} 个文件，
            当前多出来的 {{ restoreResult.kept.length }} 个用户文件未被删除。
          </div>

          <div v-if="!versions || !versions.length" class="empty-hint">
            还没有版本快照。
          </div>

          <div v-for="v in versions || []" :key="v.version" class="version-row">
            <div class="version-main">
              <span class="version-id">{{ v.version }}</span>
              <span class="version-reason">{{ v.reason }}</span>
              <span class="version-meta">
                {{ v.files }} 文件 / {{ v.lines }} 行 ·
                {{ new Date(v.created_at * 1000).toLocaleString() }}
              </span>
              <span v-if="v.note" class="version-note">{{ v.note }}</span>
            </div>
            <div class="version-actions">
              <button class="ghost-btn small" @click="loadVersionDiff(v.version)">看差异</button>
              <button
                class="ghost-btn small danger"
                :disabled="restoringVersion === v.version"
                @click="restoreTo(v.version)"
              >
                {{ restoringVersion === v.version ? '回滚中…' : '回滚到此版本' }}
              </button>
            </div>
            <div v-if="versionDiffs[v.version]" class="version-diff">
              <div v-if="versionDiffs[v.version].identical" class="card-sub">
                与当前文件完全一致
              </div>
              <template v-else>
                <div class="diff-summary">
                  {{ versionDiffs[v.version].modified }} 处改动 ·
                  {{ versionDiffs[v.version].added }} 个新增 ·
                  {{ versionDiffs[v.version].removed }} 个只在该版本里
                </div>
                <div
                  v-for="c in versionDiffs[v.version].changes"
                  :key="c.path"
                  class="diff-row"
                  :class="c.status"
                >
                  <span class="diff-flag">{{ c.status === 'modified' ? '改' : c.status === 'added' ? '增' : '缺' }}</span>
                  <code>{{ c.path }}</code>
                  <span v-if="c.lines_delta" class="diff-delta">
                    {{ c.lines_delta > 0 ? '+' : '' }}{{ c.lines_delta }} 行
                  </span>
                </div>
              </template>
            </div>
          </div>
        </div>

        <div v-else-if="activePane === 'design'" class="design-pane">
          <div class="design-tabs">
            <button
              class="ghost-btn"
              :class="{ on: designStage === 'requirement' }"
              :disabled="!detail.has_requirement"
              @click="loadDesign('requirement')"
            >
              结构化需求
            </button>
            <button
              class="ghost-btn"
              :class="{ on: designStage === 'architecture' }"
              :disabled="!detail.has_architecture"
              @click="loadDesign('architecture')"
            >
              架构方案
            </button>
            <button
              class="ghost-btn"
              :class="{ on: designStage === 'components' }"
              :disabled="!detail.has_components"
              @click="loadDesign('components')"
            >
              组件清单
            </button>
            <span class="card-sub">代码就是按这三份产物模板化渲染出来的</span>
          </div>
          <pre class="code-pre design-pre"><code>{{ designLoading ? '读取中…' : designJson }}</code></pre>
        </div>

        <!-- 概览 -->
        <div v-else class="about-pane">
          <div class="card">
            <div class="card-head">
              <div class="card-title">需求原文</div>
              <span class="card-sub">回顾当初是怎么描述这个 Agent 的</span>
            </div>
            <div class="about-text">{{ detail.raw_text || '（未记录）' }}</div>
          </div>

          <div class="card">
            <div class="card-head">
              <div class="card-title">统计</div>
              <span class="card-sub">以磁盘上的文件为准重新统计</span>
            </div>
            <div class="about-grid">
              <div><span class="about-k">文件</span><span class="about-v">{{ detail.files }}</span></div>
              <div><span class="about-k">代码行</span><span class="about-v">{{ detail.lines }}</span></div>
              <div><span class="about-k">测试文件</span><span class="about-v">{{ detail.tests }}</span></div>
              <div>
                <span class="about-k">来源任务</span>
                <span class="about-v">{{ detail.job_id || '—' }}</span>
              </div>
              <div>
                <span class="about-k">创建</span>
                <span class="about-v">{{ fmtTime(detail.created_at) }}</span>
              </div>
              <div>
                <span class="about-k">标签</span>
                <span class="about-v">{{ detail.tags.length ? detail.tags.join(' / ') : '—' }}</span>
              </div>
            </div>
          </div>

          <div v-if="detail.warnings.length || detail.notes.length" class="card">
            <div class="card-head"><div class="card-title">生成时的提示</div></div>
            <div v-for="w in detail.warnings" :key="w" class="list-item">
              <span class="bullet" style="color: var(--amber)">⚠</span><span>{{ w }}</span>
            </div>
            <div v-for="n in detail.notes" :key="n" class="list-item">
              <span class="bullet">·</span><span>{{ n }}</span>
            </div>
          </div>
        </div>
      </template>
    </main>
  </div>
</template>
