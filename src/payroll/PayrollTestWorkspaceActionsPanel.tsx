import { useMemo, useState } from 'react'

import { api, ApiError } from '../api'
import type {
  PayrollTestBatchValidationResult,
  PayrollInputMaterialPreview,
  PayrollTestMaterialType,
  PayrollTestWorkspaceMaterial,
  PayrollTestWorkspaceReadResponse,
} from '../types'
import './payroll-test-workspace.css'

const MATERIAL_TYPES: ReadonlyArray<{ value: PayrollTestMaterialType; label: string }> = [
  { value: 'ATTENDANCE_SHEET', label: '考勤表' },
  { value: 'AUNT_ATTENDANCE_SHEET', label: '阿姨考勤表' },
  { value: 'REVIEW_STATISTICS', label: '好评统计' },
  { value: 'ADJUSTMENT_SOURCE', label: '好评统计（旧分类）' },
]

const INPUT_MATERIAL_TYPES = new Set(MATERIAL_TYPES.map(({ value }) => value))

const PAGE_SIZE = 25

type Filter = 'ALL' | '2026-07' | '2026-08'

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
      : 'ATTENDANCE_SHEET',
  }
}

function materialTypeLabel(value: string | null): string {
  return MATERIAL_TYPES.find((item) => item.value === value)?.label ?? '工资表素材'
}

function standardMaterialName(material: PayrollTestWorkspaceMaterial): string {
  const period = material.period
    ? `${material.period.slice(0, 4)}.${Number(material.period.slice(5))}`
    : '月份待确认'
  return `${period}_${materialTypeLabel(material.material_type).replace('（旧分类）', '')}`
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
  const [filter, setFilter] = useState<Filter>('ALL')
  const [page, setPage] = useState(0)
  const [editingId, setEditingId] = useState<string | null>(null)
  const [draft, setDraft] = useState<Draft | null>(null)
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState<{ tone: 'success' | 'error'; text: string } | null>(null)
  const [validation, setValidation] = useState<PayrollTestBatchValidationResult | null>(null)
  const [preview, setPreview] = useState<PayrollInputMaterialPreview | null>(null)

  const inputMaterials = useMemo(
    () => workspace.data.materials
      .filter((material) => INPUT_MATERIAL_TYPES.has(material.material_type as PayrollTestMaterialType))
      .sort((left, right) => `${left.period}-${left.material_type}-${left.material_id}`.localeCompare(
        `${right.period}-${right.material_type}-${right.material_id}`,
      )),
    [workspace.data.materials],
  )
  const displayNames = useMemo(() => {
    const totals = new Map<string, number>()
    for (const item of inputMaterials) {
      const base = standardMaterialName(item)
      totals.set(base, (totals.get(base) ?? 0) + 1)
    }
    const seen = new Map<string, number>()
    return new Map(inputMaterials.map((item) => {
      const base = standardMaterialName(item)
      const position = (seen.get(base) ?? 0) + 1
      seen.set(base, position)
      return [
        item.material_id,
        (totals.get(base) ?? 0) > 1 ? `${base}（版本 ${position}）` : base,
      ]
    }))
  }, [inputMaterials])
  const filtered = useMemo(
    () => inputMaterials.filter((material) => filter === 'ALL' || material.period === filter),
    [filter, inputMaterials],
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
      setFilter(result.data.material.period as Filter)
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
      const result = await api.previewPayrollInputMaterial(materialId)
      setPreview(result.data)
    } catch {
      setMessage({ tone: 'error', text: '素材内容暂时无法解析，请检查 Excel 文件或调整素材类型。' })
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="payroll-test-actions" aria-labelledby="payroll-test-actions-heading">
      <header>
        <div>
          <span className="payroll-test-eyebrow">测试数据，不会发薪</span>
          <h2 id="payroll-test-actions-heading">七、八月工资表素材</h2>
          <p>素材库只保留考勤表、阿姨考勤表和好评统计；导入后按月份和类型自动命名。工资主表与代发表不进入素材库。</p>
        </div>
        <button className="payroll-test-primary" type="button" disabled={busy} onClick={validate}>
          {busy ? '处理中…' : '检查七八月素材'}
        </button>
      </header>

      <div className="payroll-test-summary" aria-label="材料状态汇总">
        <button type="button" data-active={filter === 'ALL'} onClick={() => changeFilter('ALL')}>
          全部 <strong>{inputMaterials.length}</strong>
        </button>
        <button type="button" data-active={filter === '2026-07'} onClick={() => changeFilter('2026-07')}>
          2026 年 7 月 <strong>{inputMaterials.filter((item) => item.period === '2026-07').length}</strong>
        </button>
        <button type="button" data-active={filter === '2026-08'} onClick={() => changeFilter('2026-08')}>
          2026 年 8 月 <strong>{inputMaterials.filter((item) => item.period === '2026-08').length}</strong>
        </button>
      </div>

      {message ? <div className={`payroll-test-message ${message.tone}`} role="status">{message.text}</div> : null}

      <div className="payroll-test-materials" aria-label="工资表素材">
        {visible.map((material) => {
          const editing = editingId === material.material_id && draft !== null
          return (
            <article key={material.material_id}>
              <div className="payroll-test-material-main">
                <strong>{displayNames.get(material.material_id) ?? standardMaterialName(material)}</strong>
                <span>用于生成工资主表 · 材料编号 {material.material_id.slice(-8).toUpperCase()}</span>
              </div>
              {editing ? (
                <div className="payroll-test-editor">
                  <label>
                    归属月份
                    <input
                      type="month"
                      min="2026-07"
                      max="2026-08"
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
                  <span data-routing={material.routing_status}>已识别为工资表素材</span>
                  <button type="button" disabled={busy} onClick={() => previewMaterial(material.material_id)}>
                    查看内容
                  </button>
                  <button type="button" disabled={busy} onClick={() => startEditing(material)}>
                    调整名称
                  </button>
                </div>
              )}
              {preview?.material_id === material.material_id ? (
                <div className="payroll-test-preview" aria-label={`${preview.canonical_name}内容预览`}>
                  <span className="payroll-test-preview-boundary">只读内容预览 · 不可付款</span>
                  <div className="payroll-input-preview-heading">
                    <div>
                      <strong>{preview.canonical_name}</strong>
                      <span>{preview.selected_sheet} · 共 {preview.record_count} 条记录</span>
                    </div>
                    <span>{preview.sheet_names.length} 个工作表</span>
                  </div>
                  <div className="payroll-input-preview-table" role="table" aria-label="素材前八行">
                    <div role="row" className="header" style={{ gridTemplateColumns: `repeat(${preview.columns.length}, minmax(120px, 1fr))` }}>
                      {preview.columns.map((column) => <strong role="columnheader" key={column}>{column}</strong>)}
                    </div>
                    {preview.preview_rows.map((row) => (
                      <div role="row" key={row.source_row} style={{ gridTemplateColumns: `repeat(${preview.columns.length}, minmax(120px, 1fr))` }}>
                        {row.values.map((value, index) => (
                          <span role="cell" key={`${row.source_row}-${preview.columns[index]}`}>{value || '—'}</span>
                        ))}
                      </div>
                    ))}
                  </div>
                  {preview.detected_material_type !== material.material_type ? (
                    <p>内容识别结果与当前名称不同；确认后可用“调整名称”保存正确类型。</p>
                  ) : <p>内容识别与当前名称一致。</p>}
                </div>
              ) : null}
            </article>
          )
        })}
        {visible.length === 0 ? <p className="payroll-test-empty">这个月份暂无工资表素材。</p> : null}
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
