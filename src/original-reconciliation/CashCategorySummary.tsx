import { Fragment, useState } from 'react'
import { CaretDown, CaretUp } from '@phosphor-icons/react'
import type { CashReconciliation } from '../types'
import { cashSourceLabel } from './groupCashRows'
import { cashFlowTotals, summarizeCashCategories } from './cashCategoryModel'

const currency = new Intl.NumberFormat('zh-CN', { style: 'currency', currency: 'CNY' })
const money = (minor: number) => currency.format(minor / 100)
const flows = [
  { kind: 'INCOME', label: '收入', detail: '按酒店和收入项目汇总' },
  { kind: 'EXPENSE', label: '支出', detail: '按酒店和支出项目汇总' },
  { kind: 'CURRENT', label: '往来款', detail: '流入、流出分别列示，不计入经营收支；分类不代表核销，父母往来需单独确认' },
] as const

export function CashCategorySummary({ data, month, loading }: {
  data: CashReconciliation | null; month: string; loading: boolean
}) {
  const [openKey, setOpenKey] = useState<string | null>(null)
  const groups = summarizeCashCategories(data?.rows ?? [])
  if (!data) return <div className="empty-state compact-empty statement-empty">
    <h3>{loading ? '正在读取本月流水' : '分类汇总暂不可用'}</h3>
    <p>{loading ? '读取完成后显示本月收支与往来款。' : '数据尚未读取成功，请稍后重试。'}</p>
  </div>

  return <div className="cash-category-summary">
    <p className="cash-summary-coverage" role="note">以下仅汇总正式接口已返回的已分类流水。银行、微信及平台材料的覆盖完整性仍需核验；分类金额不等于整月已结清。</p>
    {flows.map(flow => {
      const rows = groups.filter(row => row.flow === flow.kind)
      const total = cashFlowTotals(groups, flow.kind)
      const current = flow.kind === 'CURRENT'
      return <section className={`cash-summary-section ${flow.kind.toLowerCase()}`} aria-label={`${flow.label}分类汇总`} key={flow.kind}>
        <div className="cash-summary-heading">
          <div><h3>{flow.label}</h3><p>{flow.detail}</p></div>
          {current ? <div className="cash-summary-current-totals">
            <span>流入 <strong>{total.directionComplete ? money(total.inflowMinor) : '待核'}</strong></span>
            <span>流出 <strong>{total.directionComplete ? money(total.outflowMinor) : '待核'}</strong></span>
            <span>净流入 <strong>{total.directionComplete ? money(total.inflowMinor - total.outflowMinor) : '待核'}</strong></span>
          </div> : <strong className="cash-summary-amount">{money(total.amountMinor)}</strong>}
        </div>
        {rows.length ? <div className="cash-summary-table-wrap"><table>
          <caption className="sr-only">{flow.label}按酒店与分类汇总，明细可展开</caption>
          <thead><tr><th scope="col">酒店 / 主体</th><th scope="col">业务分类</th>
            {current ? <><th scope="col">流入</th><th scope="col">流出</th><th scope="col">净流入</th></> : <th scope="col">金额</th>}
            <th scope="col">来源明细</th></tr></thead>
          <tbody>{rows.map((row, index) => {
            const open = openKey === row.key
            const id = `cash-facts-${flow.kind}-${index}`
            return <Fragment key={row.key}><tr>
              <th scope="row">{row.hotel}</th><td>{row.category}</td>
              {current ? <><td className="money">{row.directionComplete ? money(row.inflowMinor) : '待核'}</td>
                <td className="money">{row.directionComplete ? money(row.outflowMinor) : '待核'}</td>
                <td className="money">{row.directionComplete ? money(row.inflowMinor - row.outflowMinor) : '待核'}</td></>
                : <td className="money">{money(row.amountMinor)}</td>}
              <td><button className="cash-summary-disclosure" aria-expanded={open} aria-controls={id}
                onClick={() => setOpenKey(open ? null : row.key)} type="button">
                {open ? <CaretUp size={14} /> : <CaretDown size={14} />}
                {open ? '收起明细' : `查看 ${row.transactionCount} 笔流水明细`}
              </button></td>
            </tr>{open ? <tr className="cash-summary-detail-row"><td colSpan={current ? 6 : 4}>
              <div id={id} role="region" aria-label={`${row.category}流水明细`} className="cash-summary-details">
                {!row.directionComplete && current ? <p role="status">方向明细不足，暂不计算净额；接口往来发生额 {money(row.amountMinor)}，不可作为净流入。</p> : null}
                {row.sources.map((source, sourceIndex) => <div className="cash-summary-source" key={`${source.rule_key}-${sourceIndex}`}>
                  <h4>{source.item_label} <small>{cashSourceLabel(source.source_kind)} · {source.transaction_count} 笔</small></h4>
                  {source.facts.map((fact, factIndex) => <div className="statement-fact-row" key={`${fact.fact_ref}-${factIndex}`}>
                    {source.source_kind === 'CANDIDATE' ? <span>{month}（月粒度）</span> : <time dateTime={fact.occurred_on}>{fact.occurred_on}</time>}
                    <strong>{money(fact.amount_minor)}</strong>
                    <code><span>{fact.fact_ref}</span><br />{source.source_ref}<br />{source.rule_key}</code>
                  </div>)}
                  {!source.facts.length ? <p>已登记调整 {money(source.amount_minor)} · {source.source_ref}（非新增流水）</p> : null}
                </div>)}
              </div>
            </td></tr> : null}</Fragment>
          })}</tbody>
        </table></div> : <p className="cash-summary-empty">本月没有{flow.label}事项</p>}
      </section>
    })}
  </div>
}
