<script setup lang="ts">
import { computed } from 'vue'
import type { ComponentPlan, ComponentType } from '../types'

const props = defineProps<{ plan: ComponentPlan }>()

const TYPE_LABEL: Record<ComponentType, string> = {
  prompt: 'Prompt 提示词',
  skill: 'Skill 技能',
  tool: 'Tool 工具',
  mcp: 'MCP 服务',
  harness: 'Harness 运行框架',
  memory: 'Memory 记忆',
  rag: 'RAG 知识检索',
}

const ORDER: ComponentType[] = [
  'prompt',
  'skill',
  'tool',
  'mcp',
  'harness',
  'memory',
  'rag',
]

const groups = computed(() =>
  ORDER.map((type) => ({
    type,
    label: TYPE_LABEL[type],
    items: props.plan.components.filter((c) => c.type === type),
  })).filter((g) => g.items.length),
)

function formatInterface(iface: Record<string, unknown>): string {
  return Object.entries(iface)
    .map(([k, v]) => {
      const val =
        typeof v === 'object' && v !== null
          ? JSON.stringify(v, null, 0)
          : String(v)
      return `${k}: ${val.length > 90 ? val.slice(0, 90) + '…' : val}`
    })
    .join('\n')
}
</script>

<template>
  <div class="card">
    <div class="card-head">
      <div class="card-title">组件设计说明</div>
      <span class="tag">{{ plan.components.length }} 个组件</span>
    </div>
    <div class="card-sub">{{ plan.summary }}</div>
    <div v-if="plan.notes.length" style="margin-top: 10px">
      <div v-for="n in plan.notes" :key="n" class="list-item">
        <span class="bullet">!</span><span>{{ n }}</span>
      </div>
    </div>
  </div>

  <div v-for="g in groups" :key="g.type" class="type-group">
    <div class="type-group-head">
      <span class="tag blue">{{ g.label }}</span>
      <span class="count">{{ g.items.length }} 个</span>
    </div>

    <div
      v-for="c in g.items"
      :key="c.name"
      class="comp-row"
      :class="{ optional: c.priority === 'optional' }"
    >
      <div class="comp-top">
        <span class="comp-name">{{ c.name }}</span>
        <span class="tag" :class="c.priority === 'required' ? 'green' : ''">
          {{ c.priority === 'required' ? '必需' : '可选' }}
        </span>
      </div>
      <div class="comp-purpose">{{ c.purpose }}</div>

      <div v-if="Object.keys(c.interface).length" class="iface">
        {{ formatInterface(c.interface) }}
      </div>

      <div class="comp-meta">
        <span v-for="o in c.owner_agents" :key="`o-${o}`">@{{ o }}</span>
        <span v-for="d in c.depends_on" :key="`d-${d}`">dep: {{ d }}</span>
      </div>

      <div v-if="c.test_requirements.length" style="margin-top: 7px">
        <div
          v-for="t in c.test_requirements"
          :key="t"
          class="list-item"
          style="font-size: 12px; color: var(--text-2)"
        >
          <span class="bullet">✓</span><span>{{ t }}</span>
        </div>
      </div>
    </div>
  </div>
</template>
