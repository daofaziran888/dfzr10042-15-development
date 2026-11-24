<template>
  <section class="page" :data-module="meta.key">
    <header class="page-head">
      <div>
        <h2>{{ meta.name }}</h2>
        <p class="page-desc">{{ meta.desc }}</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">
          登记{{ meta.entity }}
        </button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">
          导出{{ meta.name }}清单
        </button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <p class="status-legend">
      <span v-for="item in statusSummary" :key="item.status" class="legend-item">
        {{ item.status }}：{{ item.count }}
      </span>
    </p>

    <section
      v-if="meta.key === 'refund_voucher' || meta.key === 'metric_recalc_batch'"
      class="recalc-panel"
      data-testid="recalc-panel"
    >
      <header class="recalc-head">
        <h3>日 / 周实际指标金额</h3>
        <span class="recalc-note">{{ meta.key === 'metric_recalc_batch' ? '看板聚合读取批次里各窗口的完整事实' : '按原订单关联日、周两个窗口，先校验凭据资格' }}</span>
      </header>
      <div class="recalc-cards">
        <article v-for="metric in dashboard?.metrics ?? []" :key="metric.window" class="recalc-card" :data-testid="`dash-${metric.period}`">
          <span class="stat-label">{{ metric.period }}额 · {{ metric.window }}</span>
          <strong class="stat-value">{{ metric.actual.toFixed(2) }}</strong>
          <span class="recalc-meta">
            原始订单 {{ metric.gross.toFixed(2) }} ·
            {{ publishedLabel(metric) }}
          </span>
          <code class="recalc-formula">{{ metric.basis?.formula }}</code>
        </article>
      </div>
      <p v-if="dashboard" class="recalc-meta">
        数据来源：{{ dashboard.source }}；参考日 {{ dashboard.reference_date }}
      </p>
      <p v-if="dashboardError" class="error-text">{{ dashboardError }}</p>
    </section>

    <form
      v-if="meta.key === 'refund_voucher'"
      class="recalc-form"
      data-testid="voucher-bulk-form"
      @submit.prevent="submitBulk"
    >
      <label v-for="field in voucherDraftFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="voucherDraft[field]" :data-testid="`bulk-${field}`" :placeholder="`填写${field}`" />
      </label>
      <button class="btn" data-testid="bulk-add" type="button" @click="addVoucherDraft">加入批量</button>
      <button class="btn primary" data-testid="bulk-submit" type="submit">批量录入</button>
      <p v-if="bulkMessage" class="recalc-meta" data-testid="bulk-message">{{ bulkMessage }}</p>
    </form>

    <form
      v-if="meta.key === 'metric_recalc_batch'"
      class="recalc-form"
      data-testid="batch-run-form"
      @submit.prevent="submitBatch"
    >
      <label class="filter-item">
        <span>批次编号</span>
        <input v-model="batchDraft.batch_code" data-testid="batch-code" placeholder="如 RCB-20261124-01" />
      </label>
      <label class="filter-item">
        <span>发布策略</span>
        <select v-model="batchDraft.publish_policy" data-testid="batch-policy">
          <option value="局部发布">局部发布</option>
          <option value="两周期一起发布">两周期一起发布</option>
        </select>
      </label>
      <button class="btn primary" data-testid="batch-submit" type="submit">执行重算批次</button>
      <p v-if="batchMessage" class="recalc-meta" data-testid="batch-message">{{ batchMessage }}</p>
    </form>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :data-testid="`filter-${field}`" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" data-testid="query" type="submit">查询</button>
      <button class="btn ghost" data-testid="reset" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in meta.fields" :key="column">{{ column }}</th>
          <th>当前状态</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :data-testid="`row-${row.id}`" :data-status="String(row.status)">
          <td v-for="column in meta.fields" :key="column">{{ row[column] ?? '—' }}</td>
          <td>{{ row.status }}</td>
          <td class="row-actions">
            <button
              v-for="action in meta.actions"
              :key="action"
              class="link"
              type="button"
              :data-testid="`action-${row.id}-${action}`"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="meta.fields.length + 2" class="empty-state">
            暂无{{ meta.name }}数据，可先登记{{ meta.entity }}
          </td>
        </tr>
      </tbody>
    </table>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记{{ meta.entity }}</h3>
        <label v-for="field in meta.fields" :key="field" class="create-field">
          <span>{{ field }}</span>
          <input v-model="createValues[field]" :data-testid="`create-${field}`" required />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="showCreate = false">取消</button>
          <button class="btn primary" data-testid="submit-create" type="submit" :disabled="creating">
            {{ creating ? '提交中…' : '确认登记' }}
          </button>
        </div>
      </form>
    </div>

    <footer class="page-foot">
      <span>共 {{ total }} 条{{ meta.name }}记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import {
  createEntry,
  downloadEntries,
  importVouchers,
  listEntries,
  loadDashboardActuals,
  moduleMeta,
  runRecalcBatch,
  runAction as applyAction,
} from '@/api/backend-service'
import type { DashboardActuals, EntryRow } from '@/data/types'

const props = defineProps<{ moduleKey: string }>()
const meta = moduleMeta(props.moduleKey)
const rows = ref<EntryRow[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = meta.fields.slice(0, 3)
const showCreate = ref(false)
const creating = ref(false)
const createValues = ref<Record<string, string>>({})
const dashboard = ref<DashboardActuals | null>(null)
const dashboardError = ref('')
const voucherDraftFields = ["凭据编号", "原订单编号", "退款金额", "凭据来源", "核验状态"]
const voucherDraft = ref<Record<string, string>>(
  Object.fromEntries(voucherDraftFields.map((field) => [field, ''])),
)
const voucherQueue = ref<Record<string, string>[]>([])
const bulkMessage = ref('')
const batchDraft = ref({ batch_code: '', publish_policy: '局部发布' })
const batchMessage = ref('')

function publishedLabel(metric: { published: boolean; batch_code: string; effective_round: number | null }) {
  if (!metric.published) return '未发布，展示原始金额'
  return `${metric.batch_code || '批次'} 第${metric.effective_round}轮生效`
}
const stats = computed(() => [
  { label: meta.metrics[0], value: total.value },
  { label: meta.metrics[1], value: rows.value.filter((row) => row.status === meta.statuses[1]).length },
  {
    label: meta.metrics[2],
    value: rows.value.filter((row) => row.status === meta.statuses[meta.statuses.length - 1]).length,
  },
])
const statusSummary = computed(() =>
  meta.statuses.map((status) => ({
    status,
    count: rows.value.filter((row) => String(row.status) === status).length,
  })),
)

function resetFilters() {
  filters.value = {}
  void reload()
}

async function exportRows() {
  errorMessage.value = ''
  try {
    await downloadEntries(meta.key, filters.value)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出失败'
  }
}

function openCreate() {
  createValues.value = Object.fromEntries(meta.fields.map((field) => [field, '']))
  errorMessage.value = ''
  showCreate.value = true
}

async function submitCreate() {
  creating.value = true
  errorMessage.value = ''
  try {
    const result = await createEntry(meta.key, createValues.value)
    if (!result.ok) throw new Error(result.message)
    showCreate.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '登记失败'
  } finally {
    creating.value = false
  }
}

async function runAction(action: string, row: EntryRow) {
  errorMessage.value = ''
  try {
    const result = await applyAction(meta.key, Number(row.id), action)
    if (!result.ok) throw new Error(result.message)
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  try {
    const payload = await listEntries(meta.key, filters.value)
    rows.value = payload.items
    total.value = payload.total
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '业务列表读取失败'
  }
  if (meta.key === 'refund_voucher' || meta.key === 'metric_recalc_batch') {
    await loadDashboard()
  }
}

async function loadDashboard() {
  dashboardError.value = ''
  try {
    dashboard.value = await loadDashboardActuals()
  } catch (error) {
    dashboardError.value = error instanceof Error ? error.message : '看板金额读取失败'
  }
}

function addVoucherDraft() {
  const entry = { ...voucherDraft.value }
  if (!entry["凭据编号"] && !entry["原订单编号"]) {
    bulkMessage.value = '请至少填写凭据编号和原订单编号'
    return
  }
  voucherQueue.value.push(entry)
  voucherDraft.value = Object.fromEntries(voucherDraftFields.map((field) => [field, '']))
  bulkMessage.value = `已加入 ${voucherQueue.value.length} 条，点击批量录入提交`
}

async function submitBulk() {
  if (!voucherQueue.value.length) {
    bulkMessage.value = '请先把凭据加入批量'
    return
  }
  try {
    const result = await importVouchers(voucherQueue.value)
    bulkMessage.value = result.message
    if (result.ok) voucherQueue.value = []
    await reload()
  } catch (error) {
    bulkMessage.value = error instanceof Error ? error.message : '批量录入失败'
  }
}

async function submitBatch() {
  if (!batchDraft.value.batch_code.trim()) {
    batchMessage.value = '批次编号不能为空'
    return
  }
  try {
    const result = await runRecalcBatch({ ...batchDraft.value })
    batchMessage.value = result.message
    await reload()
  } catch (error) {
    batchMessage.value = error instanceof Error ? error.message : '批次执行失败'
  }
}

onMounted(reload)
</script>
