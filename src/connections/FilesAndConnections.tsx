import { useState } from 'react'
import { Badge, Button, Dialog, TextField } from '@radix-ui/themes'
import {
  ArrowsClockwise,
  CheckCircle,
  CloudArrowUp,
  Database,
  FileText,
  FolderOpen,
  Info,
  ShieldCheck,
  Warning,
} from '@phosphor-icons/react'

import { api } from '../api'
import type {
  Candidate,
  ConnectionStatus,
  EvidenceReference,
  Notice,
} from '../types'
import { PageHeader } from '../shared/PagePrimitives'
import { accountingMonthLabel } from '../candidates/candidateLabels'
import { materialNameFor, materialRiskCodes } from '../personal-finance/personalFinanceRules'

const connectionStateLabel: Record<ConnectionStatus['state'], string> = {
  CONNECTED: '已连接',
  DISCONNECTED: '已断开',
  DEGRADED: '服务降级',
  NOT_CONFIGURED: '未配置',
}

export function ConnectionBadge({ connection }: { connection?: ConnectionStatus }) {
  const state = connection?.state ?? 'NOT_CONFIGURED'
  const color = state === 'CONNECTED' ? 'green' : state === 'DEGRADED' ? 'amber' : 'gray'
  return <Badge color={color}>{connectionStateLabel[state]}</Badge>
}

function EvidenceUnlockDialog({ evidence, csrfToken, onClose, onUnlocked }: {
  evidence: EvidenceReference
  csrfToken: string
  onClose: () => void
  onUnlocked: () => Promise<void>
}) {
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const submit = async (event: React.FormEvent) => {
    event.preventDefault()
    if (busy || password.length === 0 || !evidence.source_ref) return
    setBusy(true)
    setError(null)
    const submittedPassword = { value: password }
    let unlocked = false
    setPassword('')
    try {
      await api.unlockEvidence({
        sourceRef: evidence.source_ref,
        password: submittedPassword.value,
        csrfToken,
      })
      unlocked = true
    } catch (unlockError) {
      setError(unlockError instanceof Error ? unlockError.message : '账单解锁失败，请检查密码后重试')
    } finally {
      submittedPassword.value = ''
      setPassword('')
      setBusy(false)
    }
    if (!unlocked) return
    onClose()
    await onUnlocked()
  }

  return (
    <Dialog.Root open onOpenChange={(open) => {
      if (!open && !busy) {
        setPassword('')
        setError(null)
        onClose()
      }
    }}>
      <Dialog.Content maxWidth="460px">
        <Dialog.Title>输入账单解压密码</Dialog.Title>
        <Dialog.Description>密码只用于本次解锁请求，提交后立即从输入框清除。</Dialog.Description>
        <form className="evidence-unlock-form" onSubmit={(event) => void submit(event)}>
          <label htmlFor="evidence-unlock-password">解压密码</label>
          <TextField.Root
            autoComplete="off"
            autoFocus
            id="evidence-unlock-password"
            type="password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            aria-describedby={error ? 'evidence-unlock-error' : undefined}
          />
          {error ? <div className="auth-error" id="evidence-unlock-error" role="alert"><Warning size={17} />{error}</div> : null}
          <div className="auth-actions">
            <Button type="button" variant="outline" color="gray" disabled={busy} onClick={() => {
              setPassword('')
              setError(null)
              onClose()
            }}>取消</Button>
            <Button type="submit" disabled={busy || password.length === 0}>{busy ? '正在解锁' : '解锁账单'}</Button>
          </div>
        </form>
      </Dialog.Content>
    </Dialog.Root>
  )
}

export function FilesAndConnections({ candidates, connections, csrfToken, onOpenCandidate, onRefresh, onNotice }: {
  candidates: Candidate[]
  connections: ConnectionStatus[]
  csrfToken: string | null
  onOpenCandidate: (candidate: Candidate) => void
  onRefresh: () => Promise<boolean>
  onNotice: (notice: Notice) => void
}) {
  const [selectedUnlockEvidence, setSelectedUnlockEvidence] = useState<EvidenceReference | null>(null)
  const [dismissedUnlockSources, setDismissedUnlockSources] = useState<Set<string>>(() => new Set())
  const connection = (id: ConnectionStatus['id']) => connections.find((item) => item.id === id)
  const evidenceLibrary = [...candidates.reduce((items, candidate) => {
    for (const evidence of candidate.evidence) {
      const contentKey = evidence.sha256 ? `sha256:${evidence.sha256.toLowerCase()}` : `id:${evidence.id}`
      const unlockKey = evidence.unlock_status || evidence.source_ref
        ? `:unlock:${evidence.unlock_status ?? 'UNKNOWN'}:${evidence.source_ref ?? 'none'}`
        : ''
      const dedupeKey = `${contentKey}${unlockKey}`
      const current = items.get(dedupeKey) ?? {
        evidence,
        candidates: [] as Candidate[],
        sources: new Set<string>(),
        periods: new Set<string>(),
      }
      if (!current.candidates.some((item) => item.id === candidate.id)) current.candidates.push(candidate)
      current.sources.add(candidate.source)
      if (candidate.accountingMonth) current.periods.add(candidate.accountingMonth)
      items.set(dedupeKey, current)
    }
    return items
  }, new Map<string, { evidence: EvidenceReference; candidates: Candidate[]; sources: Set<string>; periods: Set<string> }>()).values()]
  const automaticUnlockEvidence = csrfToken
    ? evidenceLibrary.find(({ evidence }) => (
        evidence.unlock_status === 'PASSWORD_REQUIRED'
        && Boolean(evidence.source_ref)
        && !dismissedUnlockSources.has(evidence.source_ref ?? '')
      ))?.evidence ?? null
    : null
  const unlockEvidence = selectedUnlockEvidence ?? automaticUnlockEvidence
  const actionableCandidates = candidates.filter((candidate) => ['PENDING', 'INCOMPLETE', 'CONFLICTED'].includes(candidate.status))
  const materialGaps = [...actionableCandidates.reduce((items, candidate) => {
    for (const risk of candidate.reviewRisks) {
      if (!materialRiskCodes.has(risk.code)) continue
      const material = materialNameFor(candidate, risk.code)
      if (!material) continue
      const period = candidate.accountingMonth ?? ''
      const key = `${risk.code}:${material}:${period}`
      const current = items.get(key) ?? { material, period, reasons: new Set<string>(), candidates: new Set<string>() }
      current.reasons.add(risk.message)
      current.candidates.add(candidate.id)
      items.set(key, current)
    }
    return items
  }, new Map<string, { material: string; period: string; reasons: Set<string>; candidates: Set<string> }>()).values()]

  const closeUnlockDialog = () => {
    const dismissedSourceRef = unlockEvidence?.source_ref
    if (dismissedSourceRef) {
      setDismissedUnlockSources((current) => new Set(current).add(dismissedSourceRef))
    }
    setSelectedUnlockEvidence(null)
  }

  return (
    <>
      <PageHeader
        eyebrow="服务与文件"
        title="文件与连接"
        description="状态来自同源 API。界面不显示令牌或其他敏感凭据。"
        action={<Button variant="outline" color="gray" onClick={onRefresh}><ArrowsClockwise size={17} />重新检查</Button>}
      />

      <section className="panel evidence-library-panel">
        <div className="panel-heading"><div><h2>已导入账单与凭证</h2><p>按证据编号去重，展开后可进入关联候选查看原始内容。</p></div><Badge color="gray">{evidenceLibrary.length} 份</Badge></div>
        {evidenceLibrary.length > 0 ? (
          <div className="evidence-library-list">
            {evidenceLibrary.map((item) => {
              const hasPending = item.candidates.some((candidate) => ['PENDING', 'INCOMPLETE', 'CONFLICTED'].includes(candidate.status))
              const allConfirmed = item.candidates.every((candidate) => candidate.status === 'CONFIRMED')
              const status = hasPending ? '含待审核' : allConfirmed ? '已确认' : '已归档'
              return (
                <article className="evidence-library-item" key={item.evidence.id}>
                  <div className="evidence-library-title"><FileText size={20} /><div><strong>{item.evidence.original_filename ?? (item.evidence.kind === 'message' ? '消息原文' : '原始文件')}</strong><span>{[...item.sources].join('、')}</span></div><Badge color={hasPending ? 'amber' : allConfirmed ? 'green' : 'gray'}>{status}</Badge></div>
                  <div className="evidence-library-meta"><span>{[...item.periods].map(accountingMonthLabel).join('、') || '期间待确认'}</span><span>证据 {item.evidence.id}</span></div>
                  {item.evidence.unlock_status === 'PASSWORD_REQUIRED' ? (
                    <Button className="evidence-unlock-button" size="1" variant="soft" color="amber" disabled={!csrfToken || !item.evidence.source_ref} onClick={() => setSelectedUnlockEvidence(item.evidence)}>输入解压密码</Button>
                  ) : null}
                  <details>
                    <summary>关联 {item.candidates.length} 条候选</summary>
                    <div className="evidence-candidate-links">
                      {item.candidates.map((candidate) => <button key={candidate.id} onClick={() => onOpenCandidate(candidate)} type="button">{candidate.shortId} · {candidate.businessUnit} · {candidate.category}</button>)}
                    </div>
                  </details>
                </article>
              )
            })}
          </div>
        ) : <div className="empty-state compact-empty"><FolderOpen size={34} weight="light" /><h2>尚无已导入材料</h2><p>Core 返回的候选证据会显示在这里。</p></div>}
      </section>

      <section className="panel material-gap-panel" aria-label="待补账单清单">
        <div className="panel-heading"><div><h2>待补账单清单</h2><p>只展示 Core 明确标记的资金、关联账户和酒店结算材料缺口。</p></div><Badge color={materialGaps.length > 0 ? 'amber' : 'green'}>{materialGaps.length} 项</Badge></div>
        {materialGaps.length > 0 ? (
          <div className="material-gap-list">
            {materialGaps.map((gap) => (
              <article className="material-gap-card" key={`${gap.material}:${gap.period}`}>
                <Warning size={20} />
                <div><strong>{gap.material}</strong><span>{accountingMonthLabel(gap.period || null)}</span><span>影响 {gap.candidates.size} 条记录</span><p>{[...gap.reasons].join('；')}</p></div>
              </article>
            ))}
          </div>
        ) : <div className="empty-state compact-empty"><CheckCircle size={34} weight="light" /><h2>当前没有明确材料缺口</h2><p>这里只依据 Core 风险事实，不推测缺失账单。</p></div>}
      </section>

      <div className="connection-grid">
        <section className="panel connection-card">
          <div className="connection-title"><div className="service-icon onedrive"><CloudArrowUp size={24} weight="fill" /></div><div><h2>OneDrive Personal</h2><p>应用专用文件夹</p></div><ConnectionBadge connection={connection('onedrive_appfolder')} /></div>
          <p>仅访问 <code>Apps/LedgerBridge</code>，不读取 OneDrive 中的其他文件。</p>
          <div className="permission-line"><ShieldCheck size={17} /><span>计划权限：Files.ReadWrite.AppFolder</span></div>
          <Button disabled>连接功能尚未开放</Button>
        </section>
        <section className="panel connection-card">
          <div className="connection-title"><div className="service-icon hermes"><Database size={24} weight="fill" /></div><div><h2>Hermes 消息入口</h2><p>Telegram、钉钉、微信</p></div><ConnectionBadge connection={connection('hermes_ingress')} /></div>
          <p>只处理启用后的主账号私聊。家庭账号、群聊和历史消息均不在范围内。</p>
          <div className="permission-line"><ShieldCheck size={17} /><span>附件在消息入口即时提取与留证</span></div>
          <Button variant="soft" color="gray" disabled>规则由后台配置</Button>
        </section>
        <section className="panel connection-card">
          <div className="connection-title"><div className="service-icon office"><FileText size={24} weight="fill" /></div><div><h2>LibreOffice 计算服务</h2><p>Hermes 后台进程</p></div><ConnectionBadge connection={connection('libreoffice_worker')} /></div>
          <p>在临时副本上重算工作簿，检查公式错误和关键值，不覆盖原始文件。</p>
          <div className="permission-line"><Info size={17} /><span>结果标记为 LibreOffice 已验证</span></div>
          <Button variant="soft" color="gray" disabled>策略由后台配置</Button>
        </section>
      </div>

      <section className="panel files-panel">
        <div className="panel-heading"><div><h2>最近的工作簿</h2><p>连接 OneDrive 后显示应用文件夹中的版本</p></div><Button variant="outline" color="gray" disabled><CloudArrowUp size={17} />上传副本</Button></div>
        <div className="empty-state compact-empty"><FolderOpen size={34} weight="light" /><h2>尚未连接文件来源</h2><p>连接后，系统会显示可用于月度对账的工作簿副本。</p></div>
      </section>

      {unlockEvidence && csrfToken ? (
        <EvidenceUnlockDialog
          evidence={unlockEvidence}
          csrfToken={csrfToken}
          onClose={closeUnlockDialog}
          onUnlocked={async () => {
            const refreshed = await onRefresh()
            onNotice(refreshed
              ? { tone: 'success', message: '账单已解锁，数据已刷新' }
              : { tone: 'info', message: '已解锁，但列表刷新失败，请重试刷新' })
          }}
        />
      ) : null}
    </>
  )
}
