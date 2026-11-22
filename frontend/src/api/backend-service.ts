import { request } from './client'
import { moduleMeta } from '@/data/modules'
import type { ActionResult, EntryRow, OverviewResult, PageResult } from '@/data/types'

export { moduleMeta }

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

// ===== 审计分发 =====

export type PermissionCheck = {
  name: string
  passed: boolean
  detail: string
}

export type RecipientPermission = {
  subscription_id: number
  application_no: string
  recipient: string
  eligible: boolean
  reasons: string[]
  checks: PermissionCheck[]
  in_scope: boolean
}

export type PermissionsResult = {
  ok: boolean
  audit_id: number
  request_no: string
  api_name: string
  audit_status: string
  recipients: RecipientPermission[]
}

export type PackageColumn = {
  field: string
  class: '共享' | '受控'
  value: string | number | null
  masked: boolean
  mask_algorithm: string
}

export type DistributionPackage = {
  package_id: string
  request_no: string
  subscription_id: number
  application_no: string
  recipient: string
  status: string
  reissued: boolean
  reissue_count: number
  permission: { eligible: boolean; checks: PermissionCheck[]; reasons: string[] }
  scope: { confirmed: boolean; in_scope: boolean }
  reconciliation: {
    request_no: string
    returned_rows: number
    usage_record_no: string | null
    usage_record_count: number
    usage_data_rows: number
    usage_call_count: number
    difference: number
    linked_by: string
  }
  shared_fields: string[]
  controlled_fields: string[]
  withheld_fields: { field: string; reason: string }[]
  columns: PackageColumn[]
  row_count: number
  returned_rows: number
  cross_scope_leaks: unknown[]
}

export type DistributeResult = {
  ok: boolean
  request_no: string
  api_name: string
  returned_rows: number
  shared_numbers: { request_no: string; returned_rows: number }
  packages: DistributionPackage[]
  rejected: { subscription_id: number; application_no: string; recipient: string; reasons: string[] }[]
}

export type CompareResult = {
  ok: boolean
  request_no: string
  api_name: string
  overlap_columns: string[]
  unique_columns: Record<string, string[]>
  reconciliation: {
    same_request_no: boolean
    same_returned_rows: boolean
    request_no: string | null
    returned_rows: number | null
  }
  cross_scope_leaks: { subscription_id: number; field: string; reason: string }[]
  isolated: boolean
  recipients: {
    subscription_id: number
    recipient: string
    eligible: boolean
    in_scope: boolean
    request_no: string
    returned_rows: number
    row_count: number
    usable_columns: string[]
    usable_column_count: number
    controlled_columns: string[]
    withheld_fields: { field: string; reason: string }[]
  }[]
}

export async function loadPermissions(auditId: number): Promise<PermissionsResult> {
  const response = await request(`/api/audit-distribution/audits/${auditId}/permissions`)
  if (!response.ok) throw new Error('订阅权限核对失败')
  return await response.json()
}

export async function distributeAudit(auditId: number, dryRun = false): Promise<DistributeResult> {
  const response = await request('/api/audit-distribution/packages', {
    method: 'POST',
    body: JSON.stringify({ audit_id: auditId, dry_run: dryRun }),
  })
  const payload = await response.json()
  if (!response.ok) throw new Error(payload.detail ?? '分发失败')
  return payload
}

export async function reissuePackage(requestNo: string, subscriptionId: number): Promise<DistributionPackage> {
  const response = await request(
    `/api/audit-distribution/packages/${encodeURIComponent(requestNo)}/${subscriptionId}/reissue`,
    { method: 'POST' },
  )
  const payload = await response.json()
  if (!response.ok) throw new Error(payload.detail ?? '重发失败')
  return payload.package
}

export async function compareSubscriptions(
  requestNo: string,
  subscriptionIds: number[],
): Promise<CompareResult> {
  const response = await request('/api/audit-distribution/compare', {
    method: 'POST',
    body: JSON.stringify({ request_no: requestNo, subscription_ids: subscriptionIds }),
  })
  const payload = await response.json()
  if (!response.ok) throw new Error(payload.detail ?? '对照失败')
  return payload
}
