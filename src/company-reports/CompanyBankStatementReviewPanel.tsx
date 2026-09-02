import { useCallback, useEffect, useState } from 'react'
import { Badge, Button } from '@radix-ui/themes'
import { Bank, CheckCircle, Warning } from '@phosphor-icons/react'
import { api } from '../api'
import type { CompanyBankStatement, CompanyBankStatementsResponse } from '../types'

function statusLabel(status: CompanyBankStatement['review_status']) {
  if (status === 'CONFIRMED') return { label: '已确认', color: 'green' as const }
  if (status === 'REJECTED') return { label: '已退回', color: 'red' as const }
  return { label: '待确认', color: 'amber' as const }
}

export function CompanyBankStatementReviewPanel({ csrfToken }: { csrfToken: string }) {
  const [data, setData] = useState<CompanyBankStatementsResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [busyRef, setBusyRef] = useState<string | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const result = await api.getCompanyBankStatements()
      if (result.statements.length !== 6 || new Set(result.statements.map((item) => item.statement_ref)).size !== 6) {
        throw new Error('公司账单清单不完整')
      }
      setData(result)
    } catch (loadError) {
      setData(null)
      setError(loadError instanceof Error ? loadError.message : '公司账单暂不可用')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    const timer = window.setTimeout(() => void load(), 0)
    return () => window.clearTimeout(timer)
  }, [load])

  const confirm = async (statement: CompanyBankStatement) => {
    setBusyRef(statement.statement_ref)
    setError(null)
    try {
      await api.reviewCompanyBankStatement({
        statement,
        decision: 'CONFIRMED',
        reason: 'Web 审核：确认公司银行账单',
        csrfToken,
      })
      await load()
    } catch (reviewError) {
      setError(reviewError instanceof Error ? reviewError.message : '公司账单确认失败')
    } finally {
      setBusyRef(null)
    }
  }

  const pending = data?.statements.filter((item) => item.review_status === 'PENDING').length ?? 0
  return (
    <section className="panel company-bank-review" aria-label="公司账单确认">
      <div className="panel-heading">
        <div><h2>公司账单确认</h2><p>6 份正式账单逐项确认；公司归属由服务端固定，不接受页面传入。</p></div>
        <Badge color={pending > 0 ? 'amber' : 'green'}>{pending > 0 ? `待确认 ${pending}` : '全部已确认'}</Badge>
      </div>
      {loading ? <p className="company-bank-review-state">正在读取公司账单…</p> : null}
      {error ? <div className="company-bank-review-error" role="alert"><Warning size={18} /><span>{error}</span><Button size="1" variant="soft" onClick={() => void load()}>重试公司流水</Button></div> : null}
      {data ? (
        <div className="company-bank-statement-list">
          {data.statements.map((statement) => {
            const status = statusLabel(statement.review_status)
            return (
              <article key={statement.statement_ref}>
                <Bank size={20} />
                <div><strong>{statement.company_name}</strong><span>{statement.period_start} 至 {statement.period_end} · 尾号 {statement.account_suffix}</span></div>
                <div><strong>{statement.transaction_count} 笔</strong><Badge color={status.color}>{status.label}</Badge></div>
                {statement.review_status === 'PENDING' ? (
                  <Button disabled={!csrfToken || busyRef !== null} onClick={() => void confirm(statement)}>
                    <CheckCircle size={16} />{busyRef === statement.statement_ref ? '正在确认' : '确认账单'}
                  </Button>
                ) : null}
              </article>
            )
          })}
        </div>
      ) : null}
    </section>
  )
}
