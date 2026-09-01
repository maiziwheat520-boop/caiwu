import { ArrowDown, CheckCircle, ClockCounterClockwise } from '@phosphor-icons/react'

type Props = {
  hasTestWorkspace: boolean
  liveDataReady: boolean
  canVerifyReceipts: boolean
}

type Feature = {
  id: string
  label: string
  detail: string
  target?: string
  available: (props: Props) => boolean
}

const features: ReadonlyArray<Feature> = [
  {
    id: 'fill-main',
    label: '填入主表',
    detail: '材料归类、工资表解析与测试批次验证已接回；新工资计算和保存仍在迁移。',
    target: '#payroll-test-actions-heading',
    available: (props: Props) => props.hasTestWorkspace,
  },
  {
    id: 'normal-draft',
    label: '生成代发表',
    detail: '恢复网商银行正常代发的不可付款草稿。',
    available: () => false,
  },
  {
    id: 'supplemental-draft',
    label: '生成补发代发表',
    detail: '恢复少发补发与多发结转，并保留原原因和审计记录。',
    available: () => false,
  },
  {
    id: 'summary',
    label: '汇总到工资总表',
    detail: '历史工资表可先生成网页汇总预览；正式月度汇总写入仍在迁移。',
    target: '#payroll-history-summary-heading',
    available: (props: Props) => props.hasTestWorkspace,
  },
  {
    id: 'previous-pending',
    label: '检查上月待办',
    detail: '恢复少发、多发、忽略与结转事项的逐人处理。',
    available: () => false,
  },
  {
    id: 'verify-paid',
    label: '核对本月已发',
    detail: '按公司真实回单逐人核对金额、账户与发放渠道。',
    target: '#payroll-verification-heading',
    available: (props: Props) => props.liveDataReady && props.canVerifyReceipts,
  },
  {
    id: 'manage-rules',
    label: '管理工资规则',
    detail: '恢复固定待遇、门店绩效、可休天数和月度调整的增删改。',
    available: () => false,
  },
  {
    id: 'check-history',
    label: '检查规则与历史',
    detail: '恢复当前表规则、历史月份和重复发放的只读检查。',
    available: () => false,
  },
]

export function PayrollFeatureHub(props: Props) {
  const availableCount = features.filter((feature) => feature.available(props)).length
  return (
    <section className="payroll-feature-hub" aria-labelledby="payroll-feature-hub-heading">
      <header>
        <div>
          <span>原软件功能对等清单</span>
          <h2 id="payroll-feature-hub-heading">八项工资功能独立恢复</h2>
          <p>入口不会再消失；只有已经接通真实读写链路的功能才允许操作。</p>
        </div>
        <strong>{availableCount} / 8 已接回可见工作区</strong>
      </header>
      <div className="payroll-feature-grid">
        {features.map((feature) => {
          const available = feature.available(props)
          const content = (
            <>
              <div>
                {available ? <CheckCircle size={19} weight="fill" /> : <ClockCounterClockwise size={19} />}
                <strong>{feature.label}</strong>
                <span>{available ? '已有可操作入口' : '功能恢复中'}</span>
              </div>
              <p>{feature.detail}</p>
              {available ? <small>进入当前入口 <ArrowDown size={13} /></small> : <small>未接后端，不提供假按钮</small>}
            </>
          )
          return available && feature.target
            ? <a href={feature.target} key={feature.id}>{content}</a>
            : <article key={feature.id}>{content}</article>
        })}
      </div>
    </section>
  )
}
