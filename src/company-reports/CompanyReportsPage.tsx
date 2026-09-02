import { useCallback, useEffect, useState } from 'react'
import { Badge } from '@radix-ui/themes'
import { Bank, Database, Info, Warning } from '@phosphor-icons/react'
import { api, minorToMajor } from '../api'
import type {
  CompanyReportAggregate,
  CompanyReportCategoryComposition,
  CompanyReportCategorySlice,
  CompanyReportCompany,
  CompanyReportCompositionItem,
  CompanyReportLayer,
  CompanyReportMonth,
  CompanyReportsResponse,
} from '../types'
import { ErrorState, LoadingState, PageHeader } from '../shared/PagePrimitives'
import { CompanyBankStatementReviewPanel } from './CompanyBankStatementReviewPanel'

export function CompanyReportsPage({ csrfToken }: { csrfToken: string }) {
  const [reports, setReports] = useState<CompanyReportsResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [selectedCompanyRef, setSelectedCompanyRef] = useState('')
  const [basis, setBasis] = useState<CompanyReportLayer['basis']>('CONFIRMED_CANDIDATE')
  const [fromMonth, setFromMonth] = useState('')
  const [toMonth, setToMonth] = useState('')
  const [appliedRange, setAppliedRange] = useState<{ fromMonth: string; toMonth: string } | null>(null)

  const loadReports = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const response = await api.getCompanyReports(appliedRange ?? {})
      setReports(response)
      const statementLayer = response.layers.find((layer) => layer.basis === 'ACCOUNT_STATEMENT')
      setBasis(statementLayer?.items.some((item) => (
        item.metrics.basis === 'ACCOUNT_STATEMENT'
        && item.metrics.confirmed_transaction_count > 0
      )) ? 'ACCOUNT_STATEMENT' : 'CONFIRMED_CANDIDATE')
      setFromMonth(response.from_month)
      setToMonth(response.to_month)
    } catch (loadError) {
      setReports(null)
      const detail = loadError instanceof Error ? loadError.message : '公司报表暂不可用'
      setError(`公司报表层暂不可用，未显示任何 0 值。${detail}`)
    } finally {
      setLoading(false)
    }
  }, [appliedRange])

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
    return <>{header}<CompanyBankStatementReviewPanel csrfToken={csrfToken} /><LoadingState title="正在读取公司报表" description="正在分别读取已确认来源、账户流水与正式入账投影。" /></>
  }
  if (error) return <>{header}<CompanyBankStatementReviewPanel csrfToken={csrfToken} /><ErrorState message={error} onRetry={() => void loadReports()} /></>
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

  const companies = [...companyIndex.entries()]
  const activeCompanyRef = companyIndex.has(selectedCompanyRef)
    ? selectedCompanyRef
    : companies[0]?.[0] ?? ''
  const activeCompany = companyIndex.get(activeCompanyRef)
  const activeComposition = compositionFor(reports, basis, activeCompanyRef)
  const activeReport = companyFor(layerFor(reports.layers, basis), activeCompanyRef)
  const dashboard = dashboardSummary(basis, activeReport, activeComposition)
  const rangeInvalid = !isReportMonth(fromMonth)
    || !isReportMonth(toMonth)
    || fromMonth > toMonth
    || monthDistance(fromMonth, toMonth) >= 24

  const toolbar = (
    <section className="company-report-toolbar" aria-label="报表筛选">
      <label>
        <span>公司</span>
        <select
          aria-label="选择公司"
          value={activeCompanyRef}
          disabled={companies.length === 0}
          onChange={(event) => setSelectedCompanyRef(event.target.value)}
        >
          {companies.map(([companyRef, identity]) => (
            <option key={companyRef} value={companyRef}>{identity.name}</option>
          ))}
        </select>
      </label>
      <label>
        <span>开始月份</span>
        <input aria-label="开始月份" type="month" value={fromMonth} onChange={(event) => setFromMonth(event.target.value)} />
      </label>
      <label>
        <span>结束月份</span>
        <input aria-label="结束月份" type="month" value={toMonth} onChange={(event) => setToMonth(event.target.value)} />
      </label>
      <button
        type="button"
        className="primary-button company-report-apply"
        disabled={rangeInvalid}
        onClick={() => setAppliedRange({ fromMonth, toMonth })}
      >
        应用期间
      </button>
      <button type="button" className="secondary-button" onClick={() => void loadReports()}>
        重新加载
      </button>
      {rangeInvalid ? <p role="alert">请选择不超过 24 个月的有效月份范围。</p> : null}
    </section>
  )

  if (companyIndex.size === 0) {
    return (
      <>
        {header}
        {toolbar}
        <CompanyBankStatementReviewPanel csrfToken={csrfToken} />
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
      {toolbar}
      <section className="company-report-basis-note" aria-label="公司报表口径说明">
        <Info size={18} />
        <div>
          <strong>正式数据的不同处理阶段分开展示</strong>
          <span>银行账单和其流水是正式业务数据；已确认事项与会计已过账结果分开计算。</span>
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
      <section className="company-financial-dashboard" aria-label={`${activeCompany?.name ?? '公司'} 财务汇总`}>
        <header className="company-dashboard-header">
          <div>
            <span className="eyebrow">{reports.from_month} 至 {reports.to_month}</span>
            <h2>{activeCompany?.name}</h2>
            <p>{basisDescription(basis)}</p>
          </div>
          <div className="company-basis-switch" role="group" aria-label="汇总口径">
            <button
              type="button"
              aria-pressed={basis === 'ACCOUNT_STATEMENT'}
              className={basis === 'ACCOUNT_STATEMENT' ? 'active' : ''}
              onClick={() => setBasis('ACCOUNT_STATEMENT')}
            >正式银行流水</button>
            <button
              type="button"
              aria-pressed={basis === 'CONFIRMED_CANDIDATE'}
              className={basis === 'CONFIRMED_CANDIDATE' ? 'active' : ''}
              onClick={() => setBasis('CONFIRMED_CANDIDATE')}
            >已确认事项</button>
            <button
              type="button"
              aria-pressed={basis === 'POSTED_LEDGER'}
              className={basis === 'POSTED_LEDGER' ? 'active' : ''}
              onClick={() => setBasis('POSTED_LEDGER')}
            >正式账簿</button>
          </div>
        </header>
        <div className="company-dashboard-totals">
          <ReportTotal label="总收入" value={dashboard.available ? reportMoney(dashboard.incomeMinor, activeCompany?.currencyCode ?? 'CNY') : '待接正式账簿'} />
          <ReportTotal label="总支出" value={dashboard.available ? reportMoney(dashboard.expenseMinor, activeCompany?.currencyCode ?? 'CNY') : '待接正式账簿'} />
          <ReportTotal label="净额" value={dashboard.available ? reportMoney(dashboard.netMinor, activeCompany?.currencyCode ?? 'CNY') : '待接正式账簿'} emphasis />
        </div>
        <div className="company-composition-grid">
          <CategoryShareChart
            title="收入类型占比"
            composition={dashboard.incomeComposition}
            currencyCode={activeCompany?.currencyCode ?? 'CNY'}
            tone="income"
            unavailable={!dashboard.available}
            emptyMessage={basis === 'ACCOUNT_STATEMENT' ? '账户流水尚未完成收支类型分类，当前仅展示现金流总额。' : undefined}
          />
          <CategoryShareChart
            title="支出类型占比"
            composition={dashboard.expenseComposition}
            currencyCode={activeCompany?.currencyCode ?? 'CNY'}
            tone="expense"
            unavailable={!dashboard.available}
            emptyMessage={basis === 'ACCOUNT_STATEMENT' ? '账户流水尚未完成收支类型分类，当前仅展示现金流总额。' : undefined}
          />
        </div>
      </section>
      <CompanyBankStatementReviewPanel csrfToken={csrfToken} />
      <div className="company-report-list">
        {activeCompany ? [[activeCompanyRef, activeCompany] as const].map(([companyRef, identity]) => (
          <CompanyReportCard
            key={companyRef}
            companyRef={companyRef}
            companyName={identity.name}
            currencyCode={identity.currencyCode}
            postedLedgerStatus={reports.posted_ledger_status}
            layers={reports.layers}
          />
        )) : null}
      </div>
    </>
  )
}

function monthDistance(fromMonth: string, toMonth: string) {
  const [fromYear, fromValue] = fromMonth.split('-').map(Number)
  const [toYear, toValue] = toMonth.split('-').map(Number)
  return (toYear * 12 + toValue) - (fromYear * 12 + fromValue)
}

function isReportMonth(value: string) {
  return /^\d{4}-(0[1-9]|1[0-2])$/.test(value)
}

function compositionFor(
  reports: CompanyReportsResponse,
  basis: CompanyReportLayer['basis'],
  companyRef: string,
) {
  if (basis === 'ACCOUNT_STATEMENT') return undefined
  return reports.compositions
    ?.find((layer) => layer.basis === basis)
    ?.items.find((item) => item.company_ref === companyRef)
}

function dashboardSummary(
  basis: CompanyReportLayer['basis'],
  report: CompanyReportCompany | undefined,
  composition: CompanyReportCompositionItem | undefined,
) {
  if (basis === 'CONFIRMED_CANDIDATE') {
    const metrics = confirmedMetrics(report)
    return {
      available: metrics !== null,
      incomeMinor: metrics?.confirmed_positive_minor ?? 0,
      expenseMinor: Math.abs(metrics?.confirmed_negative_minor ?? 0),
      netMinor: metrics?.confirmed_net_minor ?? 0,
      incomeComposition: composition?.basis === basis ? composition.positive : undefined,
      expenseComposition: composition?.basis === basis ? composition.negative : undefined,
    }
  }
  if (basis === 'ACCOUNT_STATEMENT') {
    const metrics = statementMetrics(report)
    return {
      available: metrics !== null,
      incomeMinor: metrics?.cash_inflow_minor ?? 0,
      expenseMinor: metrics?.cash_outflow_minor ?? 0,
      netMinor: metrics?.net_cash_flow_minor ?? 0,
      incomeComposition: undefined,
      expenseComposition: undefined,
    }
  }
  const metrics = postedMetrics(report)
  return {
    available: metrics !== null,
    incomeMinor: metrics?.revenue_minor ?? 0,
    expenseMinor: metrics?.expense_minor ?? 0,
    netMinor: metrics?.profit_minor ?? 0,
    incomeComposition: composition?.basis === basis ? composition.revenue : undefined,
    expenseComposition: composition?.basis === basis ? composition.expense : undefined,
  }
}

function visibleCategorySlices(composition: CompanyReportCategoryComposition) {
  if (composition.items.length <= 8) return composition.items
  const visible = composition.items.slice(0, 7)
  const remainder = composition.items.slice(7)
  return [
    ...visible,
    {
      category_code: 'OTHER_AGGREGATED',
      category_label: '其他类型',
      amount_minor: remainder.reduce((total, item) => total + item.amount_minor, 0),
      fact_count: remainder.reduce((total, item) => total + item.fact_count, 0),
    },
  ]
}

function basisDescription(basis: CompanyReportLayer['basis']) {
  if (basis === 'ACCOUNT_STATEMENT') return '正式银行流水：按已确认的正式账单统计现金流，不等同于会计收入、费用或利润。'
  if (basis === 'CONFIRMED_CANDIDATE') return '已确认事项：按已审核的业务事项金额正负统计，尚未生成会计过账分录。'
  return '会计账簿：仅统计已过账分录。'
}

function CategoryShareChart({ title, composition, currencyCode, tone, unavailable, emptyMessage }: {
  title: string
  composition: CompanyReportCategoryComposition | undefined
  currencyCode: string
  tone: 'income' | 'expense'
  unavailable: boolean
  emptyMessage?: string
}) {
  const items = composition ? visibleCategorySlices(composition) : []
  return (
    <section className={`company-category-card ${tone}`} aria-label={title}>
      <header><h3>{title}</h3><span>{composition?.fact_count ?? 0} 条</span></header>
      {unavailable ? (
        <p className="company-category-empty">会计账簿尚未接入，未显示任何 0 值。</p>
      ) : items.length === 0 || !composition ? (
        <p className="company-category-empty">{emptyMessage ?? '当前期间没有可展示的类型金额。'}</p>
      ) : (
        <div className="company-category-list">
          {items.map((item) => (
            <CategoryShareRow
              key={`${item.category_code ?? 'unclassified'}:${item.category_label ?? ''}`}
              item={item}
              totalMinor={composition.total_minor}
              currencyCode={currencyCode}
            />
          ))}
        </div>
      )}
    </section>
  )
}

function CategoryShareRow({ item, totalMinor, currencyCode }: {
  item: CompanyReportCategorySlice
  totalMinor: number
  currencyCode: string
}) {
  const percentage = totalMinor > 0 ? item.amount_minor / totalMinor * 100 : 0
  const label = item.category_label ?? '未分类'
  return (
    <div className="company-category-row">
      <div className="company-category-label"><strong>{label}</strong><span>{percentage.toFixed(1)}%</span></div>
      <div className="company-category-track" role="img" aria-label={`${label} ${percentage.toFixed(1)}%`}>
        <span style={{ width: `${percentage}%` }} />
      </div>
      <div className="company-category-value"><strong>{reportMoney(item.amount_minor, currencyCode)}</strong><span>{item.fact_count} 条</span></div>
    </div>
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

      {statementData ? (
        <section className="company-test-summary" aria-label={`${companyName} 账户流水汇总`}>
          <header>
            <div><strong>正式银行流水</strong><span>来自已确认的正式银行账单；当前为现金流统计，尚未生成会计过账分录。</span></div>
            <Badge color="blue">正式数据</Badge>
          </header>
          <div className="company-test-totals">
            <ReportTotal label="账户流入" value={reportMoney(statementData.cash_inflow_minor, currencyCode)} />
            <ReportTotal label="账户流出" value={reportMoney(statementData.cash_outflow_minor, currencyCode)} />
            <ReportTotal label="净现金流" value={reportMoney(statementData.net_cash_flow_minor, currencyCode)} emphasis />
          </div>
          <div className="company-statement-summary">
            <span>已确认流水 <strong>{statementData.confirmed_transaction_count} 条</strong></span>
            <small>{statementData.statement_count} 份账单</small>
          </div>
        </section>
      ) : candidateData ? (
        <section className="company-test-summary" aria-label={`${companyName} 已确认事项汇总`}>
          <header>
            <div><strong>已确认业务事项</strong><span>来自已审核的正式数据，尚未生成会计过账分录。</span></div>
            <Badge color="amber">已确认</Badge>
          </header>
          <div className="company-test-totals">
            <ReportTotal label="已确认收入" value={reportMoney(candidateData.confirmed_positive_minor, currencyCode)} />
            <ReportTotal label="已确认支出" value={reportMoney(Math.abs(candidateData.confirmed_negative_minor), currencyCode)} />
            <ReportTotal label="已确认净额" value={reportMoney(candidateData.confirmed_net_minor, currencyCode)} emphasis />
          </div>
        </section>
      ) : null}

      {!postedHasEntries ? <p className="company-formal-empty">{postedAvailable ? '正式数据已接入，尚无会计过账分录' : '会计账簿尚未接入'}</p> : null}
      {postedHasEntries ? (
        <section className="company-report-totals" aria-label={`${companyName} 正式财务总额`}>
          <ReportTotal label="正式收入" value={postedMoney(postedData.revenue_minor)} />
          <ReportTotal label="正式费用" value={postedMoney(postedData.expense_minor)} />
          <ReportTotal label="正式利润" value={postedMoney(postedData.profit_minor)} emphasis />
        </section>
      ) : null}

      <section className="company-report-layers" aria-label={`${companyName} 三层事实`}>
        <div>
          <span>已确认来源</span>
          <strong>已确认来源 {candidateData?.confirmed_count ?? 0} 条</strong>
          <small>{candidateData?.source_count ?? 0} 个正式数据来源；尚未生成会计过账分录</small>
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
      {statementData ? (
        <div className="company-month-totals">
          <span>账户流入 <strong>{reportMoney(statementData.cash_inflow_minor, currencyCode)}</strong></span>
          <span>账户流出 <strong>{reportMoney(statementData.cash_outflow_minor, currencyCode)}</strong></span>
          <span>净现金流 <strong>{reportMoney(statementData.net_cash_flow_minor, currencyCode)}</strong></span>
        </div>
      ) : null}
      {postedAvailable && postedData && postedData.posted_entry_count > 0 ? (
        <div className="company-month-totals company-month-formal-totals">
          <span>正式收入 <strong>{postedMoney(postedData.revenue_minor)}</strong></span>
          <span>正式费用 <strong>{postedMoney(postedData.expense_minor)}</strong></span>
          <span>正式利润 <strong>{postedMoney(postedData.profit_minor)}</strong></span>
        </div>
      ) : null}
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
