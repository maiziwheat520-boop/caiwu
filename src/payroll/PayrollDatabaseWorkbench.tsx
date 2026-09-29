import { useEffect, useState } from 'react'
import { api, ApiError, minorToMajor } from '../api'
import { previousBusinessMonth } from '../shared/monthPolicy'
import type { PayrollDatabaseWorkbench as Workbench } from '../types'

const currency = new Intl.NumberFormat('zh-CN', { style: 'currency', currency: 'CNY' })
const money = (minor: number) => currency.format(minorToMajor(minor))

export function PayrollDatabaseWorkbench() {
  const [period, setPeriod] = useState(previousBusinessMonth)
  const [workbench, setWorkbench] = useState<Workbench | null>(null)
  const [state, setState] = useState<'loading' | 'empty' | 'error' | 'ready'>('loading')

  useEffect(() => {
    let current = true
    void api.getPayrollDatabaseWorkbench(period).then((result) => {
      if (!current) return
      setWorkbench(result)
      setState('ready')
    }).catch((error: unknown) => {
      if (!current) return
      setWorkbench(null)
      setState(
        error instanceof ApiError && error.code === 'PAYROLL_WORKBENCH_NOT_FOUND'
          ? 'empty'
          : 'error',
      )
    })
    return () => { current = false }
  }, [period])

  return (
    <section className="panel payroll-live-section" aria-labelledby="database-payroll-heading">
      <div className="section-heading payroll-live-heading">
        <div>
          <span>数据库账本</span>
          <h2 id="database-payroll-heading">工资明细</h2>
        </div>
        <label>
          工资月份{' '}
          <input
            aria-label="数据库工资月份"
            type="month"
            value={period}
            onChange={(event) => {
              setState('loading')
              setPeriod(event.target.value)
            }}
          />
        </label>
      </div>
      {state === 'loading' ? <p role="status">正在读取数据库工资记录…</p> : null}
      {state === 'empty' ? <p role="status">该月尚未迁入数据库；旧工资文件未被覆盖。</p> : null}
      {state === 'error' ? <p role="alert">数据库工资记录暂不可用，请稍后重试。</p> : null}
      {state === 'ready' && workbench ? (
        <>
          <p role="status">
            {workbench.status === 'LOCKED' ? '已锁定' : '待复核'} · {workbench.line_count} 人 ·
            工资 {money(workbench.net_amount_minor)}，其中现金 {money(workbench.cash_amount_minor)}、
            银行 {money(workbench.bank_amount_minor)}（含补发 {money(workbench.supplemental_amount_minor)}）
          </p>
          {workbench.issues.some((issue) => !issue.resolved) ? (
            <p role="alert">仍有阻断问题，不能生成银行代发表。</p>
          ) : null}
          <div className="payroll-database-table-wrap">
            <table className="payroll-database-table">
              <thead><tr><th>员工</th><th>地点</th><th>收款人</th><th>账号</th><th>工资</th><th>现金</th><th>银行</th><th>补发</th></tr></thead>
              <tbody>
                {workbench.lines.map((line) => (
                  <tr key={line.line_ref}>
                    <td>{line.employee_name}</td>
                    <td>{line.location}</td>
                    <td>{line.payee_name ?? '现金'}</td>
                    <td>{line.account_masked ?? '—'}</td>
                    <td>{money(line.net_amount_minor)}</td>
                    <td>{money(line.cash_amount_minor)}</td>
                    <td>{money(line.bank_amount_minor)}</td>
                    <td>{money(line.supplemental_amount_minor)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      ) : null}
    </section>
  )
}
