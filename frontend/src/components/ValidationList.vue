<script setup lang="ts">
import type { ValidationReport } from '../types'

defineProps<{ report: ValidationReport; title?: string }>()
</script>

<template>
  <div class="card">
    <div class="card-head">
      <div class="card-title">{{ title ?? '校验报告' }}</div>
      <span
        class="tag"
        :class="report.passed ? 'green' : 'red'"
      >
        {{ report.passed ? '通过' : '未通过' }}
      </span>
    </div>

    <div v-if="!report.issues.length" class="card-sub">
      没有发现问题。结构、接口与需求一致性检查均通过。
    </div>

    <div
      v-for="(issue, idx) in report.issues"
      :key="`${issue.code}-${idx}`"
      class="issue"
      :class="issue.level"
    >
      <span class="issue-code">{{ issue.code }}</span>
      <span>
        {{ issue.message }}
        <span v-if="issue.target" class="issue-target">@{{ issue.target }}</span>
      </span>
    </div>
  </div>
</template>
