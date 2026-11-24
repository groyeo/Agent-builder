<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import type { ClarifyingQuestion } from '../types'

const props = defineProps<{
  questions: ClarifyingQuestion[]
  submitting: boolean
}>()

const emit = defineEmits<{
  submit: [answers: Record<string, string>]
  skip: []
}>()

const draft = ref<Record<string, string>>({})

// 新一轮问题到来时重置草稿，避免把上一轮的答案串到新问题上
watch(
  () => props.questions.map((q) => q.id).join(','),
  () => {
    draft.value = {}
  },
)

const answeredCount = computed(
  () => props.questions.filter((q) => (draft.value[q.id] ?? '').trim()).length,
)

function pick(q: ClarifyingQuestion, option: string) {
  draft.value[q.id] = option
}

function submit() {
  const answers: Record<string, string> = {}
  for (const q of props.questions) {
    const v = (draft.value[q.id] ?? '').trim()
    if (v) answers[q.id] = v
  }
  emit('submit', answers)
}
</script>

<template>
  <div class="clarify-card">
    <div class="clarify-head">
      <div>
        <div class="clarify-title">需要你确认 {{ questions.length }} 个问题</div>
        <div class="clarify-sub">
          这些问题会直接改变架构选型（比如是否需要 MCP、输出是否要写回外部系统）。
          回答后需求会被重新结构化，而不是在旧结论上打补丁。
        </div>
      </div>
      <span class="tag amber">已答 {{ answeredCount }}/{{ questions.length }}</span>
    </div>

    <div class="clarify-list">
      <div v-for="q in questions" :key="q.id" class="clarify-item">
        <div class="cq-q">
          <span class="cq-id">{{ q.id }}</span>
          {{ q.question }}
        </div>
        <div class="cq-why">{{ q.why }}</div>
        <div v-if="q.options.length" class="cq-options">
          <span
            v-for="opt in q.options"
            :key="opt"
            class="cq-option"
            :class="{ picked: draft[q.id] === opt }"
            @click="pick(q, opt)"
          >
            {{ opt }}
          </span>
        </div>
        <input
          v-model="draft[q.id]"
          class="cq-input"
          :placeholder="q.options.length ? '或自行补充更准确的答案' : '输入你的回答'"
        />
      </div>
    </div>

    <div class="clarify-actions">
      <button class="btn-primary" :disabled="submitting" @click="submit">
        {{ submitting ? '提交中…' : '提交并继续构建' }}
      </button>
      <button class="btn-ghost" :disabled="submitting" @click="emit('skip')">
        跳过，按当前信息继续
      </button>
      <span class="clarify-hint">
        跳过时会带着未确认的假设往下跑，结果里会标注"仍有 N 个关键问题未澄清"。
      </span>
    </div>
  </div>
</template>
