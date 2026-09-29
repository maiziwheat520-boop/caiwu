import { useEffect, useState } from 'react'
import { api, minorToMajor } from '../api'
import type { MonthlyReview } from '../types'

const currency = new Intl.NumberFormat('zh-CN', { style: 'currency', currency: 'CNY' })
const money = (value: number | null) => value === null ? '待核' : currency.format(minorToMajor(value))

export function MonthlyReviewPanel({ month }: { month: string }) {
  const [result, setResult] = useState<{ month: string; data?: MonthlyReview; error?: string } | null>(null)
  useEffect(() => {
    let active = true
    api.getMonthlyReview(month).then(data => {
      if (data.accounting_month !== month || data.authority !== 'NON_AUTHORITATIVE_REFERENCE') throw new Error('Review contract mismatch')
      if (active) setResult({ month, data })
    }).catch(() => {
      if (active) setResult({ month, error: '历史核对参考报告暂不可用；实时流水仍单独显示，不使用替代金额。' })
    })
    return () => { active = false }
  }, [month])
  const current = result?.month === month ? result : null
  const data = current?.data
  if (!data) return <section className="panel monthly-review-panel" aria-label="历史核对参考报告">
    <p role="status">{current?.error ?? '正在读取历史核对参考报告…'}</p>
  </section>
  const adjustment = data.adjustments.reduce((total, row) => total + row.amount_minor, 0)
  return <section className="panel monthly-review-panel" aria-label="历史核对参考报告">
    <div className="panel-heading"><div><h2>月度对账 · 历史核对参考报告</h2>
      <p>整理于 {data.confirmed_on} · 本附件不代表Core正式审核状态，不是账簿或可提交草稿</p></div>
      <span className="scope-chip">只读 · 不付款 / 不自动入账</span></div>
    <div className="monthly-review-policy">
      <strong>费用按实际支付月份；平台收入按对应截图结算期核对</strong>
      <p>每次提交对应平台截图；水费要求深圳水务收费凭证。下面的实时流水是到账/扣款口径，不直接冒充平台结算收入或已闭合结余。</p>
      <p>工资所属期截至7月用旧工资汇总，8月起用工作台已复核统计；实际发放月决定对账月份，补发不改变来源。工作台结果尚未接入此页，缺数不填零。</p>
    </div>
    {data.adjustments.length > 0 && <>
      <div className="monthly-review-total"><span>{month} 历史补记支出（待入账）</span><strong>{money(adjustment)}</strong></div>
      <p className="projection-note">只调整对账结余，不增加本月现金支出；未计入实时收支合计。</p>
      <div className="monthly-review-table-wrap"><table><thead><tr><th>项目</th><th>补记支出</th><th>结余影响</th></tr></thead>
        <tbody>{data.adjustments.map(row => <tr key={row.id}><td>{row.label}</td><td>{money(row.amount_minor)}</td><td>{money(row.balance_effect_minor)}</td></tr>)}</tbody></table></div>
      <details><summary>查看补记依据及去重说明</summary>{data.adjustments.map(row => <p key={row.id}><strong>{row.label}：</strong>{row.note}</p>)}</details>
    </>}
    {data.bridges.length > 0 && <div className="monthly-review-policy"><h3>支付口径切换衔接（待结转）</h3>{data.bridges.map(row => <p key={row.id}><strong>{row.label} {money(row.balance_effect_minor)}</strong> · {row.note}</p>)}<p>不作为收入或退款，现金影响为零；不能同时排除支出又重复加回结余。</p></div>}
    {data.pending.length > 0 && <div className="monthly-review-policy"><h3>已安排事项</h3>{data.pending.map(row => <p key={row.id}><strong>{row.label} {money(row.amount_minor)}</strong> · {row.note}</p>)}</div>}
    <details><summary>历史回测与已接受例外 · {data.backtest.length} 项本月记录</summary>
      <p>原表与历史核验来源的比较，不是本月实时现金；例外被接受不代表差额归零，也不放宽来源缺失或重复入账检查。</p>
      {data.backtest.length > 0 ? <div className="monthly-review-table-wrap"><table><thead><tr><th>项目</th><th>原表</th><th>匹配来源</th><th>差额</th><th>处理</th></tr></thead><tbody>{data.backtest.map(row => <tr key={row.label}><td>{row.label}</td><td>{money(row.original_minor)}</td><td>{money(row.matched_minor)}</td><td>{money(row.difference_minor)}</td><td>{row.status}<small>{row.note}</small></td></tr>)}</tbody></table></div> : <p>此月尚无整表历史回测记录，不代表所有项目已核平。</p>}
      {data.accepted_exceptions.map(row => <p key={row.id}><strong>{row.label}：</strong>{row.note}</p>)}
    </details>
    <details><summary>工资历史来源汇总 · 截至2026年7月</summary>
      <p>只读源表汇总；没有实际发放日期的金额不自动计入本月费用。父母工资单列、外部代发不计酒店工资，已排除的不重复减扣。</p>
      <div className="monthly-review-table-wrap"><table><thead><tr><th>工资所属月</th><th>旧汇总金额</th><th>门店明细</th></tr></thead><tbody>{data.historical_payroll.map(row => <tr key={row.period}><td>{row.period}</td><td>{money(row.total_minor)}</td><td><details><summary>查看门店</summary>{row.stores.map(store => <p key={store.label}>{store.label}：{store.amount_minor === null ? '源表空白（不是零）' : money(store.amount_minor)}</p>)}</details></td></tr>)}</tbody></table></div>
    </details>
    <p className="projection-note">报告版本：{data.revision}。父母后续往来单独确认，历史不再追查；分红仍手填。源交易、旧表和正式分录未改写。未来正式调整须回到Core受控审核重新核验，不能直接据此附件入账。</p>
  </section>
}
