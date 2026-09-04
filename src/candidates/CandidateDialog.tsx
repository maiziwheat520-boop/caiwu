import { useState } from 'react'
import { Badge, Button, Dialog, TextArea, TextField } from '@radix-ui/themes'
import { ArrowsClockwise, CheckCircle, Info, Warning } from '@phosphor-icons/react'

import {
  eligibleClassificationBatchMembers,
  majorInputToMinor,
  minorToMajor,
  minorToMajorInput,
} from '../api'
import type {
  AccountingDimensions,
  Candidate,
  CandidateCorrections,
  ClassificationGroup,
  ClassificationTarget,
} from '../types'
import { currency } from '../shared/format'
import { auditChangeLabel, decisionColors, decisionLabels } from '../audit/reviewDecisions'
import { EvidencePreviewPanel } from '../evidence/EvidencePreviewPanel'
import {
  evidenceForBillConfirmation,
  evidenceLookupReference,
} from '../evidence/evidenceReferences'
import { SourceIcon } from './candidatePresentation'
import { accountingMonthLabel, type CandidateUpdateIntent } from './candidateLabels'

export function CandidateDialog({ candidate, classificationGroup, onClose, onUpdate, onApplyGroup, busy, detailLoading, detailReady, accountingDimensions, accountingDimensionsError }: {
  candidate: Candidate
  classificationGroup: ClassificationGroup | null
  onClose: () => void
  onUpdate: (candidate: Candidate, intent: CandidateUpdateIntent, corrections?: CandidateCorrections, conflictResolution?: string, reviewNote?: string) => void
  onApplyGroup: (
    candidate: Candidate,
    group: ClassificationGroup,
    target: ClassificationTarget,
    reviewNote?: string,
  ) => void
  busy: boolean
  detailLoading: boolean
  detailReady: boolean
  accountingDimensions: AccountingDimensions | null
  accountingDimensionsError: string | null
}) {
  const [businessUnitRef, setBusinessUnitRef] = useState(candidate.businessUnitRef)
  const [categoryCode, setCategoryCode] = useState(candidate.categoryCode)
  const [amount, setAmount] = useState(minorToMajorInput(candidate.amountMinor))
  const [accountingMonth, setAccountingMonth] = useState(candidate.accountingMonth ?? '')
  const [conflictResolution, setConflictResolution] = useState('')
  const [reviewNote, setReviewNote] = useState('')
  const [applyToGroup, setApplyToGroup] = useState(false)
  const [riskAcknowledged, setRiskAcknowledged] = useState(false)
  const readOnly = ['CONFIRMED', 'IGNORED', 'SUPERSEDED'].includes(candidate.status)

  const dialogTitle = readOnly
    ? candidate.status === 'CONFIRMED'
      ? '查看已确认候选'
      : candidate.status === 'IGNORED'
        ? '查看已忽略候选'
        : '查看已被取代候选'
    : candidate.conflict
      ? '处理金额或凭证冲突'
      : candidate.incomplete
        ? '补全候选信息'
        : '核对候选数据'
  const statusTitle = readOnly
    ? candidate.status === 'CONFIRMED'
      ? '候选已确认'
      : candidate.status === 'IGNORED'
        ? '候选已忽略'
        : '当前修订已被取代'
    : candidate.conflict
      ? '必须先说明采用哪份证据'
      : candidate.incomplete
        ? '必须补全归属月份'
        : '字段完整，可以确认'
  const statusDescription = readOnly
    ? candidate.status === 'CONFIRMED'
      ? '字段在此处只读；后续变化必须形成新的追加事件。'
      : candidate.status === 'IGNORED'
        ? '候选已从草稿数据中排除，原始证据和审核记录仍保留。'
        : '该修订仅用于历史追溯，不能覆盖后续修订。'
    : candidate.conflict
      ? '处理依据会随审核事件一起留存。'
      : candidate.incomplete
        ? '系统建议仅供参考，请人工核对。'
        : '确认后候选会进入月度对账草稿。'

  const parsedAmountMinor = majorInputToMinor(amount)
  const dimensionsContainSelection = Boolean(
    accountingDimensions?.business_units.some((unit) => unit.ref === businessUnitRef)
    && accountingDimensions.categories.some((item) => item.code === categoryCode),
  )
  const classificationUnchanged = businessUnitRef === candidate.businessUnitRef
    && categoryCode === candidate.categoryCode
  const canKeepExistingClassification = Boolean(
    accountingDimensionsError
    && classificationUnchanged
    && candidate.businessUnitRef
    && candidate.categoryCode,
  )
  const classificationSelectionAllowed = dimensionsContainSelection || canKeepExistingClassification
  const formComplete = businessUnitRef.length > 0
    && categoryCode.length > 0
    && parsedAmountMinor !== null
    && /^[0-9]{4}-(0[1-9]|1[0-2])$/.test(accountingMonth)
  const dimensionsStatusId = accountingDimensionsError
    ? 'accounting-dimensions-error'
    : !detailLoading && accountingDimensions && !dimensionsContainSelection
      ? 'accounting-dimensions-selection-error'
      : undefined
  const confirmBlocked = readOnly
    || busy
    || !detailReady
    || !classificationSelectionAllowed
    || !formComplete
    || (candidate.conflict && conflictResolution.trim().length === 0)
  const groupMember = classificationGroup?.members.find(
    (member) => member.candidate_ref === candidate.id,
  )
  const selectedBusinessUnitLabel = accountingDimensions?.business_units.find(
    (unit) => unit.ref === businessUnitRef,
  )?.label ?? (businessUnitRef === candidate.businessUnitRef ? candidate.businessUnit : undefined)
  const selectedCategoryLabel = accountingDimensions?.categories.find(
    (item) => item.code === categoryCode,
  )?.label ?? (categoryCode === candidate.categoryCode ? candidate.category : undefined)
  const groupMembers = classificationGroup
    ? eligibleClassificationBatchMembers(classificationGroup)
    : []
  const canApplyGroup = Boolean(
    classificationGroup
    && candidate.status === 'PENDING'
    && groupMember?.batch_eligible
    && groupMembers.length >= 2
    && groupMembers.length <= 100,
  )
  const groupUnavailableReason = groupMembers.length > 100
    ? `本组有 ${groupMembers.length} 笔可处理交易，超过单次 100 笔上限；请缩小范围或分批处理。`
    : candidate.status !== 'PENDING'
      ? '当前交易已不在待审核状态，整组处理不可用。'
      : groupMember && !groupMember.batch_eligible
        ? '当前交易因终态冲突、风险阻断或异常金额不开放整组处理。'
        : groupMembers.length < 2
          ? '本组当前没有足够的可处理成员，仍可单笔确认。'
          : null
  const groupRisks = classificationGroup?.conditions.risk_signature ?? []
  const groupFieldDrift = parsedAmountMinor !== candidate.amountMinor
    || accountingMonth !== candidate.accountingMonth
  const groupConfirmBlocked = confirmBlocked
    || Boolean(accountingDimensionsError)
    || !dimensionsContainSelection
    || !canApplyGroup
    || groupFieldDrift
    || (groupRisks.length > 0 && !riskAcknowledged)

  const submitCorrection = () => {
    if (confirmBlocked || parsedAmountMinor === null) return
    if (applyToGroup && classificationGroup) {
      if (groupConfirmBlocked) return
      onApplyGroup(candidate, classificationGroup, {
        business_unit_ref: businessUnitRef,
        category_code: categoryCode,
      }, reviewNote)
      return
    }
    const corrections: CandidateCorrections = {}
    if (businessUnitRef !== candidate.businessUnitRef) corrections.business_unit_ref = businessUnitRef
    if (categoryCode !== candidate.categoryCode) corrections.category_code = categoryCode
    if (parsedAmountMinor !== candidate.amountMinor) corrections.amount_minor = parsedAmountMinor
    if (accountingMonth !== candidate.accountingMonth) corrections.accounting_month = accountingMonth
    onUpdate(
      candidate,
      candidate.conflict ? 'RESOLVE_CONFLICT' : 'CONFIRM',
      Object.keys(corrections).length > 0 ? corrections : undefined,
      candidate.conflict ? conflictResolution.trim() : undefined,
      reviewNote,
    )
  }

  const focusClassification = () => {
    const field = document.getElementById('candidate-business-unit')
    field?.scrollIntoView?.({ block: 'center' })
    field?.focus()
  }

  return (
    <Dialog.Root open onOpenChange={(open) => { if (!open) onClose() }}>
      <Dialog.Content className="candidate-dialog" maxWidth="1120px">
        <div className="dialog-kicker"><SourceIcon source={candidate.source} /><span>{candidate.source} · {candidate.receivedAt} · {candidate.shortId}</span></div>
        <Dialog.Title>{dialogTitle}</Dialog.Title>
        <Dialog.Description>{readOnly ? '只读核对原始证据、当前字段与追加式审核历史。' : '左侧核对原始证据，右侧确认入账字段。原始消息不会被覆盖。'}</Dialog.Description>
        <div className={`dialog-status ${readOnly ? candidate.status === 'CONFIRMED' ? 'ready' : 'terminal' : candidate.conflict ? 'danger' : candidate.incomplete ? 'warning' : 'ready'}`}>
          {readOnly && candidate.status !== 'CONFIRMED' ? <Info size={19} weight="fill" /> : candidate.conflict ? <Warning size={19} weight="fill" /> : candidate.incomplete ? <Info size={19} weight="fill" /> : <CheckCircle size={19} weight="fill" />}
          <div>
            <strong>{statusTitle}</strong>
            <span>{statusDescription}</span>
          </div>
        </div>
        <div className="dialog-layout">
          <section className="dialog-evidence-pane" aria-labelledby="evidence-heading">
            <span className="section-label" id="evidence-heading">账单凭证</span>
            <div className="evidence-box">
              <blockquote>{candidate.summary}</blockquote>
              <div className="evidence-previews">
                {evidenceForBillConfirmation(candidate).map((item) => <EvidencePreviewPanel evidence={item} reference={evidenceLookupReference(candidate)} key={item.id} />)}
              </div>
            </div>
          </section>
          <section className="dialog-fields-pane" aria-labelledby="fields-heading">
            <span className="section-label" id="fields-heading">提取字段</span>
            <div className="field-grid">
              <label htmlFor="candidate-business-unit">
                <span>营业单元</span>
                {readOnly ? (
                  <TextField.Root id="candidate-business-unit" readOnly value={candidate.businessUnit} />
                ) : (
                  <select aria-describedby={dimensionsStatusId} id="candidate-business-unit" disabled={detailLoading || !accountingDimensions || Boolean(accountingDimensionsError)} value={businessUnitRef} onChange={(event) => setBusinessUnitRef(event.target.value)}>
                    {!accountingDimensions?.business_units.some((unit) => unit.ref === businessUnitRef) ? <option value={businessUnitRef}>{accountingDimensions ? `当前（目录外）：${candidate.businessUnit}` : `当前：${candidate.businessUnit}`}</option> : null}
                    {accountingDimensions?.business_units.map((unit) => <option key={unit.ref} value={unit.ref}>{unit.label}</option>)}
                  </select>
                )}
              </label>
              <label htmlFor="candidate-category">
                <span>科目</span>
                {readOnly ? (
                  <TextField.Root id="candidate-category" readOnly value={candidate.category} />
                ) : (
                  <select aria-describedby={dimensionsStatusId} id="candidate-category" disabled={detailLoading || !accountingDimensions || Boolean(accountingDimensionsError)} value={categoryCode} onChange={(event) => setCategoryCode(event.target.value)}>
                    {!accountingDimensions?.categories.some((item) => item.code === categoryCode) ? <option value={categoryCode}>{accountingDimensions ? `当前（目录外）：${candidate.category}` : `当前：${candidate.category}`}</option> : null}
                    {accountingDimensions?.categories.map((item) => <option key={item.code} value={item.code}>{item.label}</option>)}
                  </select>
                )}
              </label>
              <label htmlFor="candidate-amount"><span>金额</span><TextField.Root id="candidate-amount" inputMode="decimal" maxLength={18} readOnly={readOnly} value={amount} onChange={(event) => setAmount(event.target.value)} /></label>
              <label htmlFor="candidate-accounting-month">
                <span>归属月份</span>
                <TextField.Root id="candidate-accounting-month" type="month" pattern="[0-9]{4}-(0[1-9]|1[0-2])" readOnly={readOnly} value={accountingMonth} onChange={(event) => setAccountingMonth(event.target.value)} />
              </label>
            </div>
            {!readOnly && accountingDimensionsError ? <div className="blocking-note amber" id="accounting-dimensions-error" role="alert"><Info size={18} weight="fill" /><span><strong>{accountingDimensionsError}</strong></span></div> : null}
            {!readOnly && !detailLoading && accountingDimensions && !dimensionsContainSelection ? <div className="blocking-note amber" id="accounting-dimensions-selection-error" role="status"><Info size={18} weight="fill" /><span><strong>当前会计维度不在授权目录中</strong>请先治理基础资料或选择有效维度后再处理。</span></div> : null}
            {candidate.conflict && !readOnly ? <>
              <label className="conflict-resolution-field" htmlFor="candidate-conflict-resolution">
                <span>冲突处理依据</span>
                <TextArea id="candidate-conflict-resolution" placeholder="例如：以银行电子回单金额为准" value={conflictResolution} onChange={(event) => setConflictResolution(event.target.value)} resize="vertical" />
              </label>
              <div className="blocking-note"><Warning size={18} weight="fill" /><span><strong>需要记录处理依据</strong>说明采用哪份证据以及原因，提交后将以追加事件保留。</span></div>
            </> : null}
            {candidate.incomplete && !readOnly ? <div className="blocking-note amber"><Info size={18} weight="fill" /><span><strong>月份为系统建议</strong>请确认归属月份后再提交。</span></div> : null}
          </section>
        </div>

        {classificationGroup && groupMember ? (
          <section className="classification-scope" aria-labelledby="classification-scope-heading">
            <div className="classification-scope-heading">
              <div>
                <span className="section-label" id="classification-scope-heading">相似交易作用域</span>
                <strong>{classificationGroup.conditions.counterparty_label} · {classificationGroup.conditions.transaction_type}</strong>
              </div>
              <Badge color={classificationGroup.conditions.counterparty_basis === 'REGISTRY_COUNTERPARTY' ? 'green' : 'amber'}>
                {classificationGroup.conditions.counterparty_basis === 'REGISTRY_COUNTERPARTY' ? '登记身份匹配' : '严格平台摘要匹配'}
              </Badge>
            </div>
            <p>
              {classificationGroup.conditions.platform} · {classificationGroup.conditions.direction === 'INFLOW' ? '收入' : classificationGroup.conditions.direction === 'OUTFLOW' ? '支出' : '中性'} · {classificationGroup.conditions.transaction_type} · {classificationGroup.conditions.counterparty_label} · {classificationGroup.conditions.funding_instrument} · {accountingMonthLabel(classificationGroup.accounting_month)} · {groupMembers.length} 笔可处理
            </p>
            {!readOnly && canApplyGroup ? (
              <label className="classification-scope-toggle">
                <input
                  type="checkbox"
                  checked={applyToGroup}
                  onChange={(event) => {
                    setApplyToGroup(event.target.checked)
                    if (!event.target.checked) setRiskAcknowledged(false)
                  }}
                />
                <span>
                  <strong>同时处理本组其余 {groupMembers.length - 1} 笔</strong>
                  本次将原子确认共 {groupMembers.length} 笔，仅统一营业单元与科目；任一成员变化则全部不处理。
                </span>
              </label>
            ) : groupUnavailableReason ? (
              <div className="blocking-note amber" role="status">
                <Info size={18} weight="fill" />
                <span><strong>当前仅可单笔处理</strong>{groupUnavailableReason}</span>
              </div>
            ) : null}
            {applyToGroup ? (
              <div className="classification-scope-preview">
                <details>
                  <summary>匹配依据详情</summary>
                  <div className="classification-conditions">
                    <span>键版本：{classificationGroup.conditions.key_version}</span>
                    <span>实体：{classificationGroup.conditions.entity_ref}</span>
                    <span>来源：{classificationGroup.conditions.source_system} / {classificationGroup.conditions.source_kind}</span>
                    <span>平台：{classificationGroup.conditions.platform}</span>
                    <span>方向：{classificationGroup.conditions.direction}</span>
                    <span>交易类型：{classificationGroup.conditions.transaction_type}</span>
                    <span>对方键：{classificationGroup.conditions.counterparty_key}</span>
                    <span>对方名称：{classificationGroup.conditions.counterparty_label}</span>
                    <span>对方依据：{classificationGroup.conditions.counterparty_basis}</span>
                    <span>资金工具：{classificationGroup.conditions.funding_instrument}</span>
                    <span>交易状态：{classificationGroup.conditions.transaction_status}</span>
                    <span>币种：{classificationGroup.conditions.currency}</span>
                    <span>风险签名：{classificationGroup.conditions.risk_signature.join('、') || '无'}</span>
                    <span>月份：{accountingMonthLabel(classificationGroup.accounting_month)}</span>
                  </div>
                </details>
                <ul aria-label="本次批量处理成员">
                  {groupMembers.map((member) => (
                    <li key={member.candidate_ref}>
                      <span>{member.short_id}{member.candidate_ref === candidate.id ? '（当前）' : ''}</span>
                      <strong>{currency.format(minorToMajor(member.amount_minor))}</strong>
                    </li>
                  ))}
                </ul>
                {classificationGroup.members.length > groupMembers.length ? (
                  <small>另有 {classificationGroup.members.length - groupMembers.length} 笔因终态、风险、阻断或异常金额不在本次作用域。</small>
                ) : null}
                {classificationGroup.conditions.counterparty_basis === 'EXACT_PLATFORM_SUMMARY_V1' ? (
                  <div className="blocking-note amber">
                    <Info size={18} weight="fill" />
                    <span><strong>当前为降级契约依据</strong>仅对严格相同的七段平台摘要开放本次显式批量，不会据此学习自动分类规则。</span>
                  </div>
                ) : null}
                {groupRisks.length > 0 ? (
                  <label className="classification-risk-ack">
                    <input
                      type="checkbox"
                      checked={riskAcknowledged}
                      onChange={(event) => setRiskAcknowledged(event.target.checked)}
                    />
                    <span>
                      <strong>我已逐项核对并确认风险条件</strong>
                      {groupRisks.join('、')}；这仍是一次人工决定，不会形成一键审批或自动规则。
                    </span>
                  </label>
                ) : null}
                {groupFieldDrift ? (
                  <div className="blocking-note amber" role="status">
                    <Info size={18} weight="fill" />
                    <span><strong>金额或月份有单笔更改</strong>分组操作只传播营业单元与科目；请恢复原值或先单独处理当前交易。</span>
                  </div>
                ) : null}
              </div>
            ) : null}
          </section>
        ) : null}

        {!readOnly ? (
          <section className="review-note-section" aria-labelledby="review-note-heading">
            <label htmlFor="candidate-review-note">
              <span className="section-label" id="review-note-heading">费用种类或说明</span>
              <TextArea
                id="candidate-review-note"
                maxLength={500}
                placeholder="例如：余额宝收益、日常餐饮、差旅住宿；留空则记录系统审核动作"
                resize="vertical"
                value={reviewNote}
                onChange={(event) => setReviewNote(event.target.value)}
              />
            </label>
            <small>说明会写入追加式审核记录，不会覆盖原始账单；正式科目仍从受控目录选择。</small>
          </section>
        ) : null}

        <section className="candidate-history" aria-labelledby="candidate-history-heading">
          <div className="candidate-history-heading">
            <span className="section-label" id="candidate-history-heading">审核历史</span>
            <span>{candidate.reviewEvents.length} 条追加记录</span>
          </div>
          {detailLoading ? (
            <div className="candidate-history-loading" role="status"><ArrowsClockwise className="state-spinner" size={17} />正在读取审核历史</div>
          ) : candidate.reviewEvents.length > 0 ? (
            <ol>
              {candidate.reviewEvents.map((event) => (
                <li key={event.id}>
                  <span className="candidate-history-sequence">{event.sequence}</span>
                  <div>
                    <div className="candidate-history-meta">
                      <Badge color={decisionColors[event.decision]}>{decisionLabels[event.decision]}</Badge>
                      <time dateTime={event.created_at}>{new Intl.DateTimeFormat('zh-CN', { dateStyle: 'medium', timeStyle: 'short' }).format(new Date(event.created_at))}</time>
                    </div>
                    <strong>{event.reason}</strong>
                    <span>修订 {event.from_revision} → {event.to_revision} · {event.changes.map(auditChangeLabel).join('、') || '无字段变化'}</span>
                    {event.conflict_resolution ? <small>冲突依据：{event.conflict_resolution}</small> : null}
                  </div>
                </li>
              ))}
            </ol>
          ) : <p className="candidate-history-empty">此候选尚无审核事件。</p>}
        </section>
        <div className="dialog-actions">
          {!readOnly ? (
            <div className={`dialog-submit-context ${classificationSelectionAllowed ? '' : 'invalid'}`}>
              <span>本次分类</span>
              <strong>{selectedBusinessUnitLabel && selectedCategoryLabel ? `${selectedBusinessUnitLabel} · ${selectedCategoryLabel}` : '尚未选择营业单元和科目'}</strong>
              {accountingDimensions && !accountingDimensionsError
                ? <button type="button" onClick={focusClassification}>选择或修改分类</button>
                : <span className="dialog-submit-context-hint">目录恢复后可修改</span>}
            </div>
          ) : null}
          <Button variant="soft" color="gray" onClick={onClose}>{readOnly ? '关闭' : '取消'}</Button>
          {!readOnly ? <>
            <Button disabled={busy || !detailReady} variant="outline" color="gray" onClick={() => onUpdate(candidate, 'IGNORE', undefined, undefined, reviewNote)}>忽略候选</Button>
            <Button aria-describedby={dimensionsStatusId} disabled={applyToGroup ? groupConfirmBlocked : confirmBlocked} onClick={submitCorrection}>
              {applyToGroup ? `确认本组 ${groupMembers.length} 笔` : candidate.conflict ? '解决冲突并确认' : '保存更正并确认'}
            </Button>
          </> : null}
        </div>
      </Dialog.Content>
    </Dialog.Root>
  )
}
