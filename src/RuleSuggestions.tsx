import { useCallback, useEffect, useState } from 'react'
import { Badge, Button, Dialog } from '@radix-ui/themes'
import { ArrowsClockwise, CaretRight, ListChecks, Warning } from '@phosphor-icons/react'
import { api, ApiError, minorToMajor } from './api'
import type {
  CategoryNature,
  LocalRuleBatchMember,
  LocalRuleOutcome,
  LocalRuleSuggestionGroup,
  LocalRuleSuggestions,
} from './types'
import { ErrorState, LoadingState, PageHeader } from './shared/PagePrimitives'

/** Core accepts at most this many members per batch request. */
const RULE_BATCH_CHUNK_SIZE = 200

const RULE_BATCH_REASON = '按规则批量确认'

const natureLabels: Record<CategoryNature, { label: string; color: 'green' | 'red' | 'blue' }> = {
  INCOME: { label: '收入', color: 'green' },
  EXPENSE: { label: '支出', color: 'red' },
  TRANSFER: { label: '转账', color: 'blue' },
}

const outcomeLabels: Array<[LocalRuleOutcome, string]> = [
  ['CONFIRMED', '已确认'],
  ['STALE', '已变化'],
  ['NOT_MATCHED', '不再匹配'],
  ['NOT_PENDING', '非待审'],
  ['REJECTED', '失败'],
]

type Tally = Record<LocalRuleOutcome, number>

const emptyTally = (): Tally => ({ CONFIRMED: 0, STALE: 0, NOT_MATCHED: 0, NOT_PENDING: 0, REJECTED: 0 })

type BatchRun = {
  groupKey: string
  rulesVersion: string
  chunks: LocalRuleBatchMember[][]
  nextChunk: number
  processed: number
  total: number
  tally: Tally
  running: boolean
  error: string | null
  finished: boolean
}

const currency = new Intl.NumberFormat('zh-CN', { style: 'currency', currency: 'CNY', minimumFractionDigits: 2 })

function chunkMembers(members: LocalRuleBatchMember[], size = RULE_BATCH_CHUNK_SIZE): LocalRuleBatchMember[][] {
  const chunks: LocalRuleBatchMember[][] = []
  for (let index = 0; index < members.length; index += size) chunks.push(members.slice(index, index + size))
  return chunks
}

export function RuleSuggestionsPage({ csrfToken, onDecided }: {
  csrfToken: string
  /** Called after a batch finishes so the candidate lists are read again. */
  onDecided: () => void | Promise<unknown>
}) {
  const [data, setData] = useState<LocalRuleSuggestions | null>(null)
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [expanded, setExpanded] = useState<Set<string>>(new Set())
  const [confirming, setConfirming] = useState<LocalRuleSuggestionGroup | null>(null)
  const [run, setRun] = useState<BatchRun | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setLoadError(null)
    try {
      setData(await api.getLocalRuleSuggestions())
    } catch (error) {
      setData(null)
      setLoadError(error instanceof Error ? error.message : '无法读取规则建议')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    const timer = window.setTimeout(() => void load(), 0)
    return () => window.clearTimeout(timer)
  }, [load])

  const execute = useCallback(async (start: BatchRun) => {
    let current: BatchRun = { ...start, running: true, error: null }
    setRun(current)
    while (current.nextChunk < current.chunks.length) {
      const members = current.chunks[current.nextChunk]
      try {
        const receipt = await api.applyLocalRuleBatchChunk({
          rulesVersion: current.rulesVersion,
          groupKey: current.groupKey,
          chunkIndex: current.nextChunk,
          reason: RULE_BATCH_REASON,
          members,
          csrfToken,
        })
        const tally = { ...current.tally }
        for (const outcome of receipt.outcomes) tally[outcome.outcome] += 1
        current = {
          ...current,
          tally,
          nextChunk: current.nextChunk + 1,
          processed: current.processed + members.length,
        }
        setRun(current)
      } catch (error) {
        // The failed chunk stays next; retrying resends it unchanged, so an
        // unknown outcome is replayed under the same Idempotency-Key.
        const message = error instanceof ApiError || error instanceof Error ? error.message : '提交失败'
        current = { ...current, running: false, error: message }
        setRun(current)
        return
      }
    }
    current = { ...current, running: false, finished: true }
    setRun(current)
    await Promise.allSettled([load(), Promise.resolve(onDecided())])
  }, [csrfToken, load, onDecided])

  const startGroup = (group: LocalRuleSuggestionGroup) => {
    if (!data) return
    setConfirming(null)
    void execute({
      groupKey: group.group_key,
      rulesVersion: data.rules_version,
      chunks: chunkMembers(group.members),
      nextChunk: 0,
      processed: 0,
      total: group.members.length,
      tally: emptyTally(),
      running: true,
      error: null,
      finished: false,
    })
  }

  const toggle = (groupKey: string) => setExpanded((current) => {
    const next = new Set(current)
    if (next.has(groupKey)) next.delete(groupKey)
    else next.add(groupKey)
    return next
  })

  const busy = Boolean(run?.running)

  return (
    <>
      <PageHeader
        eyebrow="本机规则"
        title="规则建议"
        description="规则只提出分类建议；确认后每条候选仍各自写入可追溯的审核记录，不会过账。"
        action={<Button variant="soft" disabled={loading || busy} onClick={() => void load()}><ArrowsClockwise size={16} />刷新</Button>}
      />

      {run ? (
        <section className="panel rule-batch-status" aria-label="规则批量确认进度" role="status">
          <div>
            <strong>
              {run.running ? `正在确认 ${run.processed}/${run.total} 条` : run.finished ? `已处理 ${run.total} 条` : `已处理 ${run.processed}/${run.total} 条，未完成`}
            </strong>
            <div className="rule-batch-tally">
              {outcomeLabels.map(([outcome, label]) => <span key={outcome}>{label} {run.tally[outcome]}</span>)}
            </div>
            {run.error ? <p className="rule-batch-error"><Warning size={15} />{run.error}</p> : null}
          </div>
          {!run.running && !run.finished ? (
            <Button onClick={() => void execute(run)}>重试剩余批次</Button>
          ) : null}
        </section>
      ) : null}

      {loading && !data ? <LoadingState title="正在读取规则建议" description="正在按本机规则匹配待审候选。" /> : null}
      {loadError ? <ErrorState message={loadError} onRetry={() => void load()} /> : null}

      {data ? (
        <>
          <div className="personal-finance-boundary" role="status">
            <span>待审 {data.pending_total} 条</span>
            <span>规则命中 {data.groups.reduce((total, group) => total + group.count, 0)} 条</span>
            <span>未命中 {data.unmatched_count} 条</span>
          </div>
          {data.groups.length === 0 ? (
            <div className="empty-state compact-empty"><ListChecks size={30} /><h3>当前没有规则建议</h3><p>待审候选都没有命中本机规则。</p></div>
          ) : (
            <div className="rule-group-list">
              {data.groups.map((group) => {
                const nature = natureLabels[group.nature]
                const open = expanded.has(group.group_key)
                return (
                  <section className="panel rule-group" key={group.group_key} aria-label={`规则建议 ${group.category_label || group.category_code}`}>
                    <div className="rule-group-heading">
                      <button aria-expanded={open} className="rule-group-toggle" type="button" onClick={() => toggle(group.group_key)}>
                        <CaretRight size={15} className={open ? 'rule-caret open' : 'rule-caret'} />
                        <Badge color={nature.color}>{nature.label}</Badge>
                        <strong>{group.category_label || group.category_code}</strong>
                        <span className="rule-pattern">规则：{group.rule_pattern}{group.rule_note ? `（${group.rule_note}）` : ''}</span>
                        <Badge color="gray">{group.count} 条</Badge>
                      </button>
                      <div className="rule-group-actions">
                        {!group.category_ready ? <span className="rule-not-ready">分类未就绪：先运行 local_mode.py categories</span> : null}
                        <Button
                          disabled={!group.category_ready || busy || group.members.length === 0}
                          onClick={() => setConfirming(group)}
                        >
                          按此规则确认 {group.count} 条
                        </Button>
                      </div>
                    </div>
                    {open ? (
                      <div className="personal-review-list rule-samples">
                        {group.samples.map((sample) => (
                          <div className="rule-sample" key={sample.candidate_ref}>
                            <span><strong>{sample.short_id}</strong><small>当前分类：{sample.current_category_label || '待分类'}</small></span>
                            <span>{sample.summary}</span>
                            <strong>{currency.format(minorToMajor(sample.amount_minor))}</strong>
                          </div>
                        ))}
                      </div>
                    ) : null}
                  </section>
                )
              })}
            </div>
          )}
        </>
      ) : null}

      <Dialog.Root open={confirming !== null} onOpenChange={(open) => { if (!open) setConfirming(null) }}>
        <Dialog.Content maxWidth="480px">
          <Dialog.Title>按此规则确认</Dialog.Title>
          <Dialog.Description>
            {confirming
              ? `将把 ${confirming.count} 条待审候选归入“${confirming.category_label || confirming.category_code}”并确认。每条都会重新匹配并单独记录审核；已变化或不再匹配的会跳过。`
              : ''}
          </Dialog.Description>
          <div className="dialog-actions">
            <Dialog.Close><Button variant="soft" color="gray">取消</Button></Dialog.Close>
            <Button onClick={() => { if (confirming) startGroup(confirming) }}>确认 {confirming?.count ?? 0} 条</Button>
          </div>
        </Dialog.Content>
      </Dialog.Root>
    </>
  )
}
