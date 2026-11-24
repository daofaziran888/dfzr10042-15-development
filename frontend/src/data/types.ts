/** 纯前端数据层的公共类型：与全栈版后端返回的结构保持一致，换回后端时页面不用改。 */

export type EntryRow = {
  id: number
  status: string
  pending: boolean
  abnormal: boolean
  [field: string]: string | number | boolean
}

export type ModuleMeta = {
  key: string
  name: string
  entity: string
  desc: string
  fields: string[]
  statuses: string[]
  actions: string[]
  actionTargets: Record<string, string>
  metrics: string[]
}

export type PageResult = {
  items: EntryRow[]
  total: number
  page: number
  size: number
}

export type ActionResult = {
  ok: boolean
  message: string
}

export type OverviewResult = {
  cards: { label: string; value: number }[]
  modules: { name: string; created: number; pending: number; abnormal: number }[]
}

export type DashboardWindow = {
  period: string
  window: string
  gross: number
  actual: number
  provisional_actual: number
  value_source: string
  published: boolean
  batch_code: string
  effective_round: number | null
  basis: { formula: string; [key: string]: unknown }
  pending_vouchers: { voucher_id: string; order_id: string; amount: number }[]
  can_publish: boolean
}

export type DashboardActuals = {
  dashboard_id: string
  reference_date: string
  metrics: DashboardWindow[]
  source: string
}
