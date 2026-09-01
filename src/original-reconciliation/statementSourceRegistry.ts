export type StatementSourceRule = {
  businessUnit: string
  businessSource: string
  flowKind: 'income'
  statementAccount: string
}

export const statementSourceRules: StatementSourceRule[] = [
  { businessUnit: '薇旭', businessSource: '携程、美团', flowKind: 'income', statementAccount: '个人中国银行' },
  { businessUnit: '景怡', businessSource: '美团、银行收款', flowKind: 'income', statementAccount: '个人建设银行' },
  { businessUnit: '薇旭', businessSource: '飞猪', flowKind: 'income', statementAccount: '薇旭网商银行企业账户' },
  { businessUnit: '逸豪', businessSource: '飞猪', flowKind: 'income', statementAccount: '逸豪网商银行企业账户' },
  { businessUnit: '薇旭', businessSource: '银行收款', flowKind: 'income', statementAccount: '个人农业银行' },
  { businessUnit: '薇旭', businessSource: '文杰房租', flowKind: 'income', statementAccount: '薇旭网商银行企业账户' },
]

export const currentAccountCounterpartyNote = '陈展武（老爸）、林素美（老妈）'

export const historicalClassificationCorrection =
  '原表口径校正：文杰房租记收入；消杀 4,300 元记景怡公账支出；分红及老爸、老妈明确转账记往来款且不计经营损益；爸妈实际工资仍记支出。'
