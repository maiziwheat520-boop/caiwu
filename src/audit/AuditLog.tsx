import { useState } from 'react'
import { Badge, Button, Select, TextField } from '@radix-ui/themes'
import {
  ArrowsClockwise,
  CaretRight,
  ClockCounterClockwise,
  FileText,
  MagnifyingGlass,
  Warning,
} from '@phosphor-icons/react'

import type { Candidate, Page, ReviewEvent } from '../types'
import { LoadingState, PageHeader } from '../shared/PagePrimitives'
import { minorToMajor } from '../api'
import { currency } from '../shared/format'
import { auditFieldLabels, decisionColors, decisionLabels } from './reviewDecisions'

const auditStatusLabels: Record<string, string> = {
  INCOMPLETE: '信息不完整',
  PENDING: '待审核',
  CONFLICTED: '存在冲突',
  CONFIRMED: '已确认',
  IGNORED: '已忽略',
  SUPERSEDED: '已被更正',
}

function formatAuditValue(field: ReviewEvent['changes'][number]['field'], value: string | number | null) {
  if (value === null) return '未填写'
  if (field === 'amount_minor' && typeof value === 'number') return currency.format(minorToMajor(value))
  if (field === 'status' && typeof value === 'string') return auditStatusLabels[value] ?? value
  return String(value)
}

export function AuditLog({ events, candidates, nextCursor, loading, error, onLoadMore, onRetry, onOpenCandidate, onNavigate }: {
  events: ReviewEvent[]
  candidates: Candidate[]
  nextCursor: string | null
  loading: boolean
  error: string | null
  onLoadMore: (cursor: string) => void
  onRetry: () => void
  onOpenCandidate: (candidate: Candidate) => void
  onNavigate: (page: Page) => void
}) {
  const [query, setQuery] = useState('')
  const [decision, setDecision] = useState<'ALL' | ReviewEvent['decision']>('ALL')
  const candidateById = new Map(candidates.map((candidate) => [candidate.id, candidate]))
  const normalizedQuery = query.trim().toLocaleLowerCase('zh-CN')
  const filtered = events.filter((event) => {
    if (decision !== 'ALL' && event.decision !== decision) return false
    if (!normalizedQuery) return true
    const candidate = candidateById.get(event.candidate_id)
    return [
      candidate?.shortId,
      candidate?.businessUnit,
      candidate?.category,
      event.actor,
      event.reason,
      event.conflict_resolution,
      decisionLabels[event.decision],
    ].some((value) => value?.toLocaleLowerCase('zh-CN').includes(normalizedQuery))
  })

  return (
    <>
      <PageHeader
        eyebrow="只读 · 追加式记录"
        title="审核操作记录"
        description="这里展示候选的确认、更正、冲突处置与忽略记录。合成预览不会读取真实财务审计数据。"
        action={<Button variant="outline" color="gray" onClick={() => onNavigate('overview')}>返回概览</Button>}
      />

      <section className="panel audit-log-panel">
        {loading && events.length === 0 ? (
          <LoadingState title="正在读取审核记录" description="正在加载追加式操作历史。" />
        ) : error && events.length === 0 ? (
          <div className="audit-load-state" role="alert">
            <Warning size={28} weight="fill" />
            <h2>审核记录读取失败</h2>
            <p>{error}</p>
            <Button onClick={onRetry}><ArrowsClockwise size={17} />重试</Button>
          </div>
        ) : <>
          <div className="audit-toolbar">
            <div>
              <strong>{nextCursor ? `已加载 ${filtered.length} 条` : `${filtered.length} 条记录`}</strong>
              <span>按最新操作排序</span>
            </div>
            <Select.Root value={decision} onValueChange={(value) => setDecision(value as 'ALL' | ReviewEvent['decision'])}>
              <Select.Trigger aria-label="筛选操作类型" />
              <Select.Content>
                <Select.Item value="ALL">全部操作</Select.Item>
                <Select.Item value="CONFIRM">确认候选</Select.Item>
                <Select.Item value="CORRECT_AND_CONFIRM">更正并确认</Select.Item>
                <Select.Item value="RESOLVE_CONFLICT">解决冲突</Select.Item>
                <Select.Item value="IGNORE">忽略候选</Select.Item>
              </Select.Content>
            </Select.Root>
            <TextField.Root
              aria-label="搜索操作记录"
              className="audit-search"
              placeholder="搜索候选、门店、科目或原因"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
            >
              <TextField.Slot><MagnifyingGlass size={15} /></TextField.Slot>
            </TextField.Root>
          </div>

          {error ? <div className="audit-inline-error" role="alert"><Warning size={16} />{error}<Button size="1" variant="soft" onClick={onRetry}>重试</Button></div> : null}

          {filtered.length > 0 ? (
            <div className="audit-timeline">
              {filtered.map((event) => {
                const candidate = candidateById.get(event.candidate_id)
                return (
                  <article className="audit-event" key={event.id}>
                    <div className="audit-marker"><ClockCounterClockwise size={17} weight="bold" /></div>
                    <div className="audit-event-card">
                      <div className="audit-event-heading">
                        <div>
                          <Badge color={decisionColors[event.decision]}>{decisionLabels[event.decision]}</Badge>
                          <strong>{candidate?.shortId ?? '未知候选'} · {candidate?.businessUnit ?? '未分配营业单元'}</strong>
                        </div>
                        <time dateTime={event.created_at}>{new Intl.DateTimeFormat('zh-CN', { dateStyle: 'medium', timeStyle: 'short' }).format(new Date(event.created_at))}</time>
                      </div>
                      <p className="audit-reason">{event.reason}</p>
                      <div className="audit-meta">
                        <span>{candidate?.category ?? '未知科目'}</span>
                        <span>修订 {event.from_revision} → {event.to_revision}</span>
                        <span>操作者：{event.actor}</span>
                        {candidate ? <Button size="1" variant="ghost" color="gray" onClick={() => onOpenCandidate(candidate)}><FileText size={14} />查看候选与证据</Button> : null}
                      </div>
                      {event.changes.length > 0 ? (
                        <ul className="audit-changes">
                          {event.changes.map((change, index) => (
                            <li key={`${event.id}:${change.field}:${index}`}>
                              <strong>{auditFieldLabels[change.field]}</strong>
                              <span>{formatAuditValue(change.field, change.previous_value)}</span>
                              <CaretRight size={13} />
                              <span>{formatAuditValue(change.field, change.new_value)}</span>
                            </li>
                          ))}
                        </ul>
                      ) : null}
                      {event.conflict_resolution ? <p className="audit-resolution"><strong>冲突处理依据</strong>{event.conflict_resolution}</p> : null}
                    </div>
                  </article>
                )
              })}
            </div>
          ) : (
            <div className="empty-state audit-empty">
              <ClockCounterClockwise size={30} />
              <h2>没有匹配的操作记录</h2>
              <p>{events.length > 0 ? '请调整筛选条件或搜索词。' : '完成一次候选审核后，记录会显示在这里。'}</p>
            </div>
          )}

          {nextCursor ? (
            <div className="audit-load-more">
              <Button variant="outline" color="gray" disabled={loading} onClick={() => onLoadMore(nextCursor)}>
                <ArrowsClockwise className={loading ? 'state-spinner' : undefined} size={16} />
                {loading ? '正在加载' : '加载更多记录'}
              </Button>
            </div>
          ) : null}
        </>}
      </section>
    </>
  )
}
