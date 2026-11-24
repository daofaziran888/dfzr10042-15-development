import { request } from './client'
import { moduleMeta } from '@/data/modules'
import type {
  ActionResult,
  DashboardActuals,
  EntryRow,
  OverviewResult,
  PageResult,
} from '@/data/types'

export { moduleMeta }

export async function loadDashboardActuals(): Promise<DashboardActuals> {
  const response = await request('/api/recalc/dashboard')
  if (!response.ok) throw new Error('看板实际指标金额读取失败')
  return (await response.json()) as DashboardActuals
}

export async function importVouchers(
  items: Record<string, string>[],
): Promise<{ ok: boolean; message: string }> {
  const response = await request('/api/recalc/vouchers/bulk', {
    method: 'POST',
    body: JSON.stringify({ items }),
  })
  return await response.json()
}

export async function runRecalcBatch(payload: {
  batch_code: string
  publish_policy: string
}): Promise<{ ok: boolean; message: string }> {
  const response = await request('/api/recalc/batches', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
  return await response.json()
}

export async function listEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<PageResult> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}?${query}`)
  if (!response.ok) throw new Error('业务数据读取失败')
  return await response.json()
}

export async function createEntry(
  key: string,
  values: Record<string, string>,
): Promise<ActionResult> {
  const response = await request(`/api/${key}`, {
    method: 'POST',
    body: JSON.stringify({ values }),
  })
  return await response.json()
}

export async function runAction(
  key: string,
  id: number,
  action: string,
): Promise<ActionResult> {
  const response = await request(`/api/${key}/${id}/actions`, {
    method: 'POST',
    body: JSON.stringify({ action }),
  })
  return await response.json()
}

export async function downloadEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<void> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}/export?${query}`)
  if (!response.ok) throw new Error('业务数据导出失败')
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `${key}.csv`
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  URL.revokeObjectURL(url)
}

export async function loadOverview(): Promise<OverviewResult> {
  const response = await request('/api/overview')
  if (!response.ok) throw new Error('运营概览读取失败')
  return await response.json()
}
