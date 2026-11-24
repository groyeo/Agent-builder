<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import type { JobEvent, StepState } from '../types'

const props = defineProps<{
  steps: StepState[]
  events: JobEvent[]
  running: boolean
  elapsed: number
  startedAt: number
}>()

const box = ref<HTMLElement | null>(null)
const showAll = ref(false)

const visibleEvents = computed(() =>
  showAll.value ? props.events : props.events.slice(-60),
)

const STEP_LABEL: Record<string, string> = {
  requirement: '需求分析',
  architecture: '架构规划',
  components: '组件设计',
}

// 新事件到达时贴底，除非用户上滚查看历史
watch(
  () => props.events.length,
  async () => {
    await nextTick()
    const el = box.value
    if (!el) return
    const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 80
    if (nearBottom) el.scrollTop = el.scrollHeight
  },
)

function fmtClock(ts: number, base: number): string {
  const d = Math.max(0, ts - base)
  const m = Math.floor(d / 60)
  const s = d % 60
  return `${String(m).padStart(2, '0')}:${s.toFixed(0).padStart(2, '0')}`
}
</script>

<template>
  <div class="run-panel">
    <div class="run-head">
      <span class="run-title">执行过程</span>
      <span class="run-elapsed">
        {{ elapsed.toFixed(1) }}s
        <span v-if="running" class="live-dot"></span>
      </span>
    </div>

    <div class="run-steps">
      <div
        v-for="s in steps"
        :key="s.key"
        class="run-step"
        :class="s.status"
      >
        <span class="rs-marker">
          <template v-if="s.status === 'done'">✓</template>
          <template v-else-if="s.status === 'failed'">✕</template>
          <template v-else-if="s.status === 'running'">●</template>
        </span>
        <span class="rs-label">{{ s.label }}</span>
        <span v-if="s.attempts > 1" class="rs-badge" title="模型调用次数">
          ×{{ s.attempts }}
        </span>
        <span class="rs-dur">
          {{ s.duration !== null ? `${s.duration}s` : s.status === 'running' ? '…' : '' }}
        </span>
      </div>
    </div>

    <div v-if="events.length" ref="box" class="run-log">
      <div
        v-for="e in visibleEvents"
        :key="e.seq"
        class="log-line"
        :class="e.level"
      >
        <span class="log-time">{{ fmtClock(e.ts, startedAt) }}</span>
        <span v-if="e.step" class="log-step">{{ STEP_LABEL[e.step] ?? e.step }}</span>
        <span class="log-msg">{{ e.message }}</span>
      </div>
      <button
        v-if="!showAll && events.length > 60"
        class="log-more"
        @click="showAll = true"
      >
        显示全部 {{ events.length }} 条
      </button>
    </div>
  </div>
</template>
