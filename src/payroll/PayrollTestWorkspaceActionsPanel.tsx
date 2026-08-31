import { useMemo, useState } from 'react'

import { api, ApiError } from '../api'
import type {
  PayrollTestBatchValidationResult,
  PayrollTestMaterialPreview,
  PayrollTestMaterialType,
  PayrollTestWorkspaceMaterial,
  PayrollTestWorkspaceReadResponse,
} from '../types'
import './payroll-test-workspace.css'

const MATERIAL_TYPES: ReadonlyArray<{ value: PayrollTestMaterialType; label: string }> = [
  { value: 'PAYROLL_SHEET', label: '工资明细表' },
  { value: 'RELEASE_LIST', label: '发放名单' },
  { value: 'CASH_LIST', label: '现金发放表' },
  { value: 'ATTENDANCE_SHEET', label: '考勤表' },
  { value: 'ADJUSTMENT_SOURCE', label: '调整依据' },
  { value: 'PAYROLL_SUMMARY', label: '工资汇总' },
  { value: 'SUPPORTING_SCAN', label: '辅助扫描件' },
  { value: 'BACKUP', label: '备份材料' },
  { value: 'OBSOLETE', label: '废弃材料' },
]

const PAGE_SIZE = 25

type Filter = 'ALL' | 'DATE_UNKNOWN' | 'AUTO_TEST' | 'REVIEW_REQUIRED'

type Props = {
  workspace: PayrollTestWorkspaceReadResponse
  csrfToken: string
  onWorkspaceChange: (workspace: PayrollTestWorkspaceReadResponse) => void
}

type Draft = {
  period: string
  materialType: PayrollTestMaterialType
}

function defaultDraft(material: PayrollTestWorkspaceMaterial): Draft {
  return {
    period: material.period ?? '',
    materialType: MATERIAL_TYPES.some(({ value }) => value === material.material_type)
      ? material.material_type as PayrollTestMaterialType
      : 'PAYROLL_SHEET',
  }
}

function materialTypeLabel(value: string | null): string {
  return MATERIAL_TYPES.find((item) => item.value === value)?.label ?? '类型待确认'
}

function routingLabel(value: PayrollTestWorkspaceMaterial['routing_status']): string {
  if (value === 'AUTO_TEST') return '已进入测试账本'
  if (value === 'REVIEW_REQUIRED') return '9 月后待审核'
  return '日期待确认'
}

function errorLabel(error: unknown): string {
  if (error instanceof ApiError && error.status === 409) return '材料已被更新，已为你刷新，请重新选择。'
  if (error instanceof ApiError && error.status === 422) return '期间或材料类型不符合要求。'
  return '操作没有完成，请稍后重试。'
}

export function PayrollTestWorkspaceActionsPanel({
  workspace,
  csrfToken,
  onWorkspaceChange,
}: Props) {
  const [filter, setFilter] = useState<Filter>(
    workspace.data.routing_counts.date_unknown > 0 ? 'DATE_UNKNOWN' : 'ALL',
  )
  const [page, setPage] = useState(0)
  const [editingId, setEditingId] = useState<string | null>(null)
  const [draft, setDraft] = useState<Draft | null>(null)
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState<{ tone: 'success' | 'error'; text: string } | null>(null)
  const [validation, setValidation] = useState<PayrollTestBatchValidationResult | null>(null)
  const [preview, setPreview] = useState<PayrollTestMaterialPreview | null>(null)

  const filtered = useMemo(
    () => workspace.data.materials.filter(
      (material) => filter === 'ALL' || material.routing_status === filter,
    ),
    [filter, workspace.data.materials],
  )
  const pageCount = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE))
  const safePage = Math.min(page, pageCount - 1)
  const visible = filtered.slice(safePage * PAGE_SIZE, (safePage + 1) * PAGE_SIZE)

  const changeFilter = (next: Filter) => {
    setFilter(next)
    setPage(0)
  }

  const refresh = async () => {
    const next = await api.getPayrollTestWorkspace()
    onWorkspaceChange(next)
    return next
  }

  const startEditing = (material: PayrollTestWorkspaceMaterial) => {
    setEditingId(material.material_id)
    setDraft(defaultDraft(material))
    setMessage(null)
  }

  const organize = async () => {
    if (!editingId || !draft || !draft.period || busy) return
    setBusy(true)
    setMessage(null)
    try {
      const result = await api.organizePayrollTestMaterial({
        materialId: editingId,
        expectedWorkspaceRevision: workspace.data.workspace_revision,
        period: draft.period,
        materialType: draft.materialType,
        csrfToken,
      })
      await refresh()
      setFilter(result.data.material.routing_status)
      setPage(0)
      setEditingId(null)
      setDraft(null)
      setMessage({ tone: 'success', text: '材料已归类，测试账本已刷新。' })
    } catch (error) {
      try {
        await refresh()
      } catch {
        // Keep the original operation error visible when the refresh is also unavailable.
      }
      setMessage({ tone: 'error', text: errorLabel(error) })
    } finally {
      setBusy(false)
    }
  }

  const validate = async () => {
    if (busy) return
    setBusy(true)
    setMessage(null)
    try {
      const result = await api.validatePayrollTestWorkspace({
        expectedWorkspaceRevision: workspace.data.workspace_revision,
        csrfToken,
      })
      setValidation(result.data)
      setMessage({ tone: 'success', text: `已生成 ${result.data.ready_batch_count} 个可测试批次。` })
    } catch (error) {
      setMessage({ tone: 'error', text: errorLabel(error) })
    } finally {
      setBusy(false)
    }
  }

  const previewMaterial = async (materialId: string) => {
    if (busy) return
    setBusy(true)
    setMessage(null)
    try {
      const result = await api.previewPayrollTestMaterial(materialId)
      setPreview(result.data)
    } catch {
      setMessage({ tone: 'error', text: '工资表内容暂时无法解析，请确认文件格式和归属信息。' })
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="payroll-test-actions" aria-labelledby="payroll-test-actions-heading">
      <header>
        <div>
          <span className="payroll-test-eyebrow">测试数据，不会发薪</span>
          <h2 id="payroll-test-actions-heading">整理历史工资材料</h2>
          <p>先确认日期和材料类型，再按月份生成测试批次；所有结果均不可付款。</p>
        </div>
        <button className="payroll-test-primary" type="button" disabled={busy} onClick={validate}>
          {busy ? '处理中…' : '生成并验证测试批次'}
        </button>
      </header>

      <div className="payroll-test-summary" aria-label="材料状态汇总">
        <button type="button" data-active={filter === 'ALL'} onClick={() => changeFilter('ALL')}>
          全部 <strong>{workspace.data.materials.length}</strong>
        </button>
        <button type="button" data-active={filter === 'DATE_UNKNOWN'} onClick={() => changeFilter('DATE_UNKNOWN')}>
          日期待确认 <strong>{workspace.data.routing_counts.date_unknown}</strong>
        </button>
        <button type="button" data-active={filter === 'AUTO_TEST'} onClick={() => changeFilter('AUTO_TEST')}>
          8 月及以前 <strong>{workspace.data.routing_counts.auto_test}</strong>
        </button>
        <button type="button" data-active={filter === 'REVIEW_REQUIRED'} onClick={() => changeFilter('REVIEW_REQUIRED')}>
          9 月后待审核 <strong>{workspace.data.routing_counts.review_required}</strong>
        </button>
      </div>

      {message ? <div className={`payroll-test-message ${message.tone}`} role="status">{message.text}</div> : null}

      <div className="payroll-test-materials" aria-label="历史工资材料">
        {visible.map((material) => {
          const editing = editingId === material.material_id && draft !== null
          return (
            <article key={material.material_id}>
              <div className="payroll-test-material-main">
                <strong>材料 {material.material_id.slice(-8).toUpperCase()}</strong>
                <span>{material.period ?? '日期待确认'} · {materialTypeLabel(material.material_type)}</span>
              </div>
              {editing ? (
                <div className="payroll-test-editor">
                  <label>
                    归属月份
                    <input
                      type="month"
                      value={draft.period}
                      onChange={(event) => setDraft({ ...draft, period: event.target.value })}
                    />
                  </label>
                  <label>
                    材料类型
                    <select
                      value={draft.materialType}
                      onChange={(event) => setDraft({
                        ...draft,
                        materialType: event.target.value as PayrollTestMaterialType,
                      })}
                    >
                      {MATERIAL_TYPES.map((item) => (
                        <option key={item.value} value={item.value}>{item.label}</option>
                      ))}
                    </select>
                  </label>
                  <button type="button" disabled={busy || !draft.period} onClick={organize}>确认归类</button>
                  <button type="button" disabled={busy} onClick={() => setEditingId(null)}>取消</button>
                </div>
              ) : (
                <div className="payroll-test-material-actions">
                  <span data-routing={material.routing_status}>{routingLabel(material.routing_status)}</span>
                  {material.routing_status !== 'DATE_UNKNOWN' && material.material_type === 'PAYROLL_SHEET' ? (
                    <button type="button" disabled={busy} onClick={() => previewMaterial(material.material_id)}>
                      查看工资明细
                    </button>
                  ) : null}
                  <button type="button" disabled={busy} onClick={() => startEditing(material)}>
                    {material.routing_status === 'DATE_UNKNOWN' ? '归类' : '调整'}
                  </button>
                </div>
              )}
              {preview?.material_id === material.material_id ? (
                <div className="payroll-test-preview" aria-label="工资表解析预览">
                  <span className="payroll-test-preview-boundary">只读预览 · 不可付款</span>
                  <div className="payroll-test-preview-summary">
                    <strong>{preview.period} · {preview.line_count} 人</strong>
                    <span>实发合计 ¥{(preview.total_net_pay_cents / 100).toLocaleString('zh-CN', {
                      minimumFractionDigits: 2,
                      maximumFractionDigits: 2,
                    })}</span>
                  </div>
                  <div className="payroll-test-preview-table">
                    {preview.lines.map((line) => (
                      <div key={`${line.employee_id}-${line.account_id}`}>
                        <span>{line.employee_name}</span>
                        <span>{line.account_masked}</span>
                        <strong>¥{(line.net_pay_cents / 100).toLocaleString('zh-CN', {
                          minimumFractionDigits: 2,
                          maximumFractionDigits: 2,
                        })}</strong>
                      </div>
                    ))}
                  </div>
                  {preview.exceptions.length > 0 ? (
                    <p>{preview.exceptions.length} 项需要人工核对</p>
                  ) : <p>表内金额校验通过</p>}
                </div>
              ) : null}
            </article>
          )
        })}
        {visible.length === 0 ? <p className="payroll-test-empty">这个分类暂无材料。</p> : null}
      </div>

      <footer>
        <span>第 {safePage + 1} / {pageCount} 页</span>
        <div>
          <button type="button" disabled={safePage === 0} onClick={() => setPage(safePage - 1)}>上一页</button>
          <button type="button" disabled={safePage + 1 >= pageCount} onClick={() => setPage(safePage + 1)}>下一页</button>
        </div>
      </footer>

      {validation ? (
        <div className="payroll-test-validation" aria-label="测试批次验证结果">
          <strong>批次验证结果</strong>
          <span>{validation.ready_batch_count} 个可测试，{validation.blocked_material_count} 份材料仍需整理</span>
          {validation.batches.map((batch) => (
            <div key={batch.batch_id}>
              <span>{batch.period}</span>
              <span>{batch.material_count} 份材料 · {batch.payroll_sheet_count} 份工资表</span>
              <b>{batch.status === 'READY_FOR_TEST_REVIEW' ? '可核对' : '待补工资表'}</b>
            </div>
          ))}
        </div>
      ) : null}
    </section>
  )
}
