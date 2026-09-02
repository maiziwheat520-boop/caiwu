export const ORIGINAL_RECONCILIATION_SOURCE_SYSTEM = 'original_reconciliation_xlsx'

export type LegacyItemSourceRule = {
  businessUnit: string
  businessSource: string
  flowKind: 'income' | 'expense'
  statementAccount: string
}

export const legacyItemSourceRules: LegacyItemSourceRule[] = [
  { businessUnit: '薇旭', businessSource: '携程', flowKind: 'income', statementAccount: '个人中国银行 · 赫程旅行社入账' },
  { businessUnit: '薇旭', businessSource: '美团', flowKind: 'income', statementAccount: '个人中国银行 · 北京钱袋宝入账' },
  { businessUnit: '景怡', businessSource: '美团、银行收款', flowKind: 'income', statementAccount: '个人建设银行' },
  { businessUnit: '薇旭', businessSource: '飞猪', flowKind: 'income', statementAccount: '薇旭网商银行 · 支付宝提现/飞猪房款结算' },
  { businessUnit: '逸豪', businessSource: '飞猪', flowKind: 'income', statementAccount: '逸豪网商银行企业账户' },
  { businessUnit: '薇旭', businessSource: '银行收款', flowKind: 'income', statementAccount: '个人农业银行' },
  { businessUnit: '薇旭', businessSource: '文杰房租', flowKind: 'income', statementAccount: '薇旭网商银行企业账户' },
  { businessUnit: '其他旧表对应主体', businessSource: '瓶装水', flowKind: 'expense', statementAccount: '各主体对应网商银行企业账户' },
  { businessUnit: '景怡', businessSource: '瓶装水', flowKind: 'expense', statementAccount: '景怡农业银行' },
  { businessUnit: '一品', businessSource: '瓶装水', flowKind: 'expense', statementAccount: '微信 · 收款方“东力2仓”' },
  { businessUnit: '逸豪', businessSource: '布草', flowKind: 'expense', statementAccount: '逸豪网商银行企业账户' },
  { businessUnit: '雅朵', businessSource: '税费', flowKind: 'expense', statementAccount: '雅朵农业银行' },
  { businessUnit: '景怡', businessSource: '税费', flowKind: 'expense', statementAccount: '景怡农业银行' },
  { businessUnit: '各旧表对应主体', businessSource: '代发工资', flowKind: 'expense', statementAccount: '相邻工资表已核对最终数据（权威源）' },
]

export const currentAccountCounterpartyNote = '陈展武（老爸）、林素美（老妈）'

export const historicalClassificationCorrection =
  'Core 口径：26.6、26.7 消杀均记景怡公账支出，修正后期末分别为 -161,330.34、-319,401.10；文杰房租记收入；分红及老爸、老妈明确转账记往来款且不计经营损益。网页不按银行正负号改写。'
