import { useCallback, useEffect, useState } from 'react'
import type { ReactNode } from 'react'
import { Badge, Button } from '@radix-ui/themes'
import { ArrowsClockwise, Bank, CloudArrowDown, CloudArrowUp, Database, Warning } from '@phosphor-icons/react'
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

export function PersonalBankTransactionsPanel() {
  const [data, setData] = useState<PersonalBankTransactionsResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

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
      ) : data ? <PersonalBankFacts data={data} /> : null}
    </section>
  )
}

function PersonalBankFacts({ data }: { data: PersonalBankTransactionsResponse }) {
  const summary = data.summary
  const statementsByRef = new Map(data.statements.map((statement) => [statement.statement_ref, statement]))
  const periodStart = data.statements.reduce((value, statement) => value < statement.period_start ? value : statement.period_start, data.statements[0].period_start)
  const periodEnd = data.statements.reduce((value, statement) => value > statement.period_end ? value : statement.period_end, data.statements[0].period_end)
  return (
    <>
      <div className="personal-bank-facts-metrics" aria-label="正式银行流水汇总">
        <FormalMetric icon={<Bank size={19} />} label="正式流水" value={`${summary.transaction_count} 笔`} detail={`${summary.statement_count} 份账单 · ${periodStart} 至 ${periodEnd}`} />
        <FormalMetric icon={<CloudArrowDown size={19} />} label="银行流入" value={currency.format(minorToMajor(summary.cash_inflow_minor))} detail="账户现金流，不是营业收入" tone="income" />
        <FormalMetric icon={<CloudArrowUp size={19} />} label="银行流出" value={currency.format(minorToMajor(summary.cash_outflow_minor))} detail="账户现金流，不是会计费用" tone="expense" />
        <FormalMetric icon={<ArrowsClockwise size={19} />} label="净现金流" value={currency.format(minorToMajor(summary.net_cash_flow_minor))} detail="流入减流出" />
      </div>
      <StatementStatuses statements={data.statements} />
      <div className="personal-bank-transaction-list" aria-label="正式银行流水明细">
        {data.items.map((item) => {
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
    </>
  )
}

function StatementStatuses({ statements }: { statements: PersonalBankStatement[] }) {
  return (
    <div className="personal-bank-facts-review" role="status">
      {statements.map((statement) => {
        const statusLabel = {
          CONFIRMED: '已确认',
          PENDING: '待复核',
          REJECTED: '已拒绝',
        }[statement.review_status]
        const institution = institutionLabels[statement.institution_code] ?? '银行账户'
        return [
          <span key={`${statement.statement_ref}:account`}>{institution} · 尾号 {statement.account_suffix}</span>,
          <span key={`${statement.statement_ref}:status`}>账单审核：{statusLabel}</span>,
          <span key={`${statement.statement_ref}:revision`}>审核版本 {statement.review_revision}</span>,
        ]
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
