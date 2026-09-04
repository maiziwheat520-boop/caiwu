import { useCallback, useEffect, useState } from 'react'
import { Badge } from '@radix-ui/themes'
import { Bank, CalendarBlank, CheckCircle, Database, Warning } from '@phosphor-icons/react'
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
  CompanyTransactionClassificationSummary,
  CompanyTransactionCategorySummary,
} from '../types'
import { ErrorState, LoadingState, PageHeader } from '../shared/PagePrimitives'
import { companyTabLabel } from './companyLabels'

const ALL_COMPANIES = '__all_companies__'

export function CompanyReportsPage() {
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
      const hasClassifiedTransactions = response.transaction_classifications?.items.some(
        (item) => item.confirmed_count > 0 || item.pending_count > 0,
      )
      setBasis(hasClassifiedTransactions || statementLayer?.items.some((item) => (
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
      eyebrow="经营驾驶舱"
      title="各公司报表"
      description="按公司查看经营现金流、收支构成和月度趋势。"
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

  const companies = [...companyIndex.entries()]
  const showAllCompanies = selectedCompanyRef === ALL_COMPANIES
  const activeCompanyRef = showAllCompanies
    ? ALL_COMPANIES
    : companyIndex.has(selectedCompanyRef)
    ? selectedCompanyRef
    : companies[0]?.[0] ?? ''
  const activeCompany = showAllCompanies ? null : companyIndex.get(activeCompanyRef)
  const activeCurrencyCode = activeCompany?.currencyCode ?? companies[0]?.[1].currencyCode ?? 'CNY'
  const activeComposition = showAllCompanies ? undefined : compositionFor(reports, basis, activeCompanyRef)
  const activeReport = showAllCompanies ? undefined : companyFor(layerFor(reports.layers, basis), activeCompanyRef)
  const dashboard = showAllCompanies
    ? allCompaniesDashboard(reports, basis, companies.map(([companyRef]) => companyRef))
    : dashboardSummary(
      basis,
      activeReport,
      activeComposition,
      classificationFor(reports, activeCompanyRef),
    )
  const rangeInvalid = !isReportMonth(fromMonth)
    || !isReportMonth(toMonth)
    || fromMonth > toMonth
    || monthDistance(fromMonth, toMonth) >= 24

  const toolbar = (
    <section className="company-report-toolbar" aria-label="报表筛选">
      <div className="company-report-tabs" role="tablist" aria-label="选择公司">
        <button type="button" role="tab" aria-selected={showAllCompanies} className={showAllCompanies ? 'active' : ''} onClick={() => setSelectedCompanyRef(ALL_COMPANIES)}>全部公司</button>
        {companies.map(([companyRef, identity]) => (
          <button key={companyRef} type="button" role="tab" title={identity.name} aria-selected={activeCompanyRef === companyRef} className={activeCompanyRef === companyRef ? 'active' : ''} onClick={() => setSelectedCompanyRef(companyRef)}>{companyTabLabel(identity.name)}</button>
        ))}
      </div>
      <div className="company-report-range">
        <CalendarBlank size={17} />
        <label><span>开始月份</span><input aria-label="开始月份" type="month" value={fromMonth} onChange={(event) => setFromMonth(event.target.value)} /></label>
        <span className="company-range-separator">至</span>
        <label><span>结束月份</span><input aria-label="结束月份" type="month" value={toMonth} onChange={(event) => setToMonth(event.target.value)} /></label>
        <button type="button" className="primary-button company-report-apply" disabled={rangeInvalid} onClick={() => setAppliedRange({ fromMonth, toMonth })}>应用期间</button>
        <button type="button" className="secondary-button" onClick={() => void loadReports()}>刷新</button>
      </div>
      {rangeInvalid ? <p role="alert">请选择不超过 24 个月的有效月份范围。</p> : null}
    </section>
  )

  if (companyIndex.size === 0) {
    return (
      <>
        {header}
        {toolbar}
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
      {genericCompanyOnly ? (
        <section className="company-attribution-warning" role="alert">
          <Warning size={20} />
          <div>
            <strong>待完成公司归属</strong>
            <span>当前 Core 只返回一个通用公司主体，已导入数据尚未分配到各家公司；下方汇总不代表公司报表已完整。</span>
          </div>
        </section>
      ) : null}
      <section className="company-financial-dashboard" aria-label={`${showAllCompanies ? '全部公司' : activeCompany?.name ?? '公司'} 财务汇总`}>
        <header className="company-dashboard-header">
          <div>
            <span className="eyebrow">{reports.from_month} 至 {reports.to_month}</span>
            <h2>{showAllCompanies ? '全部公司汇总' : activeCompany?.name}</h2>
            {showAllCompanies ? <span>{companies.length} 家公司合并展示</span> : null}
          </div>
          <div className="company-dashboard-actions">
            <span className="company-data-status"><CheckCircle size={15} weight="fill" />数据已更新</span>
          </div>
        </header>
        <div className="company-dashboard-totals">
          <ReportTotal label={basis === 'ACCOUNT_STATEMENT' ? '经营流入' : '总收入'} value={dashboard.available ? reportMoney(dashboard.incomeMinor, activeCurrencyCode) : '待接正式账簿'} />
          <ReportTotal label={basis === 'ACCOUNT_STATEMENT' ? '经营流出' : '总支出'} value={dashboard.available ? reportMoney(dashboard.expenseMinor, activeCurrencyCode) : '待接正式账簿'} />
          <ReportTotal label={basis === 'ACCOUNT_STATEMENT' ? '经营净现金流' : '净额'} value={dashboard.available ? reportMoney(dashboard.netMinor, activeCurrencyCode) : '待接正式账簿'} emphasis />
        </div>
        {basis === 'ACCOUNT_STATEMENT' && dashboard.confirmedCount !== undefined ? (
          <p className="company-classification-coverage">
            已分类 {dashboard.confirmedCount} 条，待人工确认 {dashboard.pendingCount ?? 0} 条；往来、融资和内部划转不计入经营流入或经营流出。
          </p>
        ) : null}
        <div className="company-dashboard-body">
          <MonthlyCashflowTable reports={reports} basis={basis} companyRefs={showAllCompanies ? companies.map(([companyRef]) => companyRef) : [activeCompanyRef]} currencyCode={activeCurrencyCode} />
          <aside className="company-dashboard-rail" aria-label="收支构成">
            <CategoryShareChart title="收入构成" composition={dashboard.incomeComposition} currencyCode={activeCurrencyCode} tone="income" unavailable={!dashboard.available} emptyMessage={basis === 'ACCOUNT_STATEMENT' ? dashboard.confirmedCount === undefined ? '账户流水尚未完成分类。' : '当前没有经营流入。' : undefined} />
            <CategoryShareChart title="支出构成" composition={dashboard.expenseComposition} currencyCode={activeCurrencyCode} tone="expense" unavailable={!dashboard.available} emptyMessage={basis === 'ACCOUNT_STATEMENT' ? dashboard.confirmedCount === undefined ? '账户流水尚未完成分类。' : '当前没有经营流出。' : undefined} />
            {basis === 'ACCOUNT_STATEMENT' ? <NonOperatingCashflow categories={dashboard.nonOperatingCategories} currencyCode={activeCurrencyCode} /> : null}
          </aside>
        </div>
      </section>
    </>
  )
}

function MonthlyCashflowTable({ reports, basis, companyRefs, currencyCode }: {
  reports: CompanyReportsResponse
  basis: CompanyReportLayer['basis']
  companyRefs: string[]
  currencyCode: string
}) {
  const layer = layerFor(reports.layers, basis)
  const monthKeys = new Set<string>()
  companyRefs.forEach((companyRef) => companyFor(layer, companyRef)?.months.forEach((month) => monthKeys.add(month.month)))
  const rows = [...monthKeys].sort((left, right) => right.localeCompare(left)).map((month) => {
    const summaries = companyRefs.map((companyRef) => dashboardSummary(basis, monthFor(companyFor(layer, companyRef), month), undefined))
    return {
      month,
      incomeMinor: summaries.reduce((total, item) => total + item.incomeMinor, 0),
      expenseMinor: summaries.reduce((total, item) => total + item.expenseMinor, 0),
      netMinor: summaries.reduce((total, item) => total + item.netMinor, 0),
    }
  })
  const maximum = Math.max(1, ...rows.flatMap((row) => [row.incomeMinor, row.expenseMinor]))
  const incomeLabel = basis === 'POSTED_LEDGER' ? '收入' : basis === 'ACCOUNT_STATEMENT' ? '账户流入' : '事项流入'
  const expenseLabel = basis === 'POSTED_LEDGER' ? '费用' : basis === 'ACCOUNT_STATEMENT' ? '账户流出' : '事项流出'
  return (
    <section className="company-monthly-overview" aria-label="月度现金流趋势">
      <header><div><h3>月度现金流趋势</h3><span>按当前公司与口径汇总</span></div><strong>{rows.length} 个月</strong></header>
      {rows.length === 0 ? <p className="company-monthly-empty">当前期间没有可展示的逐月事实。</p> : (
        <div className="company-monthly-table" role="table" aria-label="月度现金流明细">
          <div className="company-monthly-table-head" role="row"><span>月份</span><span>{incomeLabel}</span><span>{expenseLabel}</span><span>净额</span></div>
          {rows.map((row) => (
            <div className="company-monthly-table-row" role="row" key={row.month}>
              <strong>{reportMonthLabel(row.month)}</strong>
              <span className="company-flow-value income">{reportMoney(row.incomeMinor, currencyCode)}<i style={{ width: `${row.incomeMinor / maximum * 100}%` }} /></span>
              <span className="company-flow-value expense">{reportMoney(row.expenseMinor, currencyCode)}<i style={{ width: `${row.expenseMinor / maximum * 100}%` }} /></span>
              <strong className={row.netMinor < 0 ? 'negative' : 'positive'}>{reportMoney(row.netMinor, currencyCode)}</strong>
            </div>
          ))}
        </div>
      )}
    </section>
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
  report: CompanyReportAggregate | undefined,
  composition: CompanyReportCompositionItem | undefined,
  classification?: CompanyTransactionClassificationSummary,
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
      nonOperatingCategories: undefined,
      confirmedCount: undefined,
      pendingCount: undefined,
    }
  }
  if (basis === 'ACCOUNT_STATEMENT') {
    const metrics = statementMetrics(report)
    if (classification) return classificationDashboard(classification)
    return {
      available: metrics !== null,
      incomeMinor: metrics?.cash_inflow_minor ?? 0,
      expenseMinor: metrics?.cash_outflow_minor ?? 0,
      netMinor: metrics?.net_cash_flow_minor ?? 0,
      incomeComposition: undefined,
      expenseComposition: undefined,
      nonOperatingCategories: undefined,
      confirmedCount: undefined,
      pendingCount: undefined,
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
    nonOperatingCategories: undefined,
    confirmedCount: undefined,
    pendingCount: undefined,
  }
}

function allCompaniesDashboard(
  reports: CompanyReportsResponse,
  basis: CompanyReportLayer['basis'],
  companyRefs: string[],
) {
  const layer = layerFor(reports.layers, basis)
  const summaries = companyRefs.map((companyRef) => dashboardSummary(
    basis,
    companyFor(layer, companyRef),
    compositionFor(reports, basis, companyRef),
    classificationFor(reports, companyRef),
  ))
  const hasClassifications = summaries.some((summary) => summary.confirmedCount !== undefined)
  return {
    available: summaries.some((summary) => summary.available),
    incomeMinor: summaries.reduce((total, summary) => total + summary.incomeMinor, 0),
    expenseMinor: summaries.reduce((total, summary) => total + summary.expenseMinor, 0),
    netMinor: summaries.reduce((total, summary) => total + summary.netMinor, 0),
    incomeComposition: mergeCategoryCompositions(summaries.map((summary) => summary.incomeComposition)),
    expenseComposition: mergeCategoryCompositions(summaries.map((summary) => summary.expenseComposition)),
    nonOperatingCategories: mergeClassificationCategories(
      summaries.map((summary) => summary.nonOperatingCategories),
    ),
    confirmedCount: hasClassifications ? summaries.reduce(
      (total, summary) => total + (summary.confirmedCount ?? 0),
      0,
    ) : undefined,
    pendingCount: hasClassifications ? summaries.reduce(
      (total, summary) => total + (summary.pendingCount ?? 0),
      0,
    ) : undefined,
  }
}

function classificationFor(reports: CompanyReportsResponse, companyRef: string) {
  return reports.transaction_classifications?.items.find(
    (item) => item.entity_ref === companyRef,
  )
}

function classificationDashboard(summary: CompanyTransactionClassificationSummary) {
  const incomeCategories = summary.categories.filter(
    (item) => item.cashflow_role === 'OPERATING_INCOME'
      && item.inflow_minor > item.outflow_minor,
  )
  const expenseCategories = summary.categories.filter(
    (item) => item.cashflow_role === 'OPERATING_EXPENSE'
      && item.outflow_minor > item.inflow_minor,
  )
  const incomeMinor = incomeCategories.reduce(
    (total, item) => total + item.inflow_minor - item.outflow_minor,
    0,
  )
  const expenseMinor = expenseCategories.reduce(
    (total, item) => total + item.outflow_minor - item.inflow_minor,
    0,
  )
  return {
    available: true,
    incomeMinor,
    expenseMinor,
    netMinor: incomeMinor - expenseMinor,
    incomeComposition: classificationComposition(incomeCategories, 'OPERATING_INCOME'),
    expenseComposition: classificationComposition(expenseCategories, 'OPERATING_EXPENSE'),
    nonOperatingCategories: summary.categories.filter(
      (item) => item.cashflow_role === 'NON_OPERATING',
    ),
    confirmedCount: summary.confirmed_count,
    pendingCount: summary.pending_count,
  }
}

function classificationComposition(
  categories: CompanyTransactionCategorySummary[],
  role: 'OPERATING_INCOME' | 'OPERATING_EXPENSE',
): CompanyReportCategoryComposition {
  const amount = (item: CompanyTransactionCategorySummary) => role === 'OPERATING_INCOME'
    ? item.inflow_minor - item.outflow_minor
    : item.outflow_minor - item.inflow_minor
  return {
    total_minor: categories.reduce((total, item) => total + amount(item), 0),
    fact_count: categories.reduce((total, item) => total + item.transaction_count, 0),
    items: categories.map((item) => ({
      category_code: item.reporting_item_code ?? item.category_code,
      category_label: item.reporting_item_label ?? classificationLabel(item.category_code),
      amount_minor: amount(item),
      fact_count: item.transaction_count,
    })).sort((left, right) => right.amount_minor - left.amount_minor),
  }
}

function mergeClassificationCategories(
  groups: Array<CompanyTransactionCategorySummary[] | undefined>,
) {
  const merged = new Map<string, CompanyTransactionCategorySummary>()
  groups.flatMap((group) => group ?? []).forEach((item) => {
    const current = merged.get(item.category_code)
    merged.set(item.category_code, {
      ...item,
      transaction_count: (current?.transaction_count ?? 0) + item.transaction_count,
      inflow_minor: (current?.inflow_minor ?? 0) + item.inflow_minor,
      outflow_minor: (current?.outflow_minor ?? 0) + item.outflow_minor,
      net_minor: (current?.net_minor ?? 0) + item.net_minor,
      gross_minor: (current?.gross_minor ?? 0) + item.gross_minor,
      transaction_share_ppm: 0,
      gross_share_ppm: 0,
    })
  })
  return [...merged.values()].sort((left, right) => right.gross_minor - left.gross_minor)
}

function mergeCategoryCompositions(
  compositions: Array<CompanyReportCategoryComposition | undefined>,
): CompanyReportCategoryComposition | undefined {
  const available = compositions.filter((composition): composition is CompanyReportCategoryComposition => composition !== undefined)
  if (available.length === 0) return undefined
  const categories = new Map<string, CompanyReportCategorySlice>()
  available.forEach((composition) => composition.items.forEach((item) => {
    const key = `${item.category_code ?? ''}:${item.category_label ?? ''}`
    const current = categories.get(key)
    categories.set(key, {
      ...item,
      amount_minor: (current?.amount_minor ?? 0) + item.amount_minor,
      fact_count: (current?.fact_count ?? 0) + item.fact_count,
    })
  }))
  return {
    total_minor: available.reduce((total, composition) => total + composition.total_minor, 0),
    fact_count: available.reduce((total, composition) => total + composition.fact_count, 0),
    items: [...categories.values()].sort((left, right) => right.amount_minor - left.amount_minor),
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

function NonOperatingCashflow({ categories, currencyCode }: {
  categories: CompanyTransactionCategorySummary[] | undefined
  currencyCode: string
}) {
  return (
    <section className="company-non-operating" aria-label="往来及其他非经营现金流">
      <header><h3>往来及其他非经营现金流</h3><span>不计入经营收入或经营费用</span></header>
      {!categories || categories.length === 0 ? (
        <p>当前期间没有已分类的往来、融资或内部划转。</p>
      ) : (
        <div className="company-non-operating-list">
          {categories.map((item) => (
            <div key={item.category_code}>
              <strong>{classificationLabel(item.category_code)}</strong>
              <span>流入 {reportMoney(item.inflow_minor, currencyCode)}</span>
              <span>流出 {reportMoney(item.outflow_minor, currencyCode)}</span>
              <span>净额 {reportMoney(item.net_minor, currencyCode)}</span>
              <small>{item.transaction_count} 条</small>
            </div>
          ))}
        </div>
      )}
    </section>
  )
}

function classificationLabel(code: CompanyTransactionCategorySummary['category_code']) {
  return {
    PLATFORM_ROOM_REVENUE: '平台房费收入',
    RELATED_PARTY_CURRENT: '往来款',
    PAYROLL: '工资',
    FINANCING: '融资及还款',
    BOTTLED_WATER: '瓶装水',
    INTERNAL_TRANSFER: '公司内部划转',
    RENT: '房租',
    RENTAL_INCOME: '经营租赁收入',
    BANK_INTEREST: '银行利息',
    LINEN_LAUNDRY: '布草洗涤',
    OPERATING_FEE: '营运费',
  }[code]
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

// Retained as an internal diagnostic renderer; it is intentionally not mounted in the report UI.
// eslint-disable-next-line @typescript-eslint/no-unused-vars
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

      <section className="company-report-layers" aria-label={`${companyName} 数据处理阶段`}>
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
