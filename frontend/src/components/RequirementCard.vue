<script setup lang="ts">
import type { RequirementSpec } from '../types'

defineProps<{ spec: RequirementSpec }>()

const lists: { key: keyof RequirementSpec; label: string }[] = [
  { key: 'inputs', label: '输入' },
  { key: 'outputs', label: '输出' },
  { key: 'features', label: '功能点' },
  { key: 'constraints', label: '约束' },
  { key: 'quality_requirements', label: '质量要求' },
  { key: 'external_dependencies', label: '外部依赖' },
  { key: 'acceptance_criteria', label: '验收标准' },
]
</script>

<template>
  <div class="card">
    <div class="card-head">
      <div>
        <div class="card-title">{{ spec.agent_name }}</div>
        <div class="card-sub">{{ spec.goal }}</div>
      </div>
      <div style="text-align: right; flex-shrink: 0">
        <div style="font-size: 12px; color: var(--text-3)">置信度</div>
        <div style="display: flex; align-items: center; gap: 6px; margin-top: 4px">
          <div class="score-bar" style="width: 60px">
            <i :style="{ width: `${Math.round(spec.confidence * 100)}%` }"></i>
          </div>
          <span style="font-size: 12px; font-variant-numeric: tabular-nums">
            {{ Math.round(spec.confidence * 100) }}%
          </span>
        </div>
      </div>
    </div>

    <div class="kv-row">
      <div class="kv-key">架构倾向</div>
      <div class="kv-val">
        <span class="tag blue">{{ spec.architecture_hint }}</span>
        <span class="tag" style="margin-left: 6px">复杂度 {{ spec.complexity_level }}</span>
      </div>
    </div>

    <div v-if="spec.target_users.length" class="kv-row">
      <div class="kv-key">目标用户</div>
      <div class="kv-val">
        <div class="tag-list">
          <span v-for="u in spec.target_users" :key="u" class="tag">{{ u }}</span>
        </div>
      </div>
    </div>

    <div v-for="row in lists" :key="row.key" class="kv-row">
      <div class="kv-key">{{ row.label }}</div>
      <div class="kv-val">
        <div
          v-if="!(spec[row.key] as string[]).length"
          style="color: var(--text-3); font-size: 12.5px"
        >
          未提供
        </div>
        <div v-else class="tag-list">
          <span v-for="item in (spec[row.key] as string[])" :key="item" class="tag">
            {{ item }}
          </span>
        </div>
      </div>
    </div>
  </div>

  <div v-if="spec.clarifying_questions.length" class="card">
    <div class="card-head">
      <div class="card-title">
        待澄清问题
        <span class="tag amber" style="margin-left: 6px">
          {{ spec.clarifying_questions.filter((q) => q.blocking).length }} 个阻塞
        </span>
      </div>
      <div class="card-sub">标注为阻塞的问题会影响架构选型，建议先确认</div>
    </div>

    <div
      v-for="q in spec.clarifying_questions"
      :key="q.id"
      class="question"
      :class="{ blocking: q.blocking }"
    >
      <div class="q-head">
        <span v-if="q.blocking" class="tag amber" style="flex-shrink: 0">阻塞</span>
        <span>{{ q.question }}</span>
      </div>
      <div class="q-why">{{ q.why }}</div>
      <div v-if="q.options.length" class="q-options">
        <span v-for="o in q.options" :key="o" class="tag">{{ o }}</span>
      </div>
    </div>
  </div>
</template>
