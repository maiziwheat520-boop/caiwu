import { useCallback, useEffect, useMemo, useState } from 'react'
import type { ReactNode } from 'react'
import {
  ArrowClockwise,
  Calculator,
  CheckCircle,
  ClockCounterClockwise,
  FileArrowDown,
  FilePlus,
  ListChecks,
  Receipt,
  SlidersHorizontal,
  Table,
  Warning,
} from '@phosphor-icons/react'

import { api, ApiError } from '../api'
import type {
  PayrollLegacyAction,
  PayrollLegacyAdjustment,
  PayrollLegacyBatch,
  PayrollLegacyEmployeeRule,
  PayrollLegacyEvidenceDocument,
  PayrollLegacyEvidenceType,
  PayrollLegacyLine,
  PayrollLegacyWorkspace,
  PayrollTestWorkspaceReadResponse,
} from '../types'
import './payroll-legacy-workbench.css'

type Props = {
  testWorkspace: PayrollTestWorkspaceReadResponse
  csrfToken: string
}

type TaskId =
  | 'fill'
  | 'normal'
  | 'supplemental'
  | 'summary'
  | 'pending'
  | 'verify'
  | 'rules'
  | 'history'

type EditableRule = Omit<PayrollLegacyEmployeeRule, 'payment_channel'> & {
  employee_name: string
  account_masked: string
  payment_channel: '' | PayrollLegacyEmployeeRule['payment_channel']
}

type ReceiptInput = {
  employee_id: string
  account_id: string
  account_masked: string
  payment_channel: string
  expected_amount_cents: number
  amount: string
  status: '' | 'SUCCEEDED' | 'FAILED'
}

type EvidenceSlot = PayrollLegacyEvidenceDocument & {
  label: string
}

const tasks: Array<{
  id: TaskId
  label: string
  description: string
  icon: typeof Table
}> = [
  { id: 'fill', label: '填入主表', description: '读取原工资表，合并人工调整并保存计算结果。', icon: Table },
  { id: 'normal', label: '生成正常代发草稿', description: '按网商银行渠道生成不可付款草稿。', icon: FileArrowDown },
  { id: 'supplemental', label: '生成补发代发草稿', description: '只包含明确标记为补发的正向调整。', icon: FilePlus },
  { id: 'summary', label: '更新工资汇总', description: '保存人数、应发、实发和渠道汇总。', icon: Calculator },
  { id: 'pending', label: '检查上月待办', description: '逐项决定并入主表、另行补发或忽略。', icon: ClockCounterClockwise },
  { id: 'verify', label: '核对本月已发', description: '用实际回单逐人比对草稿金额和账户。', icon: Receipt },
  { id: 'rules', label: '管理工资规则', description: '编辑固定待遇、渠道、工种、地点和可休天数。', icon: SlidersHorizontal },
  { id: 'history', label: '检查规则与历史', description: '检查来源异常、缺失材料和跨月变化。', icon: ListChecks },
]

const channels = ['MYBANK', 'BOC', 'WECHAT'] as const
const paymentKinds = ['NORMAL', 'CASH', 'SUPPLEMENT'] as const
const evidenceSlotDefinitions: Array<{ evidence_type: PayrollLegacyEvidenceType; label: string }> = [
  ...Array.from({ length: 5 }, (_, index) => ({
    evidence_type: 'MYBANK_STATEMENT' as const,
    label: `网商银行代发表${index + 1}`,
  })),
  { evidence_type: 'BOC_RECEIPT', label: '中国银行现金发放账单' },
  { evidence_type: 'WECHAT_RECEIPT', label: '微信单独发放账单' },
]

const emptyEvidenceSlots = (): EvidenceSlot[] => evidenceSlotDefinitions.map((item) => ({
  ...item,
  evidence_ref: '',
}))

const money = (cents: number) => new Intl.NumberFormat('zh-CN', {
  style: 'currency',
  currency: 'CNY',
}).format(cents / 100)

const isChannel = (value: string): value is PayrollLegacyEmployeeRule['payment_channel'] =>
  channels.includes(value as (typeof channels)[number])

const activeBatchFrom = (workspace: PayrollLegacyWorkspace | null, period?: string) =>
  workspace?.batches.find((batch) => batch.period === (period ?? workspace.active_period)) ?? null

function rulesFromBatch(workspace: PayrollLegacyWorkspace, batch: PayrollLegacyBatch): EditableRule[] {
  const saved = new Map(workspace.rules.employees.map((rule) => [rule.employee_id, rule]))
  return batch.lines.map((line) => {
    const rule = saved.get(line.employee_id)
    return {
      employee_id: line.employee_id,
      employee_name: line.employee_name,
      account_masked: line.account_masked,
      fixed_base_salary_cents: rule?.fixed_base_salary_cents ?? line.base_salary_cents,
      fixed_allowance_cents: rule?.fixed_allowance_cents ?? line.allowance_cents,
      night_shift_rate_cents: rule?.night_shift_rate_cents ?? line.night_shift_rate_cents ?? 0,
      rest_days: rule?.rest_days ?? line.rest_days ?? 0,
      payment_channel: rule?.payment_channel ?? (isChannel(line.payment_channel) ? line.payment_channel : ''),
      payment_kind: rule?.payment_kind ?? line.payment_kind ?? 'NORMAL',
      job_group: rule?.job_group ?? line.job_group ?? '',
      location: rule?.location ?? line.location ?? '',
    }
  })
}

function receiptsFromBatch(batch: PayrollLegacyBatch): ReceiptInput[] {
  return batch.lines.map((line) => ({
    employee_id: line.employee_id,
    account_id: line.account_id,
    account_masked: line.account_masked,
    payment_channel: line.payment_channel,
    expected_amount_cents: line.net_pay_cents,
    amount: '',
    status: '' as const,
  }))
}

function evidenceSlotsFromBatch(batch: PayrollLegacyBatch): EvidenceSlot[] {
  const slots = emptyEvidenceSlots()
  if (batch.verification?.schema_version !== 'payroll-current-paid-verification/v2') return slots
  const byType = new Map<PayrollLegacyEvidenceType, PayrollLegacyEvidenceDocument[]>()
  for (const document of batch.verification.evidence_documents) {
    const current = byType.get(document.evidence_type) ?? []
    current.push(document)
    byType.set(document.evidence_type, current)
  }
  const offsets = new Map<PayrollLegacyEvidenceType, number>()
  return slots.map((slot) => {
    const offset = offsets.get(slot.evidence_type) ?? 0
    offsets.set(slot.evidence_type, offset + 1)
    return { ...slot, evidence_ref: byType.get(slot.evidence_type)?.[offset]?.evidence_ref ?? '' }
  })
}

export function PayrollLegacyWorkbench({ testWorkspace, csrfToken }: Props) {
  const payrollSheets = testWorkspace.data.materials.filter(
    (material) => material.material_type === 'PAYROLL_SHEET' && material.period !== null,
  )
  const supportingMaterials = testWorkspace.data.materials.filter(
    (material) => material.material_type !== 'PAYROLL_SHEET' && material.period !== null,
  )
  const sourcePeriods = [...new Set(payrollSheets.flatMap(
    (material) => material.period ? [material.period] : [],
  ))].sort((left, right) => right.localeCompare(left))
  const [sourcePeriod, setSourcePeriod] = useState(sourcePeriods[0] ?? '')
  const periodPayrollSheets = payrollSheets.filter((material) => material.period === sourcePeriod)
  const periodSupportingMaterials = supportingMaterials.filter(
    (material) => material.period === sourcePeriod,
  )
  const [workspace, setWorkspace] = useState<PayrollLegacyWorkspace | null>(null)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState<{ tone: 'success' | 'error'; text: string } | null>(null)
  const [task, setTask] = useState<TaskId>('fill')
  const [period, setPeriod] = useState('')
  const [mainMaterialId, setMainMaterialId] = useState(
    periodPayrollSheets[0]?.material_id ?? '',
  )
  const [supporting, setSupporting] = useState<Record<string, string>>({})
  const [adjustments, setAdjustments] = useState<PayrollLegacyAdjustment[]>([])
  const [rules, setRules] = useState<EditableRule[]>([])
  const [evidenceSlots, setEvidenceSlots] = useState<EvidenceSlot[]>(emptyEvidenceSlots)
  const [receipts, setReceipts] = useState<ReceiptInput[]>([])
  const [pendingDecisions, setPendingDecisions] = useState<Record<string, {
    decision: '' | 'ADD_TO_MAIN' | 'SUPPLEMENT' | 'IGNORE'
    reason: string
  }>>({})

  const applyWorkspace = useCallback((nextWorkspace: PayrollLegacyWorkspace, nextPeriod = nextWorkspace.active_period) => {
    const batch = activeBatchFrom(nextWorkspace, nextPeriod)
    setWorkspace(nextWorkspace)
    setPeriod(nextPeriod)
    setRules(batch ? rulesFromBatch(nextWorkspace, batch) : [])
    setReceipts(batch ? receiptsFromBatch(batch) : [])
    setEvidenceSlots(batch ? evidenceSlotsFromBatch(batch) : emptyEvidenceSlots())
    setAdjustments(batch ? batch.adjustments.filter((item) => !item.source_pending_id) : [])
  }, [])

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const result = await api.getPayrollLegacyWorkspace()
      applyWorkspace(result.data)
    } catch (error) {
      if (!(error instanceof ApiError) || error.status !== 404) {
        setMessage({ tone: 'error', text: '工资功能工作区暂时无法读取' })
      }
      setWorkspace(null)
      setPeriod('')
      setRules([])
      setReceipts([])
      setEvidenceSlots(emptyEvidenceSlots())
      setAdjustments([])
    } finally {
      setLoading(false)
    }
  }, [applyWorkspace])

  useEffect(() => {
    const timer = window.setTimeout(() => void load(), 0)
    return () => window.clearTimeout(timer)
  }, [load])

  const activeBatch = activeBatchFrom(workspace, period)
  const previousOpenItems = useMemo(() => {
    if (!workspace || !period) return []
    return workspace.batches
      .filter((batch) => batch.period < period)
      .sort((left, right) => right.period.localeCompare(left.period))[0]
      ?.pending_items.filter((item) => item.status === 'OPEN') ?? []
  }, [period, workspace])

  const execute = async (action: PayrollLegacyAction, payload: Record<string, unknown>) => {
    if (busy) return
    setBusy(true)
    setMessage(null)
    try {
      const result = await api.runPayrollLegacyCommand({
        action,
        expectedRevision: workspace?.revision ?? 0,
        payload,
        csrfToken,
      })
      applyWorkspace(result.data.workspace)
      setMessage({ tone: 'success', text: '已保存并重新读取最新工资工作区' })
    } catch (error) {
      const status = error instanceof ApiError ? error.status : 0
      setMessage({
        tone: 'error',
        text: status === 409
          ? '工作区已被更新，请刷新后重试'
          : status === 422
            ? '当前材料或填写内容未通过工资规则校验'
            : '工资功能操作暂时未完成',
      })
    } finally {
      setBusy(false)
    }
  }

  const fillMain = () => {
    if (!mainMaterialId) return
    void execute('FILL_MAIN', {
      main_material_id: mainMaterialId,
      supporting_material_ids: supporting,
      adjustments,
    })
  }

  const saveRules = () => {
    if (!activeBatch) return
    void execute('SAVE_RULES', {
      period: activeBatch.period,
      employee_rules: rules.map((rule) => ({
        employee_id: rule.employee_id,
        fixed_base_salary_cents: rule.fixed_base_salary_cents,
        fixed_allowance_cents: rule.fixed_allowance_cents,
        night_shift_rate_cents: rule.night_shift_rate_cents,
        rest_days: rule.rest_days,
        payment_channel: rule.payment_channel,
        payment_kind: rule.payment_kind,
        job_group: rule.job_group,
        location: rule.location,
      })),
    })
  }

  const rulesComplete = rules.length > 0 && rules.every(
    (rule) => rule.payment_channel && rule.job_group.trim() && rule.location.trim(),
  )
  const evidenceRefs = evidenceSlots.map((item) => item.evidence_ref.trim())
  const evidenceComplete = evidenceRefs.every(
    (value) => /^[A-Za-z0-9][A-Za-z0-9._-]{2,127}$/.test(value),
  ) && new Set(evidenceRefs).size === evidenceSlots.length
  const receiptsComplete = evidenceComplete && receipts.length > 0 && receipts.every(
    (receipt) => receipt.status && receipt.amount !== '' && Number(receipt.amount) >= 0,
  )
  const pendingComplete = previousOpenItems.length > 0 && previousOpenItems.every((item) => {
    const resolution = pendingDecisions[item.pending_id]
    return resolution?.decision && resolution.reason.trim()
  })

  const updateRule = (employeeId: string, patch: Partial<EditableRule>) => {
    setRules((current) => current.map((rule) => rule.employee_id === employeeId
      ? { ...rule, ...patch }
      : rule))
  }

  const selectedMaterial = payrollSheets.find((material) => material.material_id === mainMaterialId)
  const selectSourcePeriod = (nextPeriod: string) => {
    setSourcePeriod(nextPeriod)
    setMainMaterialId(
      payrollSheets.find((material) => material.period === nextPeriod)?.material_id ?? '',
    )
    setSupporting({})
    if (workspace?.batches.some((batch) => batch.period === nextPeriod)) {
      applyWorkspace(workspace, nextPeriod)
    } else {
      setAdjustments([])
    }
  }

  return (
    <section className="payroll-legacy-workbench" aria-labelledby="payroll-legacy-heading">
      <header className="payroll-legacy-header">
        <div>
          <span>原软件功能迁移</span>
          <h2 id="payroll-legacy-heading">原工资软件工作台</h2>
          <p>按业务任务操作，不复制 Excel 界面。所有结果保存到当前公司测试工作区。</p>
        </div>
        <div className="payroll-legacy-safety">
          <CheckCircle size={18} weight="fill" />
          <span>可编辑、可计算、可恢复</span>
          <small>不可付款 · 不可提交银行</small>
        </div>
      </header>

      <nav className="payroll-legacy-taskbar" aria-label="原工资软件八项功能">
        {tasks.map((item) => {
          const Icon = item.icon
          return (
            <button
              type="button"
              key={item.id}
              aria-label={item.label}
              className={task === item.id ? 'active' : ''}
              aria-pressed={task === item.id}
              onClick={() => setTask(item.id)}
            >
              <Icon size={18} />
              <span>{item.label}</span>
              <small>{item.description}</small>
            </button>
          )
        })}
      </nav>

      {loading ? (
        <div className="payroll-legacy-loading"><ArrowClockwise size={20} />正在读取已保存工资主表</div>
      ) : (
        <div className="payroll-legacy-body">
          <div className="payroll-legacy-toolbar">
            <div>
              <strong>{workspace ? `工资主表已保存 · 版本 ${workspace.revision}` : '尚未建立网页工资主表'}</strong>
              <span>{workspace ? `${workspace.batches.length} 个账期 · 当前 ${workspace.active_period}` : '请选择原工资表开始导入'}</span>
            </div>
            {workspace ? (
              <label>
                <span>查看已保存月份</span>
                <select value={period} onChange={(event) => applyWorkspace(workspace, event.target.value)}>
                  {workspace.batches.map((batch) => <option key={batch.period}>{batch.period}</option>)}
                </select>
              </label>
            ) : null}
            <button type="button" className="secondary" onClick={() => void load()} disabled={busy}>
              <ArrowClockwise size={16} />刷新恢复
            </button>
          </div>

          {task === 'fill' ? (
            <div className="payroll-task-panel">
              <div className="payroll-task-heading">
                <div><span>01</span><h3>选择来源并填入主表</h3></div>
                <p>服务端会重新解析加密保存的 XLS/XLSX；浏览器不能自行提交工资明细。</p>
              </div>
              <div className="payroll-source-form">
                <label>
                  <span>工资月份</span>
                  <select value={sourcePeriod} onChange={(event) => selectSourcePeriod(event.target.value)}>
                    {sourcePeriods.map((candidate) => (
                      <option key={candidate} value={candidate}>{candidate}</option>
                    ))}
                  </select>
                </label>
                <label>
                  <span>选择原工资表版本</span>
                  <select value={mainMaterialId} onChange={(event) => setMainMaterialId(event.target.value)}>
                    {periodPayrollSheets.map((material, index) => (
                      <option key={material.material_id} value={material.material_id}>
                        版本 {index + 1} · {material.routing_status === 'AUTO_TEST' ? '可自动测试' : '需要人工复核'}
                      </option>
                    ))}
                  </select>
                </label>
                {(['attendance', 'aunt_attendance', 'review_statistics'] as const).map((role) => (
                  <label key={role}>
                    <span>{{ attendance: '考勤表', aunt_attendance: '阿姨考勤', review_statistics: '复核统计' }[role]}</span>
                    <select
                      value={supporting[role] ?? ''}
                      onChange={(event) => setSupporting((current) => {
                        const next = { ...current }
                        if (event.target.value) next[role] = event.target.value
                        else delete next[role]
                        return next
                      })}
                    >
                      <option value="">本月未提供</option>
                      {periodSupportingMaterials.map((material) => (
                        <option key={material.material_id} value={material.material_id}>
                          {material.material_type}
                        </option>
                      ))}
                    </select>
                  </label>
                ))}
              </div>
              {selectedMaterial?.routing_status === 'REVIEW_REQUIRED' ? (
                <div className="payroll-inline-warning"><Warning size={17} />这份工资表需要人工核对；可进入主表编辑，但阻断异常解决前不能生成代发草稿。</div>
              ) : null}
              {activeBatch?.period === sourcePeriod ? (
                <AdjustmentEditor
                  lines={activeBatch.lines}
                  adjustments={adjustments}
                  onChange={setAdjustments}
                />
              ) : null}
              <button type="button" className="primary" disabled={!mainMaterialId || busy} onClick={fillMain}>
                {busy ? '正在解析并保存' : '导入主表并计算'}
              </button>
            </div>
          ) : null}

          {task === 'rules' && activeBatch ? (
            <div className="payroll-task-panel">
              <div className="payroll-task-heading"><div><span>07</span><h3>编辑规则并重新计算</h3></div><p>空白地点、工种或发放渠道不会被系统猜测。</p></div>
              <div className="payroll-rule-table" role="table" aria-label="工资规则">
                <div className="payroll-rule-row header" role="row">
                  <span>员工 / 账户</span><span>固定工资</span><span>固定津贴</span><span>渠道</span><span>类型</span><span>夜班标准</span><span>可休天数</span><span>工种</span><span>地点</span>
                </div>
                {rules.map((rule) => (
                  <div className="payroll-rule-row" role="row" key={rule.employee_id}>
                    <strong>{rule.employee_name}<small>{rule.account_masked}</small></strong>
                    <MoneyInput label={`${rule.employee_name}固定工资`} cents={rule.fixed_base_salary_cents} onChange={(value) => updateRule(rule.employee_id, { fixed_base_salary_cents: value })} />
                    <MoneyInput label={`${rule.employee_name}固定津贴`} cents={rule.fixed_allowance_cents} onChange={(value) => updateRule(rule.employee_id, { fixed_allowance_cents: value })} />
                    <select aria-label={`${rule.employee_name}发放渠道`} value={rule.payment_channel} onChange={(event) => updateRule(rule.employee_id, { payment_channel: event.target.value as EditableRule['payment_channel'] })}><option value="">请选择</option>{channels.map((channel) => <option key={channel}>{channel}</option>)}</select>
                    <select aria-label={`${rule.employee_name}发放类型`} value={rule.payment_kind} onChange={(event) => updateRule(rule.employee_id, { payment_kind: event.target.value as PayrollLegacyEmployeeRule['payment_kind'] })}>{paymentKinds.map((kind) => <option key={kind}>{kind}</option>)}</select>
                    <MoneyInput label={`${rule.employee_name}夜班标准`} cents={rule.night_shift_rate_cents} onChange={(value) => updateRule(rule.employee_id, { night_shift_rate_cents: value })} />
                    <input aria-label={`${rule.employee_name}可休天数`} type="number" min="0" max="31" value={rule.rest_days} onChange={(event) => updateRule(rule.employee_id, { rest_days: Number(event.target.value) })} />
                    <input aria-label={`${rule.employee_name}工种`} value={rule.job_group} onChange={(event) => updateRule(rule.employee_id, { job_group: event.target.value })} placeholder="必须确认" />
                    <input aria-label={`${rule.employee_name}地点`} value={rule.location} onChange={(event) => updateRule(rule.employee_id, { location: event.target.value })} placeholder="必须确认" />
                  </div>
                ))}
              </div>
              <div className="payroll-main-total"><span>重新计算后实发合计</span><strong>{money(activeBatch.lines.reduce((sum, line) => sum + line.net_pay_cents, 0))}</strong></div>
              <button type="button" className="primary" disabled={!rulesComplete || busy} onClick={saveRules}>保存规则并重新计算</button>
            </div>
          ) : null}

          {task === 'normal' && activeBatch ? (
            <SimpleAction
              title="生成正常代发草稿"
              detail="仅收集发放渠道为 MYBANK 的当前主表金额。草稿没有付款或提交能力。"
              button="生成不可付款正常草稿"
              busy={busy}
              onRun={() => void execute('GENERATE_NORMAL_DRAFT', { period: activeBatch.period })}
            />
          ) : null}

          {task === 'supplemental' && activeBatch ? (
            <SimpleAction
              title="生成补发代发草稿"
              detail="只提取明确标为 SUPPLEMENT 的正向人工调整，不自动猜测补发金额。"
              button="生成不可付款补发草稿"
              busy={busy}
              onRun={() => void execute('GENERATE_SUPPLEMENTAL_DRAFT', { period: activeBatch.period })}
            />
          ) : null}

          {task === 'summary' && activeBatch ? (
            <SimpleAction
              title="更新工资汇总"
              detail="按当前已保存主表重算人数、应发、实发与各发放渠道金额。"
              button="计算并保存汇总"
              busy={busy}
              onRun={() => void execute('UPDATE_SUMMARY', { period: activeBatch.period })}
            >
              {activeBatch.summary ? <SummaryView batch={activeBatch} /> : null}
            </SimpleAction>
          ) : null}

          {task === 'history' && activeBatch ? (
            <SimpleAction
              title="检查规则与历史"
              detail="检查来源阻断项、缺失辅助材料和上一账期的人员与金额变化。"
              button="执行规则与历史检查"
              busy={busy}
              onRun={() => void execute('CHECK_RULES_AND_HISTORY', { period: activeBatch.period })}
            >
              {activeBatch.checks ? (
                <div className="payroll-check-result">
                  <strong>当前问题 {activeBatch.checks.current_issues.length} 项</strong>
                  <span>历史变化 {activeBatch.checks.history_issues.length} 项</span>
                </div>
              ) : null}
            </SimpleAction>
          ) : null}

          {task === 'pending' && activeBatch ? (
            <div className="payroll-task-panel">
              <div className="payroll-task-heading"><div><span>05</span><h3>检查上月待办</h3></div><p>每一项都必须明确决定和填写原因。</p></div>
              {previousOpenItems.length ? previousOpenItems.map((item) => (
                <div className="payroll-pending-row" key={item.pending_id}>
                  <div><strong>{item.employee_id}</strong><span>{item.source_period} · {item.direction === 'ADD' ? '少发' : '多发'} {money(item.amount_cents)}</span></div>
                  <select aria-label={`${item.employee_id}处理决定`} value={pendingDecisions[item.pending_id]?.decision ?? ''} onChange={(event) => setPendingDecisions((current) => ({ ...current, [item.pending_id]: { decision: event.target.value as 'ADD_TO_MAIN' | 'SUPPLEMENT' | 'IGNORE', reason: current[item.pending_id]?.reason ?? '' } }))}><option value="">请选择</option><option value="ADD_TO_MAIN">并入本月主表</option><option value="SUPPLEMENT">另行补发</option><option value="IGNORE">忽略并留痕</option></select>
                  <input aria-label={`${item.employee_id}处理原因`} value={pendingDecisions[item.pending_id]?.reason ?? ''} onChange={(event) => setPendingDecisions((current) => ({ ...current, [item.pending_id]: { decision: current[item.pending_id]?.decision ?? '', reason: event.target.value } }))} placeholder="填写处理原因" />
                </div>
              )) : <p className="payroll-empty-task">上一账期没有未解决的少发或多发事项。</p>}
              <button type="button" className="primary" disabled={!pendingComplete || busy} onClick={() => void execute('CHECK_PREVIOUS_PENDING', { period: activeBatch.period, resolutions: previousOpenItems.map((item) => ({ pending_id: item.pending_id, ...pendingDecisions[item.pending_id] })) })}>保存上月待办决定</button>
            </div>
          ) : null}

          {task === 'verify' && activeBatch ? (
            <div className="payroll-task-panel">
              <div className="payroll-task-heading"><div><span>06</span><h3>按三类账单核对本月已发</h3></div><p>工资表全体员工是理论应发基准；网商银行、中行和微信实际到账合计必须与它相等。</p></div>
              <div className="payroll-evidence-collection">
                <div>
                  <strong>账单收集完整度</strong>
                  <span>工资表理论总额：{money(activeBatch.lines.reduce((sum, line) => sum + line.net_pay_cents, 0))}</span>
                </div>
                <ul>
                  <li>网商银行代发表 {evidenceSlots.filter((item) => item.evidence_type === 'MYBANK_STATEMENT' && item.evidence_ref.trim()).length}/5</li>
                  <li>中国银行现金发放账单 {evidenceSlots.some((item) => item.evidence_type === 'BOC_RECEIPT' && item.evidence_ref.trim()) ? 1 : 0}/1</li>
                  <li>微信单独发放账单 {evidenceSlots.some((item) => item.evidence_type === 'WECHAT_RECEIPT' && item.evidence_ref.trim()) ? 1 : 0}/1</li>
                </ul>
                <div className="payroll-evidence-reference-grid">
                  {evidenceSlots.map((slot, index) => (
                    <label key={`${slot.evidence_type}-${index}`}>
                      <span>{slot.label}</span>
                      <input
                        aria-label={slot.label}
                        value={slot.evidence_ref}
                        onChange={(event) => setEvidenceSlots((current) => current.map(
                          (item, itemIndex) => itemIndex === index
                            ? { ...item, evidence_ref: event.target.value }
                            : item,
                        ))}
                        placeholder="填写已接收账单的证据编号"
                      />
                    </label>
                  ))}
                </div>
              </div>
              {receipts.length ? (
                <>
                  <div className="payroll-receipt-list">
                    {receipts.map((receipt) => (
                      <div key={receipt.employee_id}>
                        <span>{receipt.employee_id}<small>{receipt.account_masked} · 应发 {money(receipt.expected_amount_cents)}</small></span>
                        <input aria-label={`${receipt.employee_id}实际到账金额`} type="number" min="0" step="0.01" value={receipt.amount} onChange={(event) => setReceipts((current) => current.map((item) => item.employee_id === receipt.employee_id ? { ...item, amount: event.target.value } : item))} placeholder="实际到账元" />
                        <select aria-label={`${receipt.employee_id}回单状态`} value={receipt.status} onChange={(event) => setReceipts((current) => current.map((item) => item.employee_id === receipt.employee_id ? { ...item, status: event.target.value as ReceiptInput['status'] } : item))}><option value="">请选择</option><option value="SUCCEEDED">已成功</option><option value="FAILED">失败</option></select>
                      </div>
                    ))}
                  </div>
                </>
              ) : <p className="payroll-empty-task">当前工资表没有可核对人员。</p>}
              {activeBatch.verification?.schema_version === 'payroll-current-paid-verification/v2' ? (
                <div className={`payroll-verification-total ${activeBatch.verification.totals_match ? 'matched' : 'attention'}`}>
                  <strong>{activeBatch.verification.totals_match ? '理论总额与实际总额一致' : '理论总额与实际总额不一致'}</strong>
                  <span>理论 {money(activeBatch.verification.theoretical_total_cents)} · 实际 {money(activeBatch.verification.actual_total_cents)} · 差额 {money(activeBatch.verification.difference_cents)}</span>
                </div>
              ) : null}
              <button type="button" className="primary" disabled={!receiptsComplete || busy} onClick={() => void execute('VERIFY_CURRENT_PAID', { period: activeBatch.period, evidence_documents: evidenceSlots.map(({ evidence_type, evidence_ref }) => ({ evidence_type, evidence_ref: evidence_ref.trim() })), receipts: receipts.map((receipt) => ({ employee_id: receipt.employee_id, account_id: receipt.account_id, payment_channel: receipt.payment_channel, amount_cents: Math.round(Number(receipt.amount) * 100), status: receipt.status })) })}>保存本月已发核对</button>
            </div>
          ) : null}

          {!activeBatch && task !== 'fill' ? (
            <div className="payroll-empty-task">请先用“填入主表”导入一个工资账期。</div>
          ) : null}

          {activeBatch ? <MainTable batch={activeBatch} /> : null}
          {message ? <p className={`payroll-legacy-message ${message.tone}`} role={message.tone === 'error' ? 'alert' : 'status'}>{message.text}</p> : null}
        </div>
      )}
    </section>
  )
}

function MoneyInput({ label, cents, onChange }: { label: string; cents: number; onChange: (cents: number) => void }) {
  return <input aria-label={label} type="number" min="0" step="0.01" value={(cents / 100).toFixed(2)} onChange={(event) => onChange(Math.round(Number(event.target.value) * 100))} />
}

function SimpleAction({ title, detail, button, busy, onRun, children }: { title: string; detail: string; button: string; busy: boolean; onRun: () => void; children?: ReactNode }) {
  return (
    <div className="payroll-task-panel">
      <div className="payroll-task-heading"><div><h3>{title}</h3></div><p>{detail}</p></div>
      {children}
      <button type="button" className="primary" disabled={busy} onClick={onRun}>{button}</button>
    </div>
  )
}

function AdjustmentEditor({ lines, adjustments, onChange }: { lines: PayrollLegacyLine[]; adjustments: PayrollLegacyAdjustment[]; onChange: (value: PayrollLegacyAdjustment[]) => void }) {
  const [employeeId, setEmployeeId] = useState(lines[0]?.employee_id ?? '')
  const [amount, setAmount] = useState('')
  const [reason, setReason] = useState('')
  const [disposition, setDisposition] = useState<'MAIN' | 'SUPPLEMENT'>('MAIN')
  const add = () => {
    if (!employeeId || !reason.trim() || !amount || Number(amount) === 0) return
    onChange([...adjustments, {
      employee_id: employeeId,
      item_code: `manual_${Date.now().toString(36)}`,
      kind: 'SPECIAL',
      amount_cents: Math.round(Number(amount) * 100),
      reason: reason.trim(),
      disposition,
    }])
    setAmount('')
    setReason('')
  }
  return (
    <div className="payroll-adjustments">
      <div><strong>人工调整</strong><span>绩效、补扣和补发必须保留原因。</span></div>
      <div className="payroll-adjustment-form">
        <select aria-label="调整员工" value={employeeId} onChange={(event) => setEmployeeId(event.target.value)}>{lines.map((line) => <option key={line.employee_id} value={line.employee_id}>{line.employee_name}</option>)}</select>
        <input aria-label="调整金额" type="number" step="0.01" value={amount} onChange={(event) => setAmount(event.target.value)} placeholder="正数增加，负数扣减" />
        <select aria-label="调整去向" value={disposition} onChange={(event) => setDisposition(event.target.value as 'MAIN' | 'SUPPLEMENT')}><option value="MAIN">并入主表</option><option value="SUPPLEMENT">另行补发</option></select>
        <input aria-label="调整原因" value={reason} onChange={(event) => setReason(event.target.value)} placeholder="必填原因" />
        <button type="button" className="secondary" onClick={add}>加入调整</button>
      </div>
      {adjustments.length ? <ul>{adjustments.map((item) => <li key={item.item_code}><span>{lines.find((line) => line.employee_id === item.employee_id)?.employee_name} · {item.reason}</span><strong>{money(item.amount_cents)}</strong><button type="button" onClick={() => onChange(adjustments.filter((candidate) => candidate.item_code !== item.item_code))}>移除</button></li>)}</ul> : null}
    </div>
  )
}

function SummaryView({ batch }: { batch: PayrollLegacyBatch }) {
  if (!batch.summary) return null
  return <div className="payroll-summary-strip"><span>{batch.summary.employee_count} 人</span><span>应发 {money(batch.summary.gross_pay_cents)}</span><strong>实发 {money(batch.summary.net_pay_cents)}</strong></div>
}

function MainTable({ batch }: { batch: PayrollLegacyBatch }) {
  return (
    <div className="payroll-main-table">
      <div className="payroll-main-table-heading"><div><span>当前保存结果</span><h3>{batch.period} 工资主表</h3></div><strong>{batch.lines.length} 人 · {money(batch.lines.reduce((sum, line) => sum + line.net_pay_cents, 0))}</strong></div>
      <div className="payroll-main-table-scroll">
        <table>
          <thead><tr><th>员工</th><th>账户</th><th>基本工资</th><th>津贴</th><th>奖金</th><th>扣款</th><th>社保</th><th>公积金</th><th>个税</th><th>实发</th><th>渠道</th></tr></thead>
          <tbody>{batch.lines.map((line) => <tr key={line.employee_id}><td>{line.employee_name}</td><td>{line.account_masked}</td><td>{money(line.base_salary_cents)}</td><td>{money(line.allowance_cents)}</td><td>{money(line.bonus_cents)}</td><td>{money(line.deduction_cents)}</td><td>{money(line.social_insurance_cents)}</td><td>{money(line.housing_fund_cents)}</td><td>{money(line.individual_income_tax_cents)}</td><td><strong>{money(line.net_pay_cents)}</strong></td><td>{line.payment_channel === 'UNASSIGNED' ? '待确认' : line.payment_channel}</td></tr>)}</tbody>
        </table>
      </div>
      {batch.source_exceptions.length ? <div className="payroll-blockers"><Warning size={17} /><span>{batch.source_exceptions.length} 个来源阻断/复核项；解决前不会生成代发草稿。</span></div> : null}
      {batch.drafts.length ? <div className="payroll-draft-list">{batch.drafts.map((draft) => <div key={draft.draft_id}><span>{draft.draft_type === 'normal_bank_payroll' ? '正常代发草稿' : '补发草稿'} · {draft.lines.length} 人</span><strong>{money(draft.total_amount_cents)}</strong><small>不可付款 / 不可提交</small></div>)}</div> : null}
    </div>
  )
}
