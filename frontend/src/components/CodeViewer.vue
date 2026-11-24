<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import type { CodegenResult } from '../types'

const props = defineProps<{
  result: CodegenResult
  jobId: string
}>()

const selected = ref<string>('')
const copied = ref(false)

// 默认打开最"有信息量"的文件：先 router，其次 README
const PREFERRED = ['agent/router.py', 'agent/prompts.py', 'README.md']

watch(
  () => props.result.project_name,
  () => {
    const paths = props.result.files.map((f) => f.path)
    selected.value = PREFERRED.find((p) => paths.includes(p)) ?? paths[0] ?? ''
  },
  { immediate: true },
)

interface Node {
  name: string
  path: string | null
  children: Node[]
  lines?: number
}

const tree = computed<Node[]>(() => {
  const root: Node[] = []
  for (const f of props.result.files) {
    const parts = f.path.split('/')
    let level = root
    parts.forEach((part, idx) => {
      const isFile = idx === parts.length - 1
      let node = level.find((n) => n.name === part && !!n.path === isFile)
      if (!node) {
        node = {
          name: part,
          path: isFile ? f.path : null,
          children: [],
          lines: isFile ? f.lines : undefined,
        }
        level.push(node)
      }
      level = node.children
    })
  }
  const sort = (nodes: Node[]) => {
    nodes.sort((a, b) => {
      if (!!a.path !== !!b.path) return a.path ? 1 : -1
      return a.name.localeCompare(b.name)
    })
    nodes.forEach((n) => sort(n.children))
  }
  sort(root)
  return root
})

const current = computed(() =>
  props.result.files.find((f) => f.path === selected.value),
)

const KIND_LABEL: Record<string, string> = {
  python: 'PY',
  markdown: 'MD',
  yaml: 'YML',
  text: 'TXT',
  json: 'JSON',
}

async function copy() {
  if (!current.value) return
  try {
    await navigator.clipboard.writeText(current.value.content)
    copied.value = true
    window.setTimeout(() => (copied.value = false), 1600)
  } catch {
    copied.value = false
  }
}

function download() {
  window.open(`/api/pipeline/${props.jobId}/zip`, '_blank')
}
</script>

<template>
  <div class="code-wrap">
    <div class="code-head">
      <div class="code-stats">
        <span class="meta-pill">
          项目 <strong>{{ result.project_name }}</strong>
        </span>
        <span class="meta-pill">{{ result.stats.files }} 个文件</span>
        <span class="meta-pill">{{ result.stats.lines }} 行</span>
        <span class="meta-pill">{{ result.stats.tests }} 个测试文件</span>
        <span class="meta-pill pre">模板生成 · 0 token</span>
        <span
          class="tag"
          :class="result.validation.passed ? 'green' : 'red'"
        >
          {{ result.validation.passed ? '自检通过' : '自检未通过' }}
        </span>
      </div>
      <button class="btn-primary" style="padding: 7px 14px" @click="download">
        下载 zip
      </button>
    </div>

    <div v-if="result.warnings.length" class="warn-strip">
      <span>⚠</span>
      <span>{{ result.warnings.join(' ') }}</span>
    </div>

    <div class="code-body">
      <aside class="file-tree">
        <div class="tree-root">{{ result.project_name }}/</div>
        <template v-for="node in tree" :key="node.name">
          <div v-if="!node.path" class="tree-dir">
            <div class="tree-dir-name">{{ node.name }}/</div>
            <div
              v-for="child in node.children"
              :key="child.path ?? child.name"
              class="tree-file indent"
              :class="{ active: selected === child.path }"
              @click="child.path && (selected = child.path)"
            >
              {{ child.name }}
            </div>
          </div>
          <div
            v-else
            class="tree-file"
            :class="{ active: selected === node.path }"
            @click="selected = node.path"
          >
            {{ node.name }}
          </div>
        </template>
      </aside>

      <section class="code-view">
        <div class="code-bar">
          <span class="code-path">{{ current?.path }}</span>
          <span v-if="current" class="code-kind">{{ KIND_LABEL[current.kind] }}</span>
          <span v-if="current" class="code-lines">{{ current.lines }} 行</span>
          <button class="copy-btn" @click="copy">
            {{ copied ? '已复制' : '复制' }}
          </button>
        </div>
        <pre v-if="current" class="code-pre"><code>{{ current.content }}</code></pre>
        <div v-else class="card-sub" style="padding: 16px">
          左侧选择一个文件查看内容。
        </div>
      </section>
    </div>

    <div class="card" style="margin-top: 12px">
      <div class="card-head">
        <div class="card-title">组件落地对照</div>
        <span class="card-sub">确认每个设计过的组件都有对应代码位置</span>
      </div>
      <div class="cover-grid">
        <div v-for="c in result.coverage" :key="c.component" class="cover-item">
          <span class="tag blue">{{ c.component_type }}</span>
          <span class="cover-name">{{ c.component }}</span>
          <span class="cover-target">{{ c.target }}</span>
        </div>
      </div>
    </div>
  </div>
</template>
