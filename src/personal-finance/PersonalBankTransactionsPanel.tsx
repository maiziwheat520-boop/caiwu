import { useCallback, useEffect, useMemo, useState } from 'react'
import type { ReactNode } from 'react'
import { Badge, Button } from '@radix-ui/themes'
import { ArrowsClockwise, Bank, CaretDown, CaretUp, CloudArrowDown, CloudArrowUp, Database, MagnifyingGlass, Warning } from '@phosphor-icons/react'
import { api, minorToMajor } from '../api'
import type { PersonalBankStatement, PersonalBankTransaction, PersonalBankTransactionsResponse } from '../types'
import { presentPersonalBankTransaction } from './personalBankPresentation'

const currency = new Intl.NumberFormat('zh-CN', {
  style: 'currency',
  currency: 'CNY',
  minimumFractionDigits: 2,
})

const occurredAt = new Intl.DateTimeFormat('zh-CN', {
  dateStyle: 'medium',
  timeStyle: 'short',
  timeZone: 'Asia/Shanghai',
})

const institutionLabels: Record<string, string> = {
  abc: '中国农业银行',
  boc: '中国银行',
  ccb: '中国建设银行',
  mybank: '网商银行',
}

const transactionPageSize = 50

async function loadAllPersonalBankTransactions() {
  const result = await api.getPersonalBankTransactions()
  const statementRefs = new Set(result.statements.map((statement) => statement.statement_ref))
  if (
    statementRefs.size !== result.statements.length
    || result.statements.length !== result.summary.statement_count
    || result.items.length !== result.summary.transaction_count
    || result.statements.reduce((total, statement) => total + statement.transaction_count, 0) !== result.items.length
    || result.items.some((item) => !statementRefs.has(item.statement_ref))
  ) {
    throw new Error('正式流水总数与明细不一致，未展示不完整结果')
  }
  return result
}

export function PersonalBankTransactionsPanel({ csrfToken }: { csrfToken: string }) {
  const [data, setData] = useState<PersonalBankTransactionsResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [reviewBusy, setReviewBusy] = useState<string | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      setData(await loadAllPersonalBankTransactions())
    } catch (loadError) {
      setData(null)
      setError(loadError instanceof Error ? loadError.message : '正式银行流水暂不可用')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    const timer = window.setTimeout(() => void load(), 0)
    return () => window.clearTimeout(timer)
  }, [load])

  return (
    <section className="panel personal-bank-facts" aria-label="个人正式银行流水">
      <header className="personal-bank-facts-header">
        <div>
          <span className="eyebrow">Core 正式账户事实</span>
          <h2>正式银行流水</h2>
          <p>来自受控导入的银行账单与唯一交易事实；与下方测试候选分开，也不代表会计凭证已经过账。</p>
        </div>
        <Badge color="blue"><Database size={13} />bank_statement</Badge>
      </header>

      {loading ? (
        <div className="personal-bank-facts-state" role="status">
          <ArrowsClockwise className="state-spinner" size={25} />
          <div><strong>正在读取正式银行流水</strong><span>完整性确认后一次显示本账单全部交易。</span></div>
        </div>
      ) : error ? (
        <div className="personal-bank-facts-state error" role="alert">
          <Warning size={25} />
          <div><strong>正式银行流水暂不可用</strong><span>下方测试候选仍可查看；未把不可用误显示为 0。{error}</span></div>
          <Button size="1" variant="soft" onClick={() => void load()}>重新加载</Button>
        </div>
      ) : data && data.summary.transaction_count === 0 ? (
        <div className="personal-bank-facts-state empty">
          <Bank size={28} />
          <div><strong>尚无正式银行流水</strong><span>受控导入完成后，银行交易会显示在这里。</span></div>
        </div>
      ) : data ? <PersonalBankFacts data={data} csrfToken={csrfToken} reviewBusy={reviewBusy} setReviewBusy={setReviewBusy} reload={load} /> : null}
    </section>
  )
}

function PersonalBankFacts({ data, csrfToken, reviewBusy, setReviewBusy, reload }: {
  data: PersonalBankTransactionsResponse
  csrfToken: string
  reviewBusy: string | null
  setReviewBusy: (value: string | null) => void
  reload: () => Promise<void>
}) {
  const [detailsOpen, setDetailsOpen] = useState(false)
  const [accountFilter, setAccountFilter] = useState('all')
  const [directionFilter, setDirectionFilter] = useState<'all' | 'income' | 'expense'>('all')
  const [dateFrom, setDateFrom] = useState('')
  const [dateTo, setDateTo] = useState('')
  const [query, setQuery] = useState('')
  const [visibleCount, setVisibleCount] = useState(transactionPageSize)
  const summary = data.summary
  const statementsByRef = useMemo(
    () => new Map(data.statements.map((statement) => [statement.statement_ref, statement])),
    [data.statements],
  )
  const periodStart = data.statements.reduce((value, statement) => value < statement.period_start ? value : statement.period_start, data.statements[0].period_start)
  const periodEnd = data.statements.reduce((value, statement) => value > statement.period_end ? value : statement.period_end, data.statements[0].period_end)
  const normalizedQuery = query.trim().toLocaleLowerCase('zh-CN')
  const filteredItems = useMemo(() => data.items.filter((item) => {
    const statement = statementsByRef.get(item.statement_ref)
    if (!statement) return false
    if (accountFilter !== 'all' && item.statement_ref !== accountFilter) return false
    if (directionFilter === 'income' && item.amount_minor <= 0) return false
    if (directionFilter === 'expense' && item.amount_minor >= 0) return false
    const occurredDate = item.occurred_at.slice(0, 10)
    if (dateFrom && occurredDate < dateFrom) return false
    if (dateTo && occurredDate > dateTo) return false
    if (!normalizedQuery) return true
    const presentation = presentPersonalBankTransaction(item, statement)
    return [
      institutionLabels[statement.institution_code] ?? '银行账户',
      statement.account_suffix,
      presentation.counterparty,
      presentation.detail,
    ].some((value) => value.toLocaleLowerCase('zh-CN').includes(normalizedQuery))
  }), [accountFilter, data.items, dateFrom, dateTo, directionFilter, normalizedQuery, statementsByRef])
  const visibleItems = filteredItems.slice(0, visibleCount)
  const hasFilters = accountFilter !== 'all' || directionFilter !== 'all' || dateFrom !== '' || dateTo !== '' || query !== ''

  const resetFilters = () => {
    setAccountFilter('all')
    setDirectionFilter('all')
    setDateFrom('')
    setDateTo('')
    setQuery('')
    setVisibleCount(transactionPageSize)
  }

  return (
    <>
      <div className="personal-bank-facts-metrics" aria-label="正式银行流水汇总">
        <FormalMetric icon={<Bank size={19} />} label="正式流水" value={`${summary.transaction_count} 笔`} detail={`${summary.statement_count} 份账单 · ${periodStart} 至 ${periodEnd}`} />
        <FormalMetric icon={<CloudArrowDown size={19} />} label="银行流入" value={currency.format(minorToMajor(summary.cash_inflow_minor))} detail="账户现金流，不是营业收入" tone="income" />
        <FormalMetric icon={<CloudArrowUp size={19} />} label="银行流出" value={currency.format(minorToMajor(summary.cash_outflow_minor))} detail="账户现金流，不是会计费用" tone="expense" />
        <FormalMetric icon={<ArrowsClockwise size={19} />} label="净现金流" value={currency.format(minorToMajor(summary.net_cash_flow_minor))} detail="流入减流出" />
      </div>
      <StatementStatuses statements={data.statements} csrfToken={csrfToken} reviewBusy={reviewBusy} setReviewBusy={setReviewBusy} reload={reload} />
      <div className="personal-bank-details-entry">
        <div>
          <strong>银行流水明细</strong>
          <span>按账户、日期、收支方向或交易对象查询，不在个人财务首屏默认展开。</span>
        </div>
        <Button
          aria-controls="personal-bank-transaction-details"
          aria-expanded={detailsOpen}
          variant="soft"
          onClick={() => setDetailsOpen((open) => !open)}
        >
          {detailsOpen ? <CaretUp size={15} /> : <CaretDown size={15} />}
          {detailsOpen ? '收起流水明细' : `查看流水明细（${summary.transaction_count} 笔）`}
        </Button>
      </div>
      {detailsOpen ? (
        <div className="personal-bank-details" id="personal-bank-transaction-details">
          <div className="personal-bank-filter-bar" role="search" aria-label="筛选正式银行流水">
            <label>
              <span>账户</span>
              <select aria-label="银行账户筛选" value={accountFilter} onChange={(event) => {
                setAccountFilter(event.target.value)
                setVisibleCount(transactionPageSize)
              }}>
                <option value="all">全部账户</option>
                {data.statements.map((statement) => (
                  <option key={statement.statement_ref} value={statement.statement_ref}>
                    {institutionLabels[statement.institution_code] ?? '银行账户'} · 尾号 {statement.account_suffix}
                  </option>
                ))}
              </select>
            </label>
            <label>
              <span>方向</span>
              <select aria-label="收支方向筛选" value={directionFilter} onChange={(event) => {
                setDirectionFilter(event.target.value as typeof directionFilter)
                setVisibleCount(transactionPageSize)
              }}>
                <option value="all">全部方向</option>
                <option value="income">仅流入</option>
                <option value="expense">仅流出</option>
              </select>
            </label>
            <label>
              <span>开始日期</span>
              <input aria-label="流水开始日期" max={dateTo || periodEnd} min={periodStart} type="date" value={dateFrom} onChange={(event) => {
                setDateFrom(event.target.value)
                setVisibleCount(transactionPageSize)
              }} />
            </label>
            <label>
              <span>结束日期</span>
              <input aria-label="流水结束日期" max={periodEnd} min={dateFrom || periodStart} type="date" value={dateTo} onChange={(event) => {
                setDateTo(event.target.value)
                setVisibleCount(transactionPageSize)
              }} />
            </label>
            <label className="personal-bank-query-field">
              <span>交易对象或关键词</span>
              <div><MagnifyingGlass size={15} /><input aria-label="搜索银行流水" placeholder="搜索对手方、银行或交易类型" value={query} onChange={(event) => {
                setQuery(event.target.value)
                setVisibleCount(transactionPageSize)
              }} /></div>
            </label>
            <Button disabled={!hasFilters} size="1" variant="outline" color="gray" onClick={resetFilters}>清除筛选</Button>
          </div>
          <div className="personal-bank-filter-summary" role="status">
            符合条件 {filteredItems.length} 笔{filteredItems.length > visibleItems.length ? `，当前显示前 ${visibleItems.length} 笔` : ''}
          </div>
          {visibleItems.length > 0 ? (
            <div className="personal-bank-transaction-list" aria-label="正式银行流水明细">
              {visibleItems.map((item) => {
                const statement = statementsByRef.get(item.statement_ref)
                return statement ? (
                  <PersonalBankTransactionRow
                    key={`${item.statement_ref}:${item.source_row_number}`}
                    item={item}
                    statement={statement}
                  />
                ) : null
              })}
            </div>
          ) : <div className="personal-bank-filter-empty">当前筛选条件下没有流水。</div>}
          {visibleItems.length < filteredItems.length ? (
            <div className="personal-bank-load-more">
              <Button variant="soft" onClick={() => setVisibleCount((count) => count + transactionPageSize)}>
                再显示 {Math.min(transactionPageSize, filteredItems.length - visibleItems.length)} 笔
              </Button>
            </div>
          ) : null}
        </div>
      ) : null}
    </>
  )
}

function StatementStatuses({ statements, csrfToken, reviewBusy, setReviewBusy, reload }: {
  statements: PersonalBankStatement[]
  csrfToken: string
  reviewBusy: string | null
  setReviewBusy: (value: string | null) => void
  reload: () => Promise<void>
}) {
  const pendingCount = statements.filter((statement) => statement.review_status === 'PENDING').length
  return (
    <div className="personal-bank-facts-review" aria-label={`银行账单待确认 ${pendingCount}`}>
      {statements.map((statement) => {
        const statusLabel = {
          CONFIRMED: '已确认',
          PENDING: '待复核',
          REJECTED: '已拒绝',
        }[statement.review_status]
        const institution = institutionLabels[statement.institution_code] ?? '银行账户'
        return <div className="personal-bank-statement-review" key={statement.statement_ref}>
          <span>{institution} · 尾号 {statement.account_suffix}</span>
          <span>账单审核：{statusLabel}</span>
          <span>审核版本 {statement.review_revision}</span>
          {statement.review_status === 'PENDING' ? <Button
            disabled={reviewBusy !== null}
            size="1"
            onClick={async () => {
              setReviewBusy(statement.statement_ref)
              try {
                await api.reviewPersonalBankStatement({
                  statement,
                  decision: 'CONFIRMED',
                  reason: 'Web 审核：确认银行账单',
                  csrfToken,
                })
                await reload()
              } finally {
                setReviewBusy(null)
              }
            }}
          >{reviewBusy === statement.statement_ref ? '正在确认…' : '确认账单'}</Button> : null}
        </div>
      })}
    </div>
  )
}

function FormalMetric({ icon, label, value, detail, tone = 'neutral' }: {
  icon: ReactNode
  label: string
  value: string
  detail: string
  tone?: 'neutral' | 'income' | 'expense'
}) {
  return (
    <article className={`personal-bank-fact-metric ${tone}`}>
      <span>{icon}</span>
      <div><small>{label}</small><strong>{value}</strong><p>{detail}</p></div>
    </article>
  )
}

function PersonalBankTransactionRow({ item, statement }: {
  item: PersonalBankTransaction
  statement: PersonalBankStatement
}) {
  const institution = institutionLabels[statement.institution_code] ?? '银行账户'
  const { counterparty, detail } = presentPersonalBankTransaction(item, statement)
  const direction = item.amount_minor < 0 ? 'expense' : 'income'
  return (
    <article className="personal-bank-transaction-row">
      <time dateTime={item.occurred_at}>{occurredAt.format(new Date(item.occurred_at))}</time>
      <div className="personal-bank-account"><strong>{institution}</strong><span>尾号 {statement.account_suffix}</span></div>
      <div className="personal-bank-counterparty"><strong>{counterparty}</strong><span>{detail}</span></div>
      <div className={`personal-bank-transaction-amount ${direction}`}>
        <strong>{item.amount_minor > 0 ? '+' : ''}{currency.format(minorToMajor(item.amount_minor))}</strong>
        <span>余额 {currency.format(minorToMajor(item.balance_minor))}</span>
      </div>
    </article>
  )
}
