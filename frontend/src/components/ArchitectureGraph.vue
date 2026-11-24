<script setup lang="ts">
import { computed } from 'vue'
import { MarkerType } from '@vue-flow/core'
import { VueFlow } from '@vue-flow/core'
import { Background } from '@vue-flow/background'
import { Controls } from '@vue-flow/controls'
import { MiniMap } from '@vue-flow/minimap'
import type { ArchitectureSpec } from '../types'

const props = defineProps<{ spec: ArchitectureSpec }>()

interface LayoutNode {
  id: string
  name: string
  responsibility: string
  tools: string[]
  skills: string[]
  x: number
  y: number
  level: number
}

const NODE_W = 208
const COL_GAP = 88
const ROW_GAP = 34
const NODE_H = 132

/**
 * 分层布局：按"从入口出发的最长路径"给节点定层级，
 * 同一层纵向均匀排开。这样 workflow 的流水线形态一眼能看出来，
 * 不引入 dagre 之类的额外依赖。
 */
const layout = computed<LayoutNode[]>(() => {
  const agents = props.spec.agents
  if (!agents.length) return []

  const incoming = new Map<string, string[]>()
  const outgoing = new Map<string, string[]>()
  for (const a of agents) {
    incoming.set(a.id, [])
    outgoing.set(a.id, [])
  }
  for (const e of props.spec.workflow_edges) {
    if (!incoming.has(e.target) || !outgoing.has(e.source)) continue
    incoming.get(e.target)!.push(e.source)
    outgoing.get(e.source)!.push(e.target)
  }

  // 最长路径层级，带环路保护
  const level = new Map<string, number>()
  const visiting = new Set<string>()

  const resolve = (id: string): number => {
    if (level.has(id)) return level.get(id)!
    if (visiting.has(id)) return 0
    visiting.add(id)
    const parents = incoming.get(id) ?? []
    const value = parents.length
      ? Math.max(...parents.map((p) => resolve(p) + 1))
      : 0
    visiting.delete(id)
    level.set(id, value)
    return value
  }
  agents.forEach((a) => resolve(a.id))

  const columns = new Map<number, string[]>()
  for (const a of agents) {
    const lv = level.get(a.id) ?? 0
    if (!columns.has(lv)) columns.set(lv, [])
    columns.get(lv)!.push(a.id)
  }

  const maxRows = Math.max(...[...columns.values()].map((c) => c.length))
  const result: LayoutNode[] = []

  for (const [lv, ids] of [...columns.entries()].sort((a, b) => a[0] - b[0])) {
    const columnHeight = ids.length * NODE_H + (ids.length - 1) * ROW_GAP
    const offsetY = ((maxRows * NODE_H + (maxRows - 1) * ROW_GAP) - columnHeight) / 2
    ids.forEach((id, idx) => {
      const agent = agents.find((a) => a.id === id)!
      result.push({
        id,
        name: agent.name,
        responsibility: agent.responsibility,
        tools: agent.tools,
        skills: agent.skills,
        level: lv,
        x: lv * (NODE_W + COL_GAP),
        y: offsetY + idx * (NODE_H + ROW_GAP),
      })
    })
  }
  return result
})

const entryIds = computed(() => {
  const targets = new Set(props.spec.workflow_edges.map((e) => e.target))
  const sources = new Set(props.spec.workflow_edges.map((e) => e.source))
  if (!props.spec.workflow_edges.length) return new Set(props.spec.agents.map((a) => a.id))
  return new Set([...sources].filter((s) => !targets.has(s)))
})

const nodes = computed(() =>
  layout.value.map((n) => ({
    id: n.id,
    type: 'agent',
    position: { x: n.x, y: n.y },
    data: { ...n, isEntry: entryIds.value.has(n.id) },
    draggable: true,
  })),
)

const edges = computed(() =>
  props.spec.workflow_edges.map((e, i) => ({
    id: `e-${i}-${e.source}-${e.target}`,
    source: e.source,
    target: e.target,
    label: e.condition ?? '',
    type: 'smoothstep',
    animated: !!e.condition,
    markerEnd: MarkerType.ArrowClosed,
    style: { stroke: e.condition ? '#b7791f' : '#b6bec9', strokeWidth: 1.4 },
    labelBgStyle: { fill: '#fffdf8', fillOpacity: 0.95 },
    labelStyle: { fill: '#8a6a1f', fontSize: '10px' },
  })),
)
</script>

<template>
  <div class="graph-wrap">
    <VueFlow
      :nodes="nodes"
      :edges="edges"
      :min-zoom="0.2"
      :max-zoom="1.6"
      fit-view-on-init
      :nodes-connectable="false"
      :nodes-draggable="true"
    >
      <template #node-agent="{ data }">
        <div class="agent-node" :class="{ 'is-entry': data.isEntry }">
          <div class="node-name">{{ data.name }}</div>
          <div class="node-id">{{ data.id }}</div>
          <div class="node-resp">{{ data.responsibility }}</div>
          <div v-if="data.tools.length || data.skills.length" class="node-tools">
            <span v-for="t in data.tools" :key="t">{{ t }}</span>
            <span v-for="s in data.skills" :key="s">{{ s }}</span>
          </div>
        </div>
      </template>
      <Background :gap="18" pattern-color="#e9ecf0" />
      <Controls :show-interactive="false" />
      <MiniMap pannable zoomable :height="90" :width="150" />
    </VueFlow>
    <div class="graph-legend">
      <span><i style="background: #0f9960"></i>入口节点</span>
      <span><i style="background: #3b5bdb"></i>处理节点</span>
      <span><i style="background: #b7791f"></i>条件分支边</span>
    </div>
  </div>
</template>
