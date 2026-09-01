import { useCallback, useEffect, useState } from 'react'
import { Badge } from '@radix-ui/themes'
import { Bank, Database, Info, Warning } from '@phosphor-icons/react'
import { api, minorToMajor } from '../api'
import type {
  CompanyReportAggregate,
  CompanyReportCompany,
  CompanyReportLayer,
  CompanyReportMonth,
  CompanyReportsResponse,
} from '../types'
import { ErrorState, LoadingState, PageHeader } from '../shared/PagePrimitives'

export function CompanyReportsPage() {
  const [reports, setReports] = useState<CompanyReportsResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const loadReports = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      setReports(await api.getCompanyReports())
    } catch (loadError) {
      setReports(null)
      const detail = loadError instanceof Error ? loadError.message : '公司报表暂不可用'
      setError(`公司报表层暂不可用，未显示任何 0 值。${detail}`)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    const loadTimer = window.setTimeout(() => void loadReports(), 0)
    return () => window.clearTimeout(loadTimer)
  }, [loadReports])

  const header = (
    <PageHeader
      eyebrow="公司维度"
      title="各公司报表"
      description="按授权公司主体展示三层真实事实；正式收入、费用与利润仅来自已入账总账。"
    />
  )

  if (loading) {
    return <>{header}<LoadingState title="正在读取公司报表" description="正在分别读取已确认来源、账户流水与正式入账投影。" /></>
  }
  if (error) return <>{header}<ErrorState message={error} onRetry={() => void loadReports()} /></>
  if (!reports) return null

  const companyIndex = new Map<string, { name: string; currencyCode: string }>()
  reports.layers.forEach((layer) => layer.items.forEach((company) => {
    const current = companyIndex.get(company.company_ref)
    if (!current || layer.basis === 'POSTED_LEDGER') {
      companyIndex.set(company.company_ref, {
        name: company.company_name,
        currencyCode: company.currency,
      })
    }
  }))

  if (companyIndex.size === 0) {
    return (
      <>
        {header}
        <section className="empty-state company-report-empty">
          <Database size={34} weight="light" />
          <h2>当前期间没有可展示的公司报表</h2>
          <p>未按名称、摘要或银行信息猜测公司归属；待 Core 提供权威公司事实后自动显示。</p>
        </section>
      </>
    )
  }
  const onlyCompany = companyIndex.size === 1 ? [...companyIndex.values()][0] : null
  const genericCompanyOnly = onlyCompany?.name.trim().toLowerCase() === 'ledgerbridge controlled reconciliation'

  return (
    <>
      {header}
      <section className="company-report-basis-note" aria-label="公司报表口径说明">
        <Info size={18} />
        <div>
          <strong>三层事实彼此独立，不合并计算</strong>
          <span>已确认候选用于当前测试收支汇总，账户流水用于现金流核对；两者均不与正式账簿混算。</span>
        </div>
      </section>
      {genericCompanyOnly ? (
        <section className="company-attribution-warning" role="alert">
          <Warning size={20} />
          <div>
            <strong>待完成公司归属</strong>
            <span>当前 Core 只返回一个通用公司主体，已导入数据尚未分配到各家公司；下方汇总不代表公司报表已完整。</span>
          </div>
        </section>
      ) : null}
      <div className="company-report-list">
        {[...companyIndex.entries()].map(([companyRef, identity]) => (
          <CompanyReportCard
            key={companyRef}
            companyRef={companyRef}
            companyName={identity.name}
            currencyCode={identity.currencyCode}
            postedLedgerStatus={reports.posted_ledger_status}
            layers={reports.layers}
          />
        ))}
      </div>
    </>
  )
}

function layerFor(reports: CompanyReportLayer[], basis: CompanyReportLayer['basis']) {
  return reports.find((layer) => layer.basis === basis)
}

function companyFor(layer: CompanyReportLayer | undefined, companyRef: string) {
  return layer?.items.find((company) => company.company_ref === companyRef)
}

function monthFor(company: CompanyReportCompany | undefined, month: string) {
  return company?.months.find((item) => item.month === month)
}

function postedMetrics(aggregate: CompanyReportAggregate | undefined) {
  return aggregate?.metrics.basis === 'POSTED_LEDGER' ? aggregate.metrics : null
}

function confirmedMetrics(aggregate: CompanyReportAggregate | undefined) {
  return aggregate?.metrics.basis === 'CONFIRMED_CANDIDATE' ? aggregate.metrics : null
}

function statementMetrics(aggregate: CompanyReportAggregate | undefined) {
  return aggregate?.metrics.basis === 'ACCOUNT_STATEMENT' ? aggregate.metrics : null
}

function reportMoney(amountMinor: number, currencyCode: string) {
  try {
    return new Intl.NumberFormat('zh-CN', {
      style: 'currency',
      currency: currencyCode,
      minimumFractionDigits: 2,
    }).format(minorToMajor(amountMinor))
  } catch {
    return `${currencyCode} ${minorToMajor(amountMinor).toFixed(2)}`
  }
}

function reportMonthLabel(month: string) {
  const [year, value] = month.split('-')
  return `${year} 年 ${Number(value)} 月`
}

function CompanyReportCard({ companyRef, companyName, currencyCode, postedLedgerStatus, layers }: {
  companyRef: string
  companyName: string
  currencyCode: string
  postedLedgerStatus: CompanyReportsResponse['posted_ledger_status']
  layers: CompanyReportLayer[]
}) {
  const candidate = companyFor(layerFor(layers, 'CONFIRMED_CANDIDATE'), companyRef)
  const statement = companyFor(layerFor(layers, 'ACCOUNT_STATEMENT'), companyRef)
  const posted = companyFor(layerFor(layers, 'POSTED_LEDGER'), companyRef)
  const candidateData = confirmedMetrics(candidate)
  const statementData = statementMetrics(statement)
  const postedData = postedMetrics(posted)
  const postedAvailable = postedLedgerStatus === 'AVAILABLE' && postedData !== null
  const postedHasEntries = postedAvailable && postedData.posted_entry_count > 0
  const postedMoney = (amountMinor: number | undefined) => postedAvailable && amountMinor !== undefined
    ? reportMoney(amountMinor, currencyCode)
    : '待接正式账簿'
  const monthKeys = new Set<string>()
  ;[candidate, statement, posted].forEach((company) => company?.months.forEach((month) => monthKeys.add(month.month)))
  const months = [...monthKeys].sort((left, right) => right.localeCompare(left))

  return (
    <article className="company-report-card">
      <header className="company-report-card-header">
        <div>
          <span className="eyebrow">权威公司主体</span>
          <h2>{companyName}</h2>
        </div>
        <Badge color="blue">{currencyCode}</Badge>
      </header>

      {candidateData || statementData ? (
        <section className="company-test-summary" aria-label={`${companyName} 测试汇总`}>
          <header>
            <div><strong>测试汇总·未正式入账</strong><span>来自已导入且已确认的候选事项，不冒充正式账簿。</span></div>
            <Badge color="amber">测试口径</Badge>
          </header>
          <div className="company-test-totals">
            <ReportTotal label="测试汇总收入" value={reportMoney(candidateData?.confirmed_positive_minor ?? 0, currencyCode)} />
            <ReportTotal label="测试汇总支出" value={reportMoney(Math.abs(candidateData?.confirmed_negative_minor ?? 0), currencyCode)} />
            <ReportTotal label="测试汇总净额" value={reportMoney(candidateData?.confirmed_net_minor ?? 0, currencyCode)} emphasis />
          </div>
          {statementData ? (
            <div className="company-statement-summary">
              <span>账户流水净额 <strong>{reportMoney(statementData.net_cash_flow_minor, currencyCode)}</strong></span>
              <small>{statementData.confirmed_transaction_count} 条流水·{statementData.statement_count} 份账单</small>
            </div>
          ) : null}
        </section>
      ) : null}

      {!postedHasEntries ? <p className="company-formal-empty">{postedAvailable ? '正式账簿尚无入账金额' : '正式账簿尚未接入'}</p> : null}
      <section className="company-report-totals" aria-label={`${companyName} 正式财务总额`}>
        <ReportTotal label="正式收入" value={postedMoney(postedData?.revenue_minor)} />
        <ReportTotal label="正式费用" value={postedMoney(postedData?.expense_minor)} />
        <ReportTotal label="正式利润" value={postedMoney(postedData?.profit_minor)} emphasis />
      </section>

      <section className="company-report-layers" aria-label={`${companyName} 三层事实`}>
        <div>
          <span>已确认来源</span>
          <strong>已确认来源 {candidateData?.confirmed_count ?? 0} 条</strong>
          <small>{candidateData?.source_count ?? 0} 个来源；上方仅作测试收支汇总，未正式入账</small>
        </div>
        <div>
          <span>账户流水</span>
          <strong>账户流水 {statementData?.confirmed_transaction_count ?? 0} 条</strong>
          <small>{statementData?.statement_count ?? 0} 份对账单；净现金流 {reportMoney(statementData?.net_cash_flow_minor ?? 0, currencyCode)}，非利润</small>
        </div>
        <div>
          <span>正式入账</span>
          <strong>{postedAvailable ? `正式入账 ${postedData.posted_entry_count} 条` : '待接正式账簿'}</strong>
          <small>{postedAvailable ? `${postedData.source_count} 个入账来源` : '正式账簿接口暂不可用，未显示任何 0 值'}</small>
        </div>
      </section>

      {(candidate?.pending_review_count ?? 0) > 0 ? (
        <ReportAlert>{candidate?.pending_review_count} 条来源待审核</ReportAlert>
      ) : null}
      {(candidate?.attribution_pending_count ?? 0) > 0 ? (
        <ReportAlert>{candidate?.attribution_pending_count} 条已确认来源待账户或经济性质归属</ReportAlert>
      ) : null}
      {(statement?.attribution_pending_count ?? 0) > 0 ? (
        <ReportAlert>{statement?.attribution_pending_count} 条账户流水待分配业务单元；仍计入公司级现金流，不进入业务单元</ReportAlert>
      ) : null}

      <div className="company-balance-unavailable">
        <Bank size={18} />
        <div><strong>余额基础尚未建立</strong><span>没有权威期初或期末余额，不显示 0，也不由净现金流倒推。</span></div>
      </div>

      {months.length > 0 ? (
        <div className="company-month-list">
          {months.map((month) => (
            <CompanyReportMonthCard
              key={month}
              month={month}
              currencyCode={currencyCode}
              candidate={monthFor(candidate, month)}
              statement={monthFor(statement, month)}
              posted={monthFor(posted, month)}
              postedLedgerStatus={postedLedgerStatus}
            />
          ))}
        </div>
      ) : (
        <p className="company-report-no-months">当前期间没有可计入月份的权威事实。</p>
      )}
    </article>
  )
}

function ReportTotal({ label, value, emphasis = false }: { label: string; value: string; emphasis?: boolean }) {
  return <div className={emphasis ? 'emphasis' : ''}><span>{label}</span><strong>{value}</strong></div>
}

function ReportAlert({ children }: { children: React.ReactNode }) {
  return <div className="company-report-alert"><Warning size={17} /><strong>{children}</strong><span>不计入正式财务总额</span></div>
}

function CompanyReportMonthCard({ month, currencyCode, candidate, statement, posted, postedLedgerStatus }: {
  month: string
  currencyCode: string
  candidate: CompanyReportMonth | undefined
  statement: CompanyReportMonth | undefined
  posted: CompanyReportMonth | undefined
  postedLedgerStatus: CompanyReportsResponse['posted_ledger_status']
}) {
  const candidateData = confirmedMetrics(candidate)
  const statementData = statementMetrics(statement)
  const postedData = postedMetrics(posted)
  const postedAvailable = postedLedgerStatus === 'AVAILABLE' && postedData !== null
  const postedMoney = (amountMinor: number | undefined) => postedAvailable && amountMinor !== undefined
    ? reportMoney(amountMinor, currencyCode)
    : '待接正式账簿'
  const postedBusinessUnits = postedAvailable && posted?.business_unit_breakdown_status === 'AVAILABLE'
    ? posted.business_units
    : []

  return (
    <section className="company-month-card">
      <header>
        <div><span>归属月份</span><h3>{reportMonthLabel(month)}</h3></div>
        <small>{candidateData?.confirmed_count ?? 0} 条来源 · {statementData?.confirmed_transaction_count ?? 0} 条流水 · {postedAvailable ? `${postedData.posted_entry_count} 条入账` : '待接正式账簿'}</small>
      </header>
      <div className="company-month-totals">
        <span>正式收入 <strong>{postedMoney(postedData?.revenue_minor)}</strong></span>
        <span>正式费用 <strong>{postedMoney(postedData?.expense_minor)}</strong></span>
        <span>正式利润 <strong>{postedMoney(postedData?.profit_minor)}</strong></span>
      </div>
      {!postedAvailable ? (
        <p className="company-breakdown-state warning">正式账簿接口暂不可用；未显示任何 0 值。</p>
      ) : null}
      {statement?.business_unit_breakdown_status === 'UNAVAILABLE_ATTRIBUTION_PENDING' ? (
        <p className="company-breakdown-state warning">账户流水的业务单元归属待补；公司级现金流仍保留。</p>
      ) : null}
      {postedAvailable && posted?.business_unit_breakdown_status === 'UNAVAILABLE_MISSING_SNAPSHOT' ? (
        <p className="company-breakdown-state warning">历史业务单元快照缺失；未使用当前维度名称回填。</p>
      ) : null}
      {postedAvailable && posted?.business_unit_breakdown_status === 'EMPTY' ? (
        <p className="company-breakdown-state">正式入账层的业务单元事实确认为空。</p>
      ) : null}
      {postedBusinessUnits.length > 0 ? (
        <div className="company-business-units">
          {postedBusinessUnits.map((unit) => {
            const unitMetrics = postedMetrics(unit)
            return (
              <article key={unit.business_unit_ref}>
                <div><span>业务单元</span><strong>{unit.business_unit_label}</strong></div>
                <dl>
                  <div><dt>收入</dt><dd>{reportMoney(unitMetrics?.revenue_minor ?? 0, currencyCode)}</dd></div>
                  <div><dt>费用</dt><dd>{reportMoney(unitMetrics?.expense_minor ?? 0, currencyCode)}</dd></div>
                  <div><dt>利润</dt><dd>{reportMoney(unitMetrics?.profit_minor ?? 0, currencyCode)}</dd></div>
                </dl>
              </article>
            )
          })}
        </div>
      ) : null}
    </section>
  )
}
