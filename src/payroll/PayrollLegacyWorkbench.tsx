import { useCallback, useEffect, useMemo, useState } from 'react'
import type { ReactNode } from 'react'
import {
  ArrowClockwise,
  CheckCircle,
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
  PayrollLegacyReviewRule,
  PayrollLegacyReviewRuleType,
  PayrollLegacyWorkspace,
  PayrollTestWorkspaceReadResponse,
} from '../types'
import './payroll-legacy-workbench.css'
import type { PayrollConfirmedMaterials } from './PayrollTestWorkspaceActionsPanel'

type Props = {
  testWorkspace: PayrollTestWorkspaceReadResponse
  csrfToken: string
  confirmedMaterials?: PayrollConfirmedMaterials | null
}

type TaskId =
  | 'generate'
  | 'normal'
  | 'supplemental'
  | 'verify'
  | 'rules'
  | 'history'

type EditableRule = Omit<PayrollLegacyEmployeeRule, 'payment_channel'> & {
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
  { id: 'generate', label: '生成当月工资', description: '用唯一确认素材、工资规则和上月待办生成工资表。', icon: Table },
  { id: 'normal', label: '生成网商银行代发表', description: '按五家公司分别生成最终代发表。', icon: FileArrowDown },
  { id: 'supplemental', label: '生成补发代发表', description: '只包含明确标记为补发的正向调整。', icon: FilePlus },
  { id: 'verify', label: '复核本月已发并更新汇总', description: '先核对七份实际流水，全部匹配后更新汇总。', icon: Receipt },
  { id: 'rules', label: '管理工资规则', description: '编辑固定待遇、渠道、工种、地点和可休天数。', icon: SlidersHorizontal },
  { id: 'history', label: '检查规则与历史', description: '检查来源异常、缺失材料和跨月变化。', icon: ListChecks },
]

const channels = ['MYBANK', 'BOC', 'WECHAT'] as const
const paymentKinds = ['NORMAL', 'CASH', 'SUPPLEMENT'] as const
const reviewRuleDefinitions: ReadonlyArray<{
  rule_type: PayrollLegacyReviewRuleType
  rule_id: string
  label: string
  default_name: string
  default_severity: PayrollLegacyReviewRule['severity']
  default_threshold_cents: number
}> = [
  {
    rule_type: 'PAYMENT_CHANNEL_REQUIRED',
    rule_id: 'review_payment_channel',
    label: '发放渠道完整性',
    default_name: '发放渠道必须确认',
    default_severity: 'BLOCKING',
    default_threshold_cents: 0,
  },
  {
    rule_type: 'SUPPORTING_MATERIAL_REQUIRED',
    rule_id: 'review_supporting_materials',
    label: '辅助材料完整性',
    default_name: '三类工资素材必须齐全',
    default_severity: 'REVIEW',
    default_threshold_cents: 0,
  },
  {
    rule_type: 'HISTORY_CHANGE_REVIEW',
    rule_id: 'review_history_change',
    label: '跨月变化',
    default_name: '相邻月份人员与工资变化',
    default_severity: 'REVIEW',
    default_threshold_cents: 1,
  },
]
const evidenceSlotDefinitions: Array<{ evidence_type: PayrollLegacyEvidenceType; label: string }> = [
  ...Array.from({ length: 5 }, (_, index) => ({
    evidence_type: 'MYBANK_STATEMENT' as const,
    label: `网商银行发放流水${index + 1}`,
  })),
  { evidence_type: 'BOC_RECEIPT', label: '中国银行实际发放流水' },
  { evidence_type: 'WECHAT_RECEIPT', label: '李勇微信实际转账记录' },
]

const emptyEvidenceSlots = (): EvidenceSlot[] => evidenceSlotDefinitions.map((item) => ({
  ...item,
  evidence_ref: '',
}))

const money = (cents: number) => new Intl.NumberFormat('zh-CN', {
  style: 'currency',
  currency: 'CNY',
}).format(cents / 100)

const reviewRuleDefinition = (type: PayrollLegacyReviewRuleType) =>
  reviewRuleDefinitions.find((definition) => definition.rule_type === type)!

const activeBatchFrom = (workspace: PayrollLegacyWorkspace | null, period?: string) =>
  workspace?.batches.find((batch) => batch.period === (period ?? workspace.active_period)) ?? null

function rulesFromWorkspace(workspace: PayrollLegacyWorkspace): EditableRule[] {
  return workspace.rules.employees.map((rule) => ({ ...rule }))
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

export function PayrollLegacyWorkbench({ testWorkspace, csrfToken, confirmedMaterials = null }: Props) {
  const [workspace, setWorkspace] = useState<PayrollLegacyWorkspace | null>(null)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState<{ tone: 'success' | 'error'; text: string } | null>(null)
  const [task, setTask] = useState<TaskId>('generate')
  const [period, setPeriod] = useState('')
  const [generationPeriod, setGenerationPeriod] = useState<string>(confirmedMaterials?.period ?? '2026-08')
  const [adjustments, setAdjustments] = useState<PayrollLegacyAdjustment[]>([])
  const [rules, setRules] = useState<EditableRule[]>([])
  const [reviewRules, setReviewRules] = useState<PayrollLegacyReviewRule[]>([])
  const [ruleSourceFile, setRuleSourceFile] = useState<File | null>(null)
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
    setRules(rulesFromWorkspace(nextWorkspace))
    setReviewRules(nextWorkspace.rules.review_rules ?? [])
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
      setReviewRules([])
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
    if (!workspace || !generationPeriod) return []
    return workspace.batches
      .filter((batch) => batch.period < generationPeriod)
      .sort((left, right) => right.period.localeCompare(left.period))[0]
      ?.pending_items.filter((item) => item.status === 'OPEN') ?? []
  }, [generationPeriod, workspace])

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
          : error instanceof ApiError
            ? error.message
            : '工资功能操作暂时未完成',
      })
    } finally {
      setBusy(false)
    }
  }

  const generateMonthlyPayroll = () => {
    if (!confirmedMaterials || confirmedMaterials.period !== generationPeriod) return
    void execute('GENERATE_MONTHLY_PAYROLL', {
      period: generationPeriod,
      supporting_material_ids: confirmedMaterials.material_ids,
      adjustments,
      pending_resolutions: previousOpenItems.map((item) => ({
        pending_id: item.pending_id,
        ...pendingDecisions[item.pending_id],
      })),
    })
  }

  const saveRules = () => {
    void execute('SAVE_RULES', {
      period: generationPeriod,
      employee_rules: rules.map((rule) => ({
        employee_id: rule.employee_id,
        employee_name: rule.employee_name,
        account_id: rule.account_id,
        account_masked: rule.account_masked,
        disbursement_company: rule.disbursement_company,
        fixed_base_salary_cents: rule.fixed_base_salary_cents,
        fixed_allowance_cents: rule.fixed_allowance_cents,
        fixed_adjustment_cents: rule.fixed_adjustment_cents ?? 0,
        night_shift_rate_cents: rule.night_shift_rate_cents,
        rest_days: rule.rest_days,
        payment_channel: rule.payment_channel,
        payment_kind: rule.payment_kind,
        job_group: rule.job_group,
        location: rule.location,
      })),
      ...(reviewRules.length > 0 || workspace ? { review_rules: reviewRules } : {}),
    })
  }

  const importRules = async () => {
    if (!ruleSourceFile || busy) return
    if (!ruleSourceFile.name.toLowerCase().endsWith('.xlsx') || ruleSourceFile.size > 2 * 1024 * 1024) {
      setMessage({ tone: 'error', text: '请选择不超过 2MB 的旧工资表 .xlsx 文件' })
      return
    }
    const bytes = new Uint8Array(await ruleSourceFile.arrayBuffer())
    let binary = ''
    for (let offset = 0; offset < bytes.length; offset += 0x8000) {
      binary += String.fromCharCode(...bytes.subarray(offset, offset + 0x8000))
    }
    await execute('IMPORT_RULES', {
      period: '2026-07',
      source_filename: ruleSourceFile.name,
      source_file_base64: window.btoa(binary),
    })
    setRuleSourceFile(null)
  }

  const reviewRuleIds = new Set(reviewRules.map((rule) => rule.rule_id))
  const reviewRuleTypes = new Set(reviewRules.map((rule) => rule.rule_type))
  const reviewRulesComplete = reviewRuleIds.size === reviewRules.length &&
    reviewRuleTypes.size === reviewRules.length && reviewRules.every((rule) => (
      rule.name.trim() && Number.isSafeInteger(rule.threshold_cents) &&
      rule.threshold_cents >= 0 &&
      (rule.rule_type === 'HISTORY_CHANGE_REVIEW' || rule.threshold_cents === 0)
    ))
  const rulesComplete = reviewRulesComplete && rules.length > 0 && rules.every(
    (rule) => rule.employee_name.trim() && /^\*{4}(?:\d{4}|\?{4})$/.test(rule.account_masked) &&
      rule.disbursement_company.trim() && rule.payment_channel &&
      rule.job_group.trim() && rule.location.trim(),
  )
  const evidenceRefs = evidenceSlots.map((item) => item.evidence_ref.trim())
  const evidenceComplete = evidenceRefs.every(
    (value) => /^[A-Za-z0-9][A-Za-z0-9._-]{2,127}$/.test(value),
  ) && new Set(evidenceRefs).size === evidenceSlots.length
  const receiptsComplete = evidenceComplete && receipts.length > 0 && receipts.every(
    (receipt) => receipt.status && receipt.amount !== '' && Number(receipt.amount) >= 0,
  )
  const pendingComplete = previousOpenItems.length === 0 || previousOpenItems.every((item) => {
    const resolution = pendingDecisions[item.pending_id]
    return resolution?.decision && resolution.reason.trim()
  })

  const updateRule = (employeeId: string, patch: Partial<EditableRule>) => {
    setRules((current) => current.map((rule) => rule.employee_id === employeeId
      ? { ...rule, ...patch }
      : rule))
  }

  const addEmployeeRule = () => {
    const suffix = Date.now().toString(36)
    setRules((current) => [...current, {
      employee_id: `employee_${suffix}`,
      employee_name: '',
      account_id: `account_${suffix}`,
      account_masked: '****????',
      disbursement_company: '',
      fixed_base_salary_cents: 0,
      fixed_allowance_cents: 0,
      fixed_adjustment_cents: 0,
      night_shift_rate_cents: 0,
      rest_days: 0,
      payment_channel: '',
      payment_kind: 'NORMAL',
      job_group: '',
      location: '',
    }])
  }

  const updateReviewRule = (ruleId: string, patch: Partial<PayrollLegacyReviewRule>) => {
    setReviewRules((current) => current.map((rule) => rule.rule_id === ruleId
      ? { ...rule, ...patch }
      : rule))
  }
  const addableReviewRule = reviewRuleDefinitions.find(
    (definition) => !reviewRules.some((rule) => rule.rule_type === definition.rule_type),
  )
  const addReviewRule = () => {
    if (!addableReviewRule) return
    setReviewRules((current) => [...current, {
      rule_id: addableReviewRule.rule_id,
      name: addableReviewRule.default_name,
      rule_type: addableReviewRule.rule_type,
      enabled: true,
      severity: addableReviewRule.default_severity,
      threshold_cents: addableReviewRule.default_threshold_cents,
    }])
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
          <small>{testWorkspace.data.materials.length} 份测试素材 · 不可付款 · 不可提交银行</small>
        </div>
      </header>

      <nav className="payroll-legacy-taskbar" aria-label="工资软件六项功能">
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
        <div className="payroll-legacy-loading"><ArrowClockwise size={20} />正在读取工资规则和月度结果</div>
      ) : (
        <div className="payroll-legacy-body">
          <div className="payroll-legacy-toolbar">
            <div>
              <strong>{workspace ? `工资工作区版本 ${workspace.revision}` : '尚未建立工资规则'}</strong>
              <span>{workspace ? `${workspace.rules.employees.length} 条独立员工规则 · ${workspace.batches.length} 个已生成账期` : '先管理工资规则，再确认素材生成工资'}</span>
            </div>
            {workspace && workspace.batches.length > 0 ? (
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

          {task === 'generate' ? (
            <div className="payroll-task-panel">
              <div className="payroll-task-heading">
                <div><span>01</span><h3>生成当月工资</h3></div>
                <p>不再导入外部工资表；以独立工资规则、下方唯一确认的三类素材和上月待办生成当月工资。</p>
              </div>
              <div className="payroll-source-form">
                <label>
                  <span>工资月份</span>
                  <select value={generationPeriod} onChange={(event) => setGenerationPeriod(event.target.value)}>
                    <option value="2026-07">2026-07</option>
                    <option value="2026-08">2026-08</option>
                  </select>
                </label>
              </div>
              {confirmedMaterials?.period === generationPeriod ? (
                <div className="payroll-confirmed-materials" aria-label="已确认工资素材">
                  <strong>{generationPeriod} 三类素材已唯一确认</strong>
                  <span>考勤表 {confirmedMaterials.material_ids.attendance.slice(-8).toUpperCase()}</span>
                  <span>阿姨考勤表 {confirmedMaterials.material_ids.aunt_attendance.slice(-8).toUpperCase()}</span>
                  <span>好评统计 {confirmedMaterials.material_ids.review_statistics.slice(-8).toUpperCase()}</span>
                </div>
              ) : <div className="payroll-inline-warning"><Warning size={17} />请在下方素材库选择这个月份，并为考勤表、阿姨考勤表、好评统计各唯一确认一个版本。</div>}
              {activeBatch?.period === generationPeriod ? (
                <AdjustmentEditor
                  lines={activeBatch.lines}
                  adjustments={adjustments}
                  onChange={setAdjustments}
                />
              ) : null}
              <div className="payroll-rule-section-title"><strong>上月待办并入本次生成</strong><span>有待办时必须逐项决定；没有待办可直接生成。</span></div>
              {previousOpenItems.length ? previousOpenItems.map((item) => (
                <div className="payroll-pending-row" key={item.pending_id}>
                  <div><strong>{item.employee_id}</strong><span>{item.source_period} · {item.direction === 'ADD' ? '少发' : '多发'} {money(item.amount_cents)}</span></div>
                  <select aria-label={`${item.employee_id}处理决定`} value={pendingDecisions[item.pending_id]?.decision ?? ''} onChange={(event) => setPendingDecisions((current) => ({ ...current, [item.pending_id]: { decision: event.target.value as 'ADD_TO_MAIN' | 'SUPPLEMENT' | 'IGNORE', reason: current[item.pending_id]?.reason ?? '' } }))}><option value="">请选择</option><option value="ADD_TO_MAIN">并入本月工资</option><option value="SUPPLEMENT">另行补发</option><option value="IGNORE">忽略并留痕</option></select>
                  <input aria-label={`${item.employee_id}处理原因`} value={pendingDecisions[item.pending_id]?.reason ?? ''} onChange={(event) => setPendingDecisions((current) => ({ ...current, [item.pending_id]: { decision: current[item.pending_id]?.decision ?? '', reason: event.target.value } }))} placeholder="填写处理原因" />
                </div>
              )) : <p className="payroll-empty-task">上一账期没有未解决的少发或多发事项。</p>}
              <button type="button" className="primary" disabled={!workspace || workspace.rules.employees.length === 0 || confirmedMaterials?.period !== generationPeriod || !pendingComplete || busy} onClick={generateMonthlyPayroll}>
                {busy ? '正在生成' : '确认并生成当月工资表'}
              </button>
            </div>
          ) : null}

          {task === 'rules' ? (
            <div className="payroll-task-panel">
              <div className="payroll-task-heading"><div><span>07</span><h3>工资计算与审查规则</h3></div><p>规则保存后刷新仍会保留；停用或删除的审查规则不会参与下一次检查。</p></div>
              {rules.length === 0 ? (
                <section className="payroll-rule-import" aria-labelledby="payroll-rule-import-heading">
                  <div>
                    <strong id="payroll-rule-import-heading">一次性建立七月工资规则基线</strong>
                    <span>仅在系统首次切换时，从原软件迁移 2026 年 7 月长期规则。完成后入口自动关闭，往后只在本网页保存和修改，不再依赖 Excel。</span>
                  </div>
                  <label className="payroll-rule-file">
                    <span>{ruleSourceFile?.name ?? '选择七月规则源（.xlsx）'}</span>
                    <input type="file" accept=".xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" onChange={(event) => setRuleSourceFile(event.target.files?.[0] ?? null)} />
                  </label>
                  <button type="button" className="primary" disabled={!ruleSourceFile || busy} onClick={() => void importRules()}>
                    {busy ? '正在建立基线' : '建立规则基线'}
                  </button>
                </section>
              ) : (
                <div className="payroll-rule-baseline-status">
                  <strong>七月规则基线已建立</strong>
                  <span>当前规则以网页保存内容为准；Excel 导入入口已关闭。</span>
                </div>
              )}
              <section className="payroll-review-rules" aria-labelledby="payroll-review-rules-heading">
                <div className="payroll-review-rules-heading">
                  <div><strong id="payroll-review-rules-heading">审查规则管理</strong><span>控制“检查规则与历史”实际执行的项目。</span></div>
                  <button type="button" className="secondary" disabled={!addableReviewRule || busy} onClick={addReviewRule}>新增审查规则</button>
                </div>
                {reviewRules.length ? reviewRules.map((rule) => {
                  const definition = reviewRuleDefinition(rule.rule_type)
                  return (
                    <article className="payroll-review-rule" data-enabled={rule.enabled} key={rule.rule_id}>
                      <div className="payroll-review-rule-title">
                        <strong>{definition.label}</strong>
                        <span>{rule.enabled ? '已启用' : '已停用'}</span>
                      </div>
                      <label>
                        <span>规则名称</span>
                        <input aria-label={`${definition.label}规则名称`} value={rule.name} maxLength={120} onChange={(event) => updateReviewRule(rule.rule_id, { name: event.target.value })} />
                      </label>
                      <label>
                        <span>提醒级别</span>
                        <select aria-label={`${definition.label}提醒级别`} value={rule.severity} onChange={(event) => updateReviewRule(rule.rule_id, { severity: event.target.value as PayrollLegacyReviewRule['severity'] })}>
                          <option value="REVIEW">需复核</option>
                          <option value="BLOCKING">阻断</option>
                        </select>
                      </label>
                      {rule.rule_type === 'HISTORY_CHANGE_REVIEW' ? (
                        <label>
                          <span>金额变化阈值（元）</span>
                          <input aria-label="跨月变化金额变化阈值" type="number" min="0" step="0.01" value={(rule.threshold_cents / 100).toFixed(2)} onChange={(event) => updateReviewRule(rule.rule_id, { threshold_cents: Math.round(Number(event.target.value) * 100) })} />
                        </label>
                      ) : <span className="payroll-review-rule-scope">按当前工资表直接核对</span>}
                      <div className="payroll-review-rule-actions">
                        <button type="button" onClick={() => updateReviewRule(rule.rule_id, { enabled: !rule.enabled })}>{rule.enabled ? `停用 ${rule.name}` : `启用 ${rule.name}`}</button>
                        <button type="button" className="danger" onClick={() => setReviewRules((current) => current.filter((item) => item.rule_id !== rule.rule_id))}>删除 {rule.name}</button>
                      </div>
                    </article>
                  )
                }) : <p className="payroll-empty-task">当前没有审查规则；可新增需要的检查项目。</p>}
              </section>
              <div className="payroll-rule-section-title"><strong>员工工资计算规则</strong><span>规则独立长期保存，只影响以后生成的工资，不改已生成历史。</span><button type="button" className="secondary" onClick={addEmployeeRule}>新增员工规则</button></div>
              <div className="payroll-rule-table" role="table" aria-label="工资规则">
                <div className="payroll-rule-row header" role="row">
                  <span>员工</span><span>账户尾号</span><span>代发公司</span><span>固定工资</span><span>固定津贴</span><span>固定增减</span><span>渠道</span><span>类型</span><span>夜班标准</span><span>可休天数</span><span>工种</span><span>地点</span><span>操作</span>
                </div>
                {rules.map((rule) => (
                  <div className="payroll-rule-row" role="row" key={rule.employee_id}>
                    <input aria-label={`${rule.employee_id}员工姓名`} value={rule.employee_name} onChange={(event) => updateRule(rule.employee_id, { employee_name: event.target.value })} placeholder="姓名" />
                    <input aria-label={`${rule.employee_id}账户尾号`} value={rule.account_masked.replace('****', '')} maxLength={4} onChange={(event) => updateRule(rule.employee_id, { account_masked: `****${event.target.value.replace(/[^0-9?]/g, '').slice(0, 4)}` })} placeholder="4 位尾号" />
                    <input aria-label={`${rule.employee_id}代发公司`} value={rule.disbursement_company} onChange={(event) => updateRule(rule.employee_id, { disbursement_company: event.target.value })} placeholder="网商代发公司" />
                    <MoneyInput label={`${rule.employee_name}固定工资`} cents={rule.fixed_base_salary_cents} onChange={(value) => updateRule(rule.employee_id, { fixed_base_salary_cents: value })} />
                    <MoneyInput label={`${rule.employee_name}固定津贴`} cents={rule.fixed_allowance_cents} onChange={(value) => updateRule(rule.employee_id, { fixed_allowance_cents: value })} />
                    <MoneyInput label={`${rule.employee_name}固定增减`} cents={rule.fixed_adjustment_cents ?? 0} onChange={(value) => updateRule(rule.employee_id, { fixed_adjustment_cents: value })} />
                    <select aria-label={`${rule.employee_name}发放渠道`} value={rule.payment_channel} onChange={(event) => updateRule(rule.employee_id, { payment_channel: event.target.value as EditableRule['payment_channel'] })}><option value="">请选择</option>{channels.map((channel) => <option key={channel}>{channel}</option>)}</select>
                    <select aria-label={`${rule.employee_name}发放类型`} value={rule.payment_kind} onChange={(event) => updateRule(rule.employee_id, { payment_kind: event.target.value as PayrollLegacyEmployeeRule['payment_kind'] })}>{paymentKinds.map((kind) => <option key={kind}>{kind}</option>)}</select>
                    <MoneyInput label={`${rule.employee_name}夜班标准`} cents={rule.night_shift_rate_cents} onChange={(value) => updateRule(rule.employee_id, { night_shift_rate_cents: value })} />
                    <input aria-label={`${rule.employee_name}可休天数`} type="number" min="0" max="31" value={rule.rest_days} onChange={(event) => updateRule(rule.employee_id, { rest_days: Number(event.target.value) })} />
                    <input aria-label={`${rule.employee_name}工种`} value={rule.job_group} onChange={(event) => updateRule(rule.employee_id, { job_group: event.target.value })} placeholder="必须确认" />
                    <input aria-label={`${rule.employee_name}地点`} value={rule.location} onChange={(event) => updateRule(rule.employee_id, { location: event.target.value })} placeholder="必须确认" />
                    <button type="button" className="danger" onClick={() => setRules((current) => current.filter((item) => item.employee_id !== rule.employee_id))}>删除</button>
                  </div>
                ))}
              </div>
              <div className="payroll-main-total"><span>当前规则固定工资合计</span><strong>{money(rules.reduce((sum, rule) => sum + rule.fixed_base_salary_cents + rule.fixed_allowance_cents + (rule.fixed_adjustment_cents ?? 0), 0))}</strong></div>
              <button type="button" className="primary" disabled={!rulesComplete || busy} onClick={saveRules}>保存全部工资规则</button>
            </div>
          ) : null}

          {task === 'normal' && activeBatch ? (
            <SimpleAction
              title="生成网商银行代发表"
              detail="以系统生成的工资表为准，按五家公司拆分最终代发表；下方同时预览五份代发表和工资发放表。"
              button="生成五份网商银行代发表"
              busy={busy}
              onRun={() => void execute('GENERATE_NORMAL_DRAFT', { period: activeBatch.period })}
            >
              <NormalDraftPreview batch={activeBatch} />
            </SimpleAction>
          ) : null}

          {task === 'supplemental' && activeBatch ? (
            <SimpleAction
              title="生成补发代发表"
              detail="只提取明确标为 SUPPLEMENT 的正向人工调整，不自动猜测补发金额。"
              button="生成补发代发表预览"
              busy={busy}
              onRun={() => void execute('GENERATE_SUPPLEMENTAL_DRAFT', { period: activeBatch.period })}
            />
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

          {task === 'verify' && activeBatch ? (
            <div className="payroll-task-panel">
              <div className="payroll-task-heading"><div><span>04</span><h3>复核本月已发并更新汇总</h3></div><p>系统生成工资表是理论应发基准；先核对五份网商流水、一份中国银行流水和李勇微信转账，逐人及总额全部一致后才更新汇总。</p></div>
              <div className="payroll-evidence-collection">
                <div>
                  <strong>账单收集完整度</strong>
                  <span>工资表理论总额：{money(activeBatch.lines.reduce((sum, line) => sum + line.net_pay_cents, 0))}</span>
                </div>
                <ul>
                  <li>网商银行实际发放流水 {evidenceSlots.filter((item) => item.evidence_type === 'MYBANK_STATEMENT' && item.evidence_ref.trim()).length}/5</li>
                  <li>中国银行实际发放流水 {evidenceSlots.some((item) => item.evidence_type === 'BOC_RECEIPT' && item.evidence_ref.trim()) ? 1 : 0}/1</li>
                  <li>李勇微信实际转账记录 {evidenceSlots.some((item) => item.evidence_type === 'WECHAT_RECEIPT' && item.evidence_ref.trim()) ? 1 : 0}/1</li>
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
              {activeBatch.summary ? <SummaryView batch={activeBatch} /> : <p className="payroll-empty-task">汇总尚未更新；必须先完成匹配复核。</p>}
              <button type="button" className="primary" disabled={!receiptsComplete || busy} onClick={() => void execute('VERIFY_AND_UPDATE_SUMMARY', { period: activeBatch.period, evidence_documents: evidenceSlots.map(({ evidence_type, evidence_ref }) => ({ evidence_type, evidence_ref: evidence_ref.trim() })), receipts: receipts.map((receipt) => ({ employee_id: receipt.employee_id, account_id: receipt.account_id, payment_channel: receipt.payment_channel, amount_cents: Math.round(Number(receipt.amount) * 100), status: receipt.status })) })}>先复核本月已发，匹配后更新汇总</button>
            </div>
          ) : null}

          {!activeBatch && !new Set<TaskId>(['generate', 'rules']).has(task) ? (
            <div className="payroll-empty-task">请先用“生成当月工资”建立一个工资账期。</div>
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
        <select aria-label="调整去向" value={disposition} onChange={(event) => setDisposition(event.target.value as 'MAIN' | 'SUPPLEMENT')}><option value="MAIN">并入当月工资</option><option value="SUPPLEMENT">另行补发</option></select>
        <input aria-label="调整原因" value={reason} onChange={(event) => setReason(event.target.value)} placeholder="必填原因" />
        <button type="button" className="secondary" onClick={add}>加入调整</button>
      </div>
      {adjustments.length ? <ul>{adjustments.map((item) => <li key={item.item_code}><span>{lines.find((line) => line.employee_id === item.employee_id)?.employee_name} · {item.reason}</span><strong>{money(item.amount_cents)}</strong><button type="button" onClick={() => onChange(adjustments.filter((candidate) => candidate.item_code !== item.item_code))}>移除</button></li>)}</ul> : null}
    </div>
  )
}

function SummaryView({ batch }: { batch: PayrollLegacyBatch }) {
  if (!batch.summary) return null
  return (
    <section className="payroll-monthly-summary" aria-labelledby="payroll-location-summary-heading">
      <div className="payroll-summary-strip"><span>总汇总 · {batch.summary.employee_count} 人</span><span>应发 {money(batch.summary.gross_pay_cents)}</span><strong>实发 {money(batch.summary.net_pay_cents)}</strong></div>
      {batch.summary.by_location?.length ? (
        <div>
          <h4 id="payroll-location-summary-heading">各店当月工资汇总</h4>
          <div className="payroll-location-summary-grid">
            {batch.summary.by_location.map((item) => (
              <article key={item.location}>
                <span>{item.location} · {item.employee_count} 人</span>
                <small>应发 {money(item.gross_pay_cents)}</small>
                <strong>实发 {money(item.net_pay_cents)}</strong>
              </article>
            ))}
          </div>
        </div>
      ) : null}
    </section>
  )
}

function NormalDraftPreview({ batch }: { batch: PayrollLegacyBatch }) {
  const drafts = batch.drafts.filter((draft) => draft.draft_type === 'normal_bank_payroll')
  return (
    <div className="payroll-output-previews">
      <section aria-label="网商银行代发表预览">
        <h4>五家公司代发表预览</h4>
        {drafts.length ? drafts.map((draft) => (
          <article key={draft.draft_id}>
            <div><strong>{draft.disbursement_company ?? '网商银行代发'}</strong><span>{draft.lines.length} 人 · {money(draft.total_amount_cents)}</span></div>
            <div className="payroll-mini-table">
              {draft.lines.map((line) => <span key={line.employee_id}>{line.employee_id} · {line.account_masked} · {money(line.amount_cents)}</span>)}
            </div>
          </article>
        )) : <p className="payroll-empty-task">点击生成后在这里显示五份代发表。</p>}
      </section>
      <section aria-label="工资发放表预览">
        <h4>工资发放表</h4>
        <div className="payroll-mini-table">
          {batch.lines.map((line) => (
            <span key={line.employee_id}>{line.employee_name} · {line.payment_channel} · {line.disbursement_company ?? line.location ?? '发放主体待确认'} · {money(line.net_pay_cents)}</span>
          ))}
        </div>
      </section>
    </div>
  )
}

function MainTable({ batch }: { batch: PayrollLegacyBatch }) {
  return (
    <div className="payroll-main-table">
      <div className="payroll-main-table-heading"><div><span>系统生成结果</span><h3>{batch.period} 工资表</h3></div><strong>{batch.lines.length} 人 · {money(batch.lines.reduce((sum, line) => sum + line.net_pay_cents, 0))}</strong></div>
      <div className="payroll-main-table-scroll">
        <table>
          <thead><tr><th>员工</th><th>账户</th><th>基本工资</th><th>津贴</th><th>奖金</th><th>扣款</th><th>社保</th><th>公积金</th><th>个税</th><th>实发</th><th>渠道</th></tr></thead>
          <tbody>{batch.lines.map((line) => <tr key={line.employee_id}><td>{line.employee_name}</td><td>{line.account_masked}</td><td>{money(line.base_salary_cents)}</td><td>{money(line.allowance_cents)}</td><td>{money(line.bonus_cents)}</td><td>{money(line.deduction_cents)}</td><td>{money(line.social_insurance_cents)}</td><td>{money(line.housing_fund_cents)}</td><td>{money(line.individual_income_tax_cents)}</td><td><strong>{money(line.net_pay_cents)}</strong></td><td>{line.payment_channel === 'UNASSIGNED' ? '待确认' : line.payment_channel}</td></tr>)}</tbody>
        </table>
      </div>
      {batch.source_exceptions.length ? <div className="payroll-blockers"><Warning size={17} /><span>{batch.source_exceptions.length} 个来源阻断/复核项；解决前不会生成代发草稿。</span></div> : null}
      {batch.drafts.length ? <div className="payroll-draft-list">{batch.drafts.map((draft) => <div key={draft.draft_id}><span>{draft.draft_type === 'normal_bank_payroll' ? `${draft.disbursement_company ?? '网商银行'}代发表` : '补发代发表'} · {draft.lines.length} 人</span><strong>{money(draft.total_amount_cents)}</strong><small>仅预览 / 不可提交</small></div>)}</div> : null}
    </div>
  )
}
