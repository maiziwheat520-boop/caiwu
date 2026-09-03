import { useCallback, useEffect, useRef, useState } from 'react'
import { Badge, Button } from '@radix-ui/themes'
import { CheckCircle, Database, Info, ShieldCheck, Warning } from '@phosphor-icons/react'
import { api, ApiError, minorToMajor } from '../api'
import type {
  PayrollBatchListData,
  PayrollDashboardData,
  PayrollMaterialListData,
  PayrollReadResponse,
  PayrollStatusData,
  PayrollTestWorkspaceReadResponse,
  PayrollVerificationListData,
} from '../types'
import { ErrorState, LoadingState, PageHeader } from '../shared/PagePrimitives'
import { PayrollLegacyWorkbench } from './PayrollLegacyWorkbench'
import { PayrollHistorySummary } from './PayrollHistorySummary'
import {
  PayrollTestWorkspaceActionsPanel,
  type PayrollConfirmedMaterials,
} from './PayrollTestWorkspaceActionsPanel'

const currency = new Intl.NumberFormat('zh-CN', { style: 'currency', currency: 'CNY' })

type PayrollLiveViews = {
  dashboard: PayrollReadResponse<PayrollDashboardData>
  materials: PayrollReadResponse<PayrollMaterialListData>
  batches: PayrollReadResponse<PayrollBatchListData>
  verification: PayrollReadResponse<PayrollVerificationListData>
}

const materialStatusLabel = (status: string) => ({
  NEEDS_REVIEW: '待人工审核',
  REVIEWED: '已审核',
  REJECTED: '已拒绝',
}[status] ?? '状态待确认')

const batchStatusLabel = (status: string) => ({
  DRAFT: '草稿',
  IN_REVIEW: '审核中',
  APPROVED: '已批准',
  LOCKED: '已锁定',
}[status.toUpperCase()] ?? '状态待确认')

const verificationStatusLabel = (status: string) => ({
  MATCHED: '已匹配',
  PARTIAL: '部分匹配',
  UNMATCHED: '待核对',
}[status.toUpperCase()] ?? '状态待确认')

const verificationEvidenceRequirements = [
  { evidenceType: 'MYBANK_STATEMENT', label: '网商银行代发表', requiredCount: 5 },
  { evidenceType: 'BOC_RECEIPT', label: '中国银行现金发放账单', requiredCount: 1 },
  { evidenceType: 'WECHAT_RECEIPT', label: '微信单独发放账单', requiredCount: 1 },
] as const

const evidenceTypeLabel = (evidenceType: string) => verificationEvidenceRequirements.find(
  (item) => item.evidenceType === evidenceType,
)?.label ?? evidenceType

const setupBlockerLabel = (code: string) => ({
  UNASSIGNED_MATERIALS: '工资材料仍有待归属项',
  MATERIAL_REVIEW_REQUIRED: '已归属材料仍需人工复核',
  PAYROLL_BATCH_REQUIRED: '尚未生成可核对的工资批次',
  LIVE_DATA_NOT_READY: '正式工资投影仍在准备中',
}[code] ?? '正式数据准备条件尚未满足')

const maskRef = (value: string) => value.length <= 10
  ? value
  : `${value.slice(0, 4)}••••${value.slice(-4)}`

const viewsAreConsistent = (
  status: PayrollReadResponse<PayrollStatusData>,
  views: PayrollLiveViews,
) => {
  const revisions = [
    status.data.projection_revision,
    views.dashboard.data.projection_revision,
    views.materials.data.projection_revision,
    views.batches.data.projection_revision,
    views.verification.data.projection_revision,
  ]
  const etags = [
    status.data.etag,
    views.dashboard.data.etag,
    views.materials.data.etag,
    views.batches.data.etag,
    views.verification.data.etag,
  ]
  const companies = [
    status.company_id,
    views.dashboard.company_id,
    views.materials.company_id,
    views.batches.company_id,
    views.verification.company_id,
  ]
  return revisions.every((revision) => revision === revisions[0])
    && etags.every((etag) => etag === etags[0])
    && companies.every((companyId) => companyId === companies[0])
}

async function readViews(): Promise<PayrollLiveViews> {
  const [dashboard, materials, batches, verification] = await Promise.all([
    api.getPayrollDashboard(),
    api.listPayrollMaterials(),
    api.listPayrollBatches(),
    api.listPayrollVerification(),
  ])
  return { dashboard, materials, batches, verification }
}

export function PayrollWorkspacePage() {
  const [status, setStatus] = useState<PayrollReadResponse<PayrollStatusData> | null>(null)
  const [testWorkspace, setTestWorkspace] = useState<PayrollTestWorkspaceReadResponse | null>(null)
  const [confirmedMaterials, setConfirmedMaterials] = useState<PayrollConfirmedMaterials | null>(null)
  const [views, setViews] = useState<PayrollLiveViews | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [csrfToken, setCsrfToken] = useState<string | null>(null)
  const [selectedEvidence, setSelectedEvidence] = useState<string[]>([])
  const [selectedBatchId, setSelectedBatchId] = useState('')
  const [commandBusy, setCommandBusy] = useState(false)
  const [commandMessage, setCommandMessage] = useState<{ tone: 'info' | 'error'; text: string } | null>(null)
  const commandBusyRef = useRef(false)

  const loadPayroll = useCallback(async () => {
    setLoading(true)
    setError(null)
    setViews(null)
    setTestWorkspace(null)
    try {
      const [sessionResponse, statusResponse, testWorkspaceResponse] = await Promise.all([
        api.getSession(),
        api.getPayrollStatus(),
        api.getPayrollTestWorkspace().catch((workspaceError) => {
          if (workspaceError instanceof ApiError && workspaceError.status === 404) return null
          throw workspaceError
        }),
      ])
      setCsrfToken(sessionResponse.csrf_token)
      setStatus(statusResponse)
      setTestWorkspace(testWorkspaceResponse)
      if (!statusResponse.data.live_data_ready) return

      let nextViews = await readViews()
      if (!viewsAreConsistent(statusResponse, nextViews)) nextViews = await readViews()
      if (!viewsAreConsistent(statusResponse, nextViews)) {
        setError('数据正在刷新，请稍后重试')
        return
      }
      setViews(nextViews)
    } catch {
      setStatus(null)
      setError('工资服务暂不可用，请稍后重试')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    const timer = window.setTimeout(() => void loadPayroll(), 0)
    return () => window.clearTimeout(timer)
  }, [loadPayroll])

  const dashboard = views?.dashboard.data.dashboard
  const materials = views?.materials.data.items ?? []
  const batches = views?.batches.data.items ?? []
  const verification = views?.verification.data
  const canVerifyReceipts = status?.data.live_data_ready === true
    && status.data.capabilities.commands_enabled
    && status.data.capabilities.allowed_actions.includes('VERIFY_RECEIPTS')
  const eligibleBatches = batches.filter((batch) => verification?.available_evidence.some(
    (evidence) => evidence.status === 'READY_FOR_MATCHING' && evidence.period === batch.pay_period,
  ))
  const selectedBatch = eligibleBatches.find((batch) => batch.batch_id === selectedBatchId)
    ?? (eligibleBatches.length === 1 ? eligibleBatches[0] : null)
  const evidenceForBatch = selectedBatch
    ? verification?.available_evidence.filter((evidence) => evidence.period === selectedBatch.pay_period) ?? []
    : []
  const selectedEvidenceRecords = evidenceForBatch.filter(
    (evidence) => selectedEvidence.includes(evidence.artifact_id),
  )
  const evidenceSetAvailable = verificationEvidenceRequirements.every(
    (requirement) => evidenceForBatch.filter(
      (evidence) => evidence.evidence_type === requirement.evidenceType,
    ).length >= requirement.requiredCount,
  )
  const selectedEvidenceComplete = verificationEvidenceRequirements.every(
    (requirement) => selectedEvidenceRecords.filter(
      (evidence) => evidence.evidence_type === requirement.evidenceType,
    ).length === requirement.requiredCount,
  ) && selectedEvidenceRecords.length === 7
  const testWorkspaceReady = testWorkspace?.data.auto_test_ready === true

  const toggleEvidence = (artifactId: string) => {
    setSelectedEvidence((current) => current.includes(artifactId)
      ? current.filter((item) => item !== artifactId)
      : [...current, artifactId])
  }

  const submitVerification = async () => {
    if (
      commandBusyRef.current || !canVerifyReceipts || !selectedBatch || !csrfToken ||
      !evidenceSetAvailable || !selectedEvidenceComplete
    ) return
    commandBusyRef.current = true
    setCommandBusy(true)
    setCommandMessage(null)
    try {
      await api.verifyPayrollReceipts({
        batchId: selectedBatch.batch_id,
        expectedRevision: selectedBatch.revision,
        sourceArtifactIds: selectedEvidence,
        csrfToken,
      })
      setCommandMessage({ tone: 'info', text: '请求已受理，正在刷新真实验证结果' })
      try {
        const refreshedViews = await readViews()
        if (!status || !viewsAreConsistent(status, refreshedViews)) {
          setCommandMessage({ tone: 'info', text: '请求已受理，结果待刷新' })
        } else {
          setViews(refreshedViews)
          setSelectedEvidence([])
          setCommandMessage({ tone: 'info', text: '请求已受理，验证结果已刷新' })
        }
      } catch {
        setCommandMessage({ tone: 'info', text: '请求已受理，结果待刷新' })
      }
    } catch (requestError) {
      const statusCode = requestError instanceof ApiError ? requestError.status : 0
      const text = statusCode === 403
        ? '当前会话无权提交发放验证'
        : statusCode === 409
          ? '工资批次已更新，请刷新后重新核对'
          : statusCode === 422
            ? '所选证据不再可用，请刷新后重试'
            : '发放验证暂不可用，请稍后重试'
      setCommandMessage({ tone: 'error', text })
    } finally {
      commandBusyRef.current = false
      setCommandBusy(false)
    }
  }

  return (
    <>
      <PageHeader
        eyebrow="工资模块"
        title="工资与发放验证"
        description="读取当前公司经过服务端隔离和脱敏的工资材料、批次与发放验证投影。"
      />

      {loading ? <LoadingState /> : error ? <ErrorState message={error} onRetry={loadPayroll} /> : null}

      {!loading && !error && testWorkspace ? (
        <div className="payroll-command-center">
          <PayrollHistorySummary key={testWorkspace.data.workspace_revision} workspace={testWorkspace} />
          {csrfToken ? (
            <PayrollLegacyWorkbench
              key={confirmedMaterials?.period ?? 'payroll-unconfirmed'}
              testWorkspace={testWorkspace}
              csrfToken={csrfToken}
              confirmedMaterials={confirmedMaterials}
            />
          ) : null}
        </div>
      ) : null}

      {!loading && !error && status && !status.data.live_data_ready ? (
        <>
          <div className="payroll-status-banner" role="status">
            <Info size={20} weight="fill" />
            <div>
              <strong>{testWorkspaceReady ? '七、八月工资测试账本已就绪' : testWorkspace ? '测试账本已创建，暂无七、八月工资素材' : '七、八月工资测试账本尚未就绪'}</strong>
              <span>{testWorkspaceReady ? '只接入 2026 年 7 月和 8 月；素材库只展示考勤表、阿姨考勤表和好评统计，生成的工资表与代发表不进入素材库。' : testWorkspace ? '工作区已就绪，但当前没有 2026 年 7 月或 8 月的工资表素材。' : '七、八月素材仍保留在来源库中；测试账本接通后会自动显示，当前不会虚报已入账。'}</span>
            </div>
            <Badge color={testWorkspace ? 'blue' : 'amber'}>{testWorkspaceReady ? '七八月测试账本' : testWorkspace ? '暂无七八月素材' : '待接通'}</Badge>
          </div>
          {testWorkspace && csrfToken ? (
            <PayrollTestWorkspaceActionsPanel
              workspace={testWorkspace}
              csrfToken={csrfToken}
              onWorkspaceChange={setTestWorkspace}
              onConfirmedMaterials={setConfirmedMaterials}
            />
          ) : null}
          {status.data.setup_summary?.provider_connected ? (
            <section className="panel payroll-setup-progress" aria-label="工资材料接入进度">
              <Database size={28} weight="light" />
              <div>
                <h2>服务已接通，待归属材料 {status.data.setup_summary.unassigned_material_count} 份</h2>
                <p>已识别可处理材料 {status.data.setup_summary.ready_material_count} 份，公司已映射 {status.data.setup_summary.company_mapped_material_count} 份；完成公司归属后生成正式工资投影。</p>
                {status.data.setup_summary.blocking_reason_codes.length > 0 ? (
                  <ul>
                    {status.data.setup_summary.blocking_reason_codes.map((code) => <li key={code}>{setupBlockerLabel(code)}</li>)}
                  </ul>
                ) : null}
              </div>
            </section>
          ) : null}
          <section className="payroll-status-grid" aria-label="工资模块连接状态">
            <article className="payroll-status-card ready">
              <CheckCircle size={26} weight="fill" />
              <div><h2>当前可做</h2><strong>只读工资发布契约已部署</strong><p>服务连通状态可核验，正式业务投影生成后才会展示材料与批次。</p></div>
            </article>
            <article className="payroll-status-card blocked">
              <Warning size={26} weight="fill" />
              <div><h2>暂不可做</h2><strong>真实发薪和银行提交不可用</strong><p>页面没有付款、发薪或银行提交入口。</p></div>
            </article>
          </section>
        </>
      ) : null}

      {!loading && !error && status?.data.live_data_ready && testWorkspace ? (
        csrfToken ? (
          <PayrollTestWorkspaceActionsPanel
            workspace={testWorkspace}
            csrfToken={csrfToken}
            onWorkspaceChange={setTestWorkspace}
            onConfirmedMaterials={setConfirmedMaterials}
          />
        ) : null
      ) : null}

      {!loading && !error && dashboard && verification ? (
        <div className="payroll-live-page">
          <section className="panel payroll-live-section" aria-labelledby="payroll-material-summary">
            <div className="section-heading payroll-live-heading">
              <div><span>正式投影</span><h2 id="payroll-material-summary">真实材料汇总</h2></div>
              <Badge color={dashboard.materials_needing_review_count > 0 ? 'amber' : 'green'}>
                {dashboard.materials_needing_review_count > 0 ? '需要复核' : '已就绪'}
              </Badge>
            </div>
            <div className="payroll-metric-grid">
              <div><strong>材料 {dashboard.material_count} 份</strong><span>当前公司已归属材料</span></div>
              <div><strong>待人工审核 {dashboard.materials_needing_review_count} 份</strong><span>仍需会计核对</span></div>
              <div><strong>待归属 {dashboard.unassigned_material_count} 份</strong><span>仅展示聚合数量</span></div>
              <div><strong>批次 {dashboard.batch_count} 个</strong><span>当前公司工资批次</span></div>
              <div><strong>验证结果 {verification.items.length} 条</strong><span>真实发放核验投影</span></div>
            </div>
            {materials.length > 0 ? (
              <div className="payroll-record-list" aria-label="工资材料">
                {materials.map((material) => (
                  <article key={material.material_id}>
                    <div><strong>{material.period ?? '期间待确认'}</strong><span>{material.material_type === 'PAYROLL_SHEET' ? '工资表' : '受控工资材料'}</span></div>
                    <Badge color={material.status === 'NEEDS_REVIEW' ? 'amber' : 'green'}>{materialStatusLabel(material.status)}</Badge>
                  </article>
                ))}
              </div>
            ) : <p className="payroll-empty-state">当前公司暂无工资材料</p>}
          </section>

          <section className="panel payroll-live-section" aria-labelledby="payroll-batches-heading">
            <div className="section-heading payroll-live-heading"><div><span>批次</span><h2 id="payroll-batches-heading">公司内工资批次</h2></div></div>
            {batches.length > 0 ? (
              <div className="payroll-batch-grid">
                {batches.map((batch) => (
                  <article key={batch.batch_id}>
                    <div className="payroll-batch-title"><strong>{batch.pay_period}</strong><Badge color="gray">{batchStatusLabel(batch.status)}</Badge></div>
                    <dl>
                      <div><dt>人数</dt><dd>{batch.lines.length}</dd></div>
                      <div><dt>实发合计</dt><dd>{currency.format(minorToMajor(batch.lines.reduce((total, line) => total + line.net_pay_minor, 0)))}</dd></div>
                      <div><dt>审计闭环</dt><dd>{batch.audit_closure ? '已记录' : '待形成'}</dd></div>
                    </dl>
                    {batch.lines.length > 0 ? (
                      <ul className="payroll-batch-lines">
                        {batch.lines.map((line) => (
                          <li key={`${line.employee_id}-${line.account_id}`}>
                            <span>{line.employee_display} · {line.account_display}</span>
                            <strong>{currency.format(minorToMajor(line.net_pay_minor))}</strong>
                          </li>
                        ))}
                      </ul>
                    ) : null}
                  </article>
                ))}
              </div>
            ) : <p className="payroll-empty-state">当前公司暂无工资批次</p>}
          </section>

          <section className="panel payroll-live-section" aria-labelledby="payroll-verification-heading">
            <div className="section-heading payroll-live-heading"><div><span>受控核验</span><h2 id="payroll-verification-heading">发放验证结果</h2></div></div>
            {verification.items.length > 0 ? (
              <div className="payroll-record-list" aria-label="发放验证记录">
                {verification.items.map((item) => (
                  <article key={item.verification_id} className="payroll-verification-record">
                    <div><strong>{batches.find((batch) => batch.batch_id === item.batch_id)?.pay_period ?? '期间待确认'}</strong><span>批次 {maskRef(item.batch_id)}</span></div>
                    <Badge color={item.status.toUpperCase() === 'MATCHED' ? 'green' : 'amber'}>{verificationStatusLabel(item.status)}</Badge>
                    {item.results.length > 0 ? (
                      <ul>
                        {item.results.map((result) => (
                          <li key={`${result.employee_id}-${result.account_id}`}>
                            <span>{result.employee_display} · {result.account_display}</span>
                            <strong>{verificationStatusLabel(result.status)}</strong>
                          </li>
                        ))}
                      </ul>
                    ) : null}
                  </article>
                ))}
              </div>
            ) : <p className="payroll-empty-state">当前还没有发放验证结果</p>}

            <div className="payroll-evidence-panel">
              <h3>可用于核验的真实证据</h3>
              {verification.available_evidence.length === 0 ? (
                <p className="payroll-evidence-required">请先导入发放回单/流水</p>
              ) : canVerifyReceipts ? (
                <div className="payroll-evidence-command">
                  {eligibleBatches.length > 1 ? (
                    <label>
                      <span>选择工资批次</span>
                      <select value={selectedBatchId} onChange={(event) => { setSelectedBatchId(event.target.value); setSelectedEvidence([]) }}>
                        <option value="">请选择</option>
                        {eligibleBatches.map((batch) => <option key={batch.batch_id} value={batch.batch_id}>{batch.pay_period}</option>)}
                      </select>
                    </label>
                  ) : null}
                  <div className="payroll-evidence-options">
                    <div className="payroll-evidence-completeness">
                      <strong>本月应收 7 份账单</strong>
                      <span>工资表理论总额：{currency.format(minorToMajor(selectedBatch?.lines.reduce((sum, line) => sum + line.net_pay_minor, 0) ?? 0))}</span>
                      <ul>
                        {verificationEvidenceRequirements.map((requirement) => {
                          const received = evidenceForBatch.filter(
                            (evidence) => evidence.evidence_type === requirement.evidenceType,
                          ).length
                          return <li key={requirement.evidenceType}>{requirement.label} {received}/{requirement.requiredCount}</li>
                        })}
                      </ul>
                    </div>
                    {evidenceForBatch.map((evidence) => (
                      <label key={evidence.artifact_id}>
                        <input
                          type="checkbox"
                          checked={selectedEvidence.includes(evidence.artifact_id)}
                          onChange={() => toggleEvidence(evidence.artifact_id)}
                        />
                        <span>{evidenceTypeLabel(evidence.evidence_type)} · {evidence.period}</span>
                      </label>
                    ))}
                  </div>
                  {selectedBatch ? (
                    <>
                      {!evidenceSetAvailable ? <p className="payroll-evidence-required">三类账单尚未收齐：需 5 份网商银行、1 份中行、1 份微信账单。</p> : null}
                      <Button
                        disabled={commandBusy || !evidenceSetAvailable || !selectedEvidenceComplete}
                        onClick={() => void submitVerification()}
                      >
                        {commandBusy ? '正在提交' : selectedEvidenceComplete ? '提交发放验证' : '选择全部 7 份账单'}
                      </Button>
                    </>
                  ) : null}
                </div>
              ) : (
                <ul>
                  {verification.available_evidence.map((evidence) => (
                    <li key={evidence.artifact_id}><ShieldCheck size={18} weight="fill" /><span>{evidenceTypeLabel(evidence.evidence_type)} · {evidence.period}</span></li>
                  ))}
                </ul>
              )}
              {commandMessage ? <p className={`payroll-command-message ${commandMessage.tone}`} role={commandMessage.tone === 'error' ? 'alert' : 'status'}>{commandMessage.text}</p> : null}
            </div>
          </section>
        </div>
      ) : null}
    </>
  )
}
