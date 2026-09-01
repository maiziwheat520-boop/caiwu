import type { PersonalBankStatement, PersonalBankTransaction } from '../types'

type PersonalBankTransactionPresentation = {
  counterparty: string
  detail: string
}

const layoutDashRun = /[-‐‑‒–—―]{2,}/gu
const placeholderSegment = /^[-‐‑‒–—―\s]+$/u

function cleanPdfLayoutText(value: string | null) {
  if (!value) return ''
  return value
    .replace(layoutDashRun, ' ')
    .split(/\s*\|\s*/u)
    .map((segment) => segment.replace(/\s+/gu, ' ').trim())
    .filter((segment) => segment && !placeholderSegment.test(segment))
    .join(' · ')
}

function cleanTransactionName(value: string, institutionCode: string) {
  const repaired = institutionCode === 'boc'
    ? value.replace(/(手机|网上|掌上)\s*\|\s*银行/gu, '$1银行')
    : value
  return cleanPdfLayoutText(repaired)
    .replace(/^(.+?)\s+(手机银行|网上银行|掌上银行)$/u, '$1 · $2')
}

function cleanCounterpartyName(item: PersonalBankTransaction, statement: PersonalBankStatement) {
  const name = cleanPdfLayoutText(item.counterparty_name)
  if (
    statement.institution_code === 'boc'
    && item.counterparty_account_masked
    && /\d{4}/u.test(item.counterparty_account_masked)
  ) {
    return name.replace(/\s+\d{1,2}$/u, '')
  }
  return name
}

function counterpartyAccountLabel(value: string | null) {
  if (!value) return ''
  const digits = value.replace(/\D/gu, '')
  return digits.length >= 4 ? `对方尾号 ${digits.slice(-4)}` : ''
}

export function presentPersonalBankTransaction(
  item: PersonalBankTransaction,
  statement: PersonalBankStatement,
): PersonalBankTransactionPresentation {
  const name = cleanCounterpartyName(item, statement)
  const counterpartyInstitution = cleanPdfLayoutText(item.counterparty_institution)
  const counterparty = name || counterpartyInstitution || '未提供对方名称'
  const values = [
    cleanTransactionName(item.transaction_name, statement.institution_code),
    counterpartyInstitution,
    counterpartyAccountLabel(item.counterparty_account_masked),
  ].filter((value, index, all) => value && all.indexOf(value) === index)
  return { counterparty, detail: values.join(' · ') }
}
