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
  { businessUnit: '薇旭', businessSource: '银行收款', flowKind: 'income', statementAccount: '个人农业银行 · 乐刷结算；按原始到账核对' },
  { businessUnit: '薇旭', businessSource: '文杰房租', flowKind: 'income', statementAccount: '薇旭网商银行企业账户' },
  { businessUnit: '其他旧表对应主体', businessSource: '瓶装水', flowKind: 'expense', statementAccount: '各主体对应网商银行企业账户' },
  { businessUnit: '景怡', businessSource: '瓶装水', flowKind: 'expense', statementAccount: '景怡农业银行' },
  { businessUnit: '一品', businessSource: '瓶装水', flowKind: 'expense', statementAccount: '微信 · 收款方“东力2仓”' },
  { businessUnit: '逸豪', businessSource: '布草', flowKind: 'expense', statementAccount: '逸豪网商银行企业账户' },
  { businessUnit: '雅朵', businessSource: '税费', flowKind: 'expense', statementAccount: '雅朵农业银行' },
  { businessUnit: '景怡', businessSource: '税费', flowKind: 'expense', statementAccount: '景怡农业银行' },
  { businessUnit: '各旧表对应主体', businessSource: '代发工资', flowKind: 'expense', statementAccount: '工资所属期1—7月旧汇总、8月起工作台已复核统计；按实际发放月对账' },
]

export const currentAccountCounterpartyNote = '陈展武（老爸）、林素美（老妈）'

export const historicalClassificationCorrection =
  '已确认审核口径：雅朵农行科威达付款属于消杀；文杰房租记收入。税款、社保与银行手续费保留子分类，已记过的费用不重复补记。父母后续往来单独确认，分红手填；审核说明不改写原始交易或正式账簿。'
