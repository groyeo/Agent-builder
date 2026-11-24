<script setup lang="ts">
/**
 * 交付清单视图：把"要准备什么、去哪注册、填到哪、怎么启动"讲清楚。
 * 构建页与项目库共用同一个组件，避免两处说法不一致。
 */
import { computed, ref } from 'vue'
import type { DeliveryPlan, ResourceItem } from '../types'

const props = defineProps<{ delivery: DeliveryPlan }>()

const showOptional = ref(true)
const copiedKey = ref('')

const required = computed(() => props.delivery.resources.filter((r) => r.required))
const optional = computed(() => props.delivery.resources.filter((r) => !r.required))

const KIND_LABEL: Record<string, string> = {
  llm: '模型服务',
  api_key: 'API Key',
  credential: '账号凭证',
  knowledge_doc: '知识库文档',
  data_file: '数据文件',
  account: '需要开通的账号',
  infra: '基础设施',
}

function kindClass(kind: string): string {
  if (kind === 'llm') return 'blue'
  if (kind === 'knowledge_doc' || kind === 'data_file') return 'green'
  if (kind === 'infra') return 'gray'
  return 'amber'
}

async function copyCommand(key: string, text: string) {
  try {
    await navigator.clipboard.writeText(text)
    copiedKey.value = key
    window.setTimeout(() => (copiedKey.value = ''), 1500)
  } catch {
    copiedKey.value = ''
  }
}

function openSignup(r: ResourceItem) {
  if (r.signup_url) window.open(r.signup_url, '_blank')
}
</script>

<template>
  <div class="delivery-wrap">
    <div class="delivery-summary">
      <div class="delivery-summary-text">{{ delivery.summary }}</div>
      <div class="delivery-summary-meta">
        <span class="meta-pill">{{ required.length }} 项必需</span>
        <span class="meta-pill">{{ optional.length }} 项可后补</span>
        <span class="meta-pill pre">自检：{{ delivery.check_command }}</span>
      </div>
    </div>

    <div v-if="delivery.blockers.length" class="warn-strip">
      <span>!</span>
      <span>
        这些项没准备好之前跑不起来：{{ delivery.blockers.join('；') }}。
        每项下面都写了去哪里拿。
      </span>
    </div>

    <div class="delivery-section-title">
      必需项
      <span class="hint">缺了就跑不起来，按顺序做</span>
    </div>

    <div
      v-for="(r, i) in required"
      :key="r.key"
      class="res-card"
    >
      <div class="res-head">
        <span class="res-idx">{{ i + 1 }}</span>
        <span class="res-name">{{ r.name }}</span>
        <span class="tag" :class="kindClass(r.kind)">{{ KIND_LABEL[r.kind] ?? r.kind }}</span>
        <span style="flex: 1"></span>
        <button v-if="r.signup_url" class="btn-primary res-btn" @click="openSignup(r)">
          去注册 / 获取
        </button>
      </div>

      <div class="res-why">{{ r.why }}</div>

      <div class="res-row">
        <span class="res-k">放到哪</span>
        <span class="res-v">{{ r.target }}</span>
      </div>
      <div v-if="r.env_vars.length" class="res-row">
        <span class="res-k">变量名</span>
        <span class="res-v">
          <code v-for="v in r.env_vars" :key="v" class="env-chip">{{ v }}</code>
        </span>
      </div>

      <div class="res-steps">
        <div class="res-k" style="margin-bottom: 4px">怎么拿到</div>
        <ol>
          <li v-for="(s, si) in r.steps" :key="si">{{ s }}</li>
        </ol>
      </div>

      <div class="res-foot">
        <span v-if="r.cost_hint"><strong>费用</strong>：{{ r.cost_hint }}</span>
        <span v-if="r.fallback"><strong>没配会怎样</strong>：{{ r.fallback }}</span>
        <span v-if="r.example" class="res-example">示例：{{ r.example }}</span>
      </div>
    </div>

    <template v-if="optional.length">
      <div class="delivery-section-title" style="cursor: pointer" @click="showOptional = !showOptional">
        可后补项
        <span class="hint">先跑通主干，再回来补这些 · 点击{{ showOptional ? '收起' : '展开' }}</span>
      </div>

      <template v-if="showOptional">
        <div v-for="r in optional" :key="r.key" class="res-card optional">
          <div class="res-head">
            <span class="res-idx muted">·</span>
            <span class="res-name">{{ r.name }}</span>
            <span class="tag" :class="kindClass(r.kind)">{{ KIND_LABEL[r.kind] ?? r.kind }}</span>
            <span style="flex: 1"></span>
            <button v-if="r.signup_url" class="ghost-btn" @click="openSignup(r)">获取入口</button>
          </div>
          <div class="res-why">{{ r.why }}</div>
          <div class="res-row">
            <span class="res-k">放到哪</span>
            <span class="res-v">
              {{ r.target }}
              <code v-for="v in r.env_vars" :key="v" class="env-chip">{{ v }}</code>
            </span>
          </div>
          <div class="res-foot">
            <span v-if="r.fallback"><strong>没配会怎样</strong>：{{ r.fallback }}</span>
          </div>
        </div>
      </template>
    </template>

    <div class="delivery-section-title">
      从零启动
      <span class="hint">照着敲，最后一步是接真实 API</span>
    </div>

    <div class="startup-list">
      <div v-for="s in delivery.startup_steps" :key="s.order" class="startup-item">
        <span class="startup-idx">{{ s.order }}</span>
        <div class="startup-body">
          <div class="startup-title">{{ s.title }}</div>
          <div v-if="s.command" class="startup-cmd">
            <code>{{ s.command }}</code>
            <button class="copy-btn" @click="copyCommand(`cmd-${s.order}`, s.command!)">
              {{ copiedKey === `cmd-${s.order}` ? '已复制' : '复制' }}
            </button>
          </div>
          <div v-if="s.detail" class="startup-detail">{{ s.detail }}</div>
        </div>
      </div>
    </div>
  </div>
</template>
