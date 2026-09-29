import { useCallback, useEffect, useState } from 'react'
import { CheckCircle, Warning } from '@phosphor-icons/react'
import { api, minorToMajor } from '../api'
import type {
  CompanyTransactionCategory,
  CompanyTransactionClassification,
  CompanyTransactionClassificationsResponse,
  CompanyOperatingFeeReportingItem,
} from '../types'

const CATEGORY_OPTIONS: Array<{ value: CompanyTransactionCategory; label: string }> = [
  { value: 'PLATFORM_ROOM_REVENUE', label: '平台房费收入' },
  { value: 'RELATED_PARTY_CURRENT', label: '往来款' },
  { value: 'PAYROLL', label: '工资' },
  { value: 'FINANCING', label: '融资及还款' },
  { value: 'BOTTLED_WATER', label: '瓶装水' },
  { value: 'INTERNAL_TRANSFER', label: '公司内部划转' },
  { value: 'RENT', label: '房租' },
  { value: 'RENTAL_INCOME', label: '经营租赁收入' },
  { value: 'BANK_INTEREST', label: '银行利息' },
  { value: 'LINEN_LAUNDRY', label: '布草洗涤' },
  { value: 'OPERATING_FEE', label: '营运费' },
]

const OPERATING_FEE_OPTIONS: Array<{
  value: CompanyOperatingFeeReportingItem
  label: string
}> = [
  { value: 'BANK_FEES', label: '银行手续费' },
  { value: 'SOCIAL_SECURITY', label: '社保' },
  { value: 'TAX', label: '税费' },
  { value: 'INSURANCE', label: '保险费' },
  { value: 'DISINFECTION', label: '消杀费用' },
  { value: 'ELEVATOR', label: '电梯费用' },
  { value: 'FIRE_SAFETY', label: '消防费用' },
  { value: 'FRESH_FOOD', label: '生鲜' },
  { value: 'MOONCAKE', label: '月饼' },
  { value: 'HOTEL_TECH', label: '酒店智能设备' },
  { value: 'HOTEL_SUPPLIES', label: '酒店用品' },
  { value: 'OPERATING_FEE', label: '其他营运费' },
]

type Draft = {
  categoryCode: CompanyTransactionCategory | ''
  reportingItemCode: CompanyOperatingFeeReportingItem | ''
  reason: string
}

export function CompanyTransactionClassificationPanel({ csrfToken }: { csrfToken: string }) {
  const [page, setPage] = useState<CompanyTransactionClassificationsResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [savingRef, setSavingRef] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [drafts, setDrafts] = useState<Record<string, Draft>>({})

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      setPage(await api.getCompanyTransactionClassifications())
    } catch (loadError) {
      setPage(null)
      setError(loadError instanceof Error ? loadError.message : '待审批分类暂不可用')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    const timer = window.setTimeout(() => void load(), 0)
    return () => window.clearTimeout(timer)
  }, [load])

  const updateDraft = (transactionRef: string, patch: Partial<Draft>) => {
    setDrafts((current) => ({
      ...current,
      [transactionRef]: {
        categoryCode: current[transactionRef]?.categoryCode ?? '',
        reportingItemCode: current[transactionRef]?.reportingItemCode ?? '',
        reason: current[transactionRef]?.reason ?? '',
        ...patch,
      },
    }))
  }

  const approve = async (transaction: CompanyTransactionClassification) => {
    const draft = drafts[transaction.transaction_ref]
    if (!draft?.categoryCode || !draft.reason.trim()) {
      setError('请选择分类并填写本笔审批理由。')
      return
    }
    if (draft.categoryCode === 'OPERATING_FEE' && !draft.reportingItemCode) {
      setError('请选择营运费明细。')
      return
    }
    setSavingRef(transaction.transaction_ref)
    setError(null)
    setNotice(null)
    try {
      await api.reviewCompanyTransactionClassification({
        transaction,
        categoryCode: draft.categoryCode,
        reportingItemCode: draft.categoryCode === 'OPERATING_FEE'
          ? draft.reportingItemCode as CompanyOperatingFeeReportingItem
          : null,
        reason: draft.reason.trim(),
        csrfToken,
      })
      setPage((current) => current ? {
        ...current,
        items: current.items.filter(
          (item) => item.transaction_ref !== transaction.transaction_ref,
        ),
      } : current)
      setNotice('本笔流水已追加人工确认记录；未生成分录或过账。')
    } catch (saveError) {
      setError(saveError instanceof Error ? saveError.message : '审批失败，请重新加载后重试。')
    } finally {
      setSavingRef(null)
    }
  }

  const items = page?.items ?? []
  return (
    <section className="company-classification-review" aria-label="公司流水分类审批">
      <header>
        <div>
          <span className="eyebrow">人工审批</span>
          <h2>待确认公司流水分类</h2>
          <p>只处理规则无法安全判断的流水；每笔确认都会写入审计记录，不自动生成分录或过账。</p>
        </div>
        <strong>{loading ? '读取中' : `${items.length} 条待确认`}</strong>
      </header>
      {notice ? <p className="company-classification-notice"><CheckCircle size={18} />{notice}</p> : null}
      {error ? <p className="company-classification-error" role="alert"><Warning size={18} />{error}</p> : null}
      {!loading && page && items.length === 0 ? (
        <p className="company-classification-empty">当前没有待确认的公司流水。</p>
      ) : null}
      {items.length > 0 ? (
        <div className="company-classification-list">
          {items.map((transaction) => {
            const draft = drafts[transaction.transaction_ref] ?? {
              categoryCode: '',
              reportingItemCode: '',
              reason: '',
            }
            return (
              <article key={transaction.transaction_ref} className="company-classification-item">
                <div className="company-classification-fact">
                  <div><strong>{transaction.company_name}</strong><span>{formatDate(transaction.occurred_at)}</span></div>
                  <div><strong>{formatMoney(transaction.amount_minor)}</strong><span>{transaction.amount_minor >= 0 ? '流入' : '流出'}</span></div>
                  <p>{transaction.counterparty_name || '未提供交易对象'} · {transaction.transaction_name}</p>
                </div>
                <div className="company-classification-fields">
                  {transaction.amount_minor < 0 && /国家金库|国库/.test(transaction.counterparty_name ?? '') ? (
                    <fieldset style={{ gridColumn: '1 / -1', minWidth: 0 }}>
                      <legend>国库扣款用途待确认</legend>
                      <p>请根据本笔缴款凭证选择社保或税款；选择只填写分类，填写理由后再确认。</p>
                      {([
                        ['SOCIAL_SECURITY', '社保'],
                        ['TAX', '税款'],
                      ] as const).map(([value, label]) => (
                        <button
                          key={value}
                          type="button"
                          disabled={savingRef !== null}
                          aria-pressed={draft.categoryCode === 'OPERATING_FEE' && draft.reportingItemCode === value}
                          onClick={() => updateDraft(transaction.transaction_ref, {
                            categoryCode: 'OPERATING_FEE', reportingItemCode: value,
                          })}
                        >{label}</button>
                      ))}
                    </fieldset>
                  ) : null}
                  <label>
                    <span>确认分类</span>
                    <select
                      aria-label={`确认分类 ${transaction.transaction_ref}`}
                      value={draft.categoryCode}
                      onChange={(event) => updateDraft(transaction.transaction_ref, {
                        categoryCode: event.target.value as CompanyTransactionCategory | '',
                        reportingItemCode: '',
                      })}
                    >
                      <option value="">请选择</option>
                      {CATEGORY_OPTIONS.map((option) => (
                        <option key={option.value} value={option.value}>{option.label}</option>
                      ))}
                    </select>
                  </label>
                  {draft.categoryCode === 'OPERATING_FEE' ? (
                    <label>
                      <span>营运费明细</span>
                      <select
                        aria-label={`营运费明细 ${transaction.transaction_ref}`}
                        value={draft.reportingItemCode}
                        onChange={(event) => updateDraft(transaction.transaction_ref, {
                          reportingItemCode: event.target.value as CompanyOperatingFeeReportingItem | '',
                        })}
                      >
                        <option value="">请选择</option>
                        {OPERATING_FEE_OPTIONS.map((option) => (
                          <option key={option.value} value={option.value}>{option.label}</option>
                        ))}
                      </select>
                    </label>
                  ) : null}
                  <label>
                    <span>审批理由</span>
                    <input
                      aria-label={`审批理由 ${transaction.transaction_ref}`}
                      value={draft.reason}
                      maxLength={1000}
                      placeholder="填写判断依据"
                      onChange={(event) => updateDraft(transaction.transaction_ref, {
                        reason: event.target.value,
                      })}
                    />
                  </label>
                  <button
                    type="button"
                    className="primary-button"
                    disabled={
                      savingRef !== null
                      || !draft.categoryCode
                      || !draft.reason.trim()
                      || (draft.categoryCode === 'OPERATING_FEE' && !draft.reportingItemCode)
                    }
                    onClick={() => void approve(transaction)}
                  >
                    {savingRef === transaction.transaction_ref ? '正在确认' : '确认本笔分类'}
                  </button>
                </div>
              </article>
            )
          })}
        </div>
      ) : null}
    </section>
  )
}

function formatDate(value: string) {
  return new Intl.DateTimeFormat('zh-CN', { dateStyle: 'medium' }).format(new Date(value))
}

function formatMoney(amountMinor: number) {
  return new Intl.NumberFormat('zh-CN', {
    style: 'currency',
    currency: 'CNY',
    minimumFractionDigits: 2,
  }).format(minorToMajor(Math.abs(amountMinor)))
}
