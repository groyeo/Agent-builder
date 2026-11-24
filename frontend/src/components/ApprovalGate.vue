<script setup lang="ts">
/** 架构审批闸门：流水线停在 waiting_approval 时，先把方案摊开让人判断再往下走。 */
import { ref } from 'vue'
import type { ApprovalGate } from '../types'

const props = defineProps<{
  gate: ApprovalGate
  submitting: boolean
}>()

const emit = defineEmits<{
  approve: [note: string]
  reject: [note: string]
}>()

const note = ref('')

const TYPE_LABEL: Record<string, string> = {
  single_agent: 'Single Agent',
  multi_agent: 'Multi-Agent',
  workflow: 'Workflow',
  hybrid: 'Hybrid',
}

const LEVEL_LABEL: Record<string, string> = {
  simple: '简单',
  moderate: '中等',
  complex: '复杂',
}

function approve() {
  emit('approve', note.value.trim())
}

function reject() {
  emit('reject', note.value.trim())
}
</script>

<template>
  <div class="approval-gate">
    <div class="gate-head">
      <div>
        <div class="gate-title">架构方案待确认</div>
        <div class="gate-sub">
          还没生成任何文件。确认后从这里继续；驳回则本次构建结束（不产生半成品）。
        </div>
      </div>
      <span class="gate-badge">停下等你</span>
    </div>

    <div class="gate-meta">
      <span class="chip">{{ TYPE_LABEL[gate.summary.architecture_type] || gate.summary.architecture_type }}</span>
      <span class="chip">复杂度 {{ LEVEL_LABEL[gate.summary.complexity_level] || gate.summary.complexity_level }}</span>
      <span class="chip">置信度 {{ Math.round(gate.summary.confidence * 100) }}%</span>
      <span class="chip">{{ gate.summary.agents.length }} 个节点</span>
      <span class="chip">{{ gate.summary.edges.length }} 条流转</span>
    </div>

    <p class="gate-reason">{{ gate.summary.reasoning_summary }}</p>

    <div class="gate-cols">
      <div class="gate-col">
        <div class="gate-col-title">节点分工</div>
        <div v-for="a in gate.summary.agents" :key="a.id" class="gate-node">
          <div class="gate-node-name">{{ a.name }}<code>{{ a.id }}</code></div>
          <div class="gate-node-resp">{{ a.responsibility }}</div>
        </div>
      </div>
      <div class="gate-col">
        <div class="gate-col-title">要生成的组件</div>
        <div class="gate-tags">
          <span v-for="c in gate.summary.required_components" :key="c" class="tag">{{ c }}</span>
          <span v-for="c in gate.summary.optional_components" :key="c" class="tag muted">
            {{ c }}（可选）
          </span>
          <span v-if="!gate.summary.required_components.length" class="card-sub">无</span>
        </div>

        <template v-if="gate.summary.risks.length">
          <div class="gate-col-title" style="margin-top: 12px">已知风险</div>
          <ul class="gate-risks">
            <li v-for="r in gate.summary.risks" :key="r">{{ r }}</li>
          </ul>
        </template>
      </div>
    </div>

    <div class="gate-actions">
      <input
        v-model="note"
        class="gate-note"
        placeholder="可选的确认/驳回说明（例如：把分类节点拆成两个）"
        @keyup.enter="approve"
      />
      <button class="run-btn" :disabled="submitting" @click="approve">
        {{ submitting ? '处理中…' : '确认，继续生成' }}
      </button>
      <button class="ghost-btn" :disabled="submitting" @click="reject">驳回</button>
    </div>
    <div class="gate-foot">
      确认后不会重跑需求分析与架构 —— 你刚点头的方案不会被模型换成另一份。
    </div>
  </div>
</template>
