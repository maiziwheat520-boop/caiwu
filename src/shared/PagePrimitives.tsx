import type { ReactNode } from 'react'
import { Button } from '@radix-ui/themes'
import { ArrowsClockwise, Warning } from '@phosphor-icons/react'

export function PageHeader({ eyebrow, title, description, action }: {
  eyebrow: string
  title: string
  description: string
  action?: ReactNode
}) {
  return (
    <div className="page-header">
      <div>
        <span className="eyebrow">{eyebrow}</span>
        <h1>{title}</h1>
        <p>{description}</p>
      </div>
      {action ? <div className="page-action">{action}</div> : null}
    </div>
  )
}

export function LoadingState({
  title = '正在读取财务数据',
  description = '正在连接同源 API，并校验当前会话。',
}: {
  title?: string
  description?: string
}) {
  return (
    <div className="state-panel" role="status" aria-live="polite">
      <ArrowsClockwise className="state-spinner" size={30} />
      <h1>{title}</h1>
      <p>{description}</p>
    </div>
  )
}

export function ErrorState({ message, onRetry }: { message: string; onRetry: () => void }) {
  return (
    <div className="state-panel error-state" role="alert">
      <Warning size={31} weight="fill" />
      <h1>数据读取失败</h1>
      <p>{message}</p>
      <Button onClick={onRetry}><ArrowsClockwise size={17} />重试</Button>
    </div>
  )
}

export function Metric({ label, value, detail, icon, tone, primary = false }: {
  label: string
  value: string
  detail: string
  icon: ReactNode
  tone?: 'attention'
  primary?: boolean
}) {
  return (
    <article className={`metric ${primary ? 'primary' : ''} ${tone === 'attention' ? 'attention' : ''}`}>
      <div className="metric-icon">{icon}</div>
      <span>{label}</span>
      <strong>{value}</strong>
      <small>{detail}</small>
    </article>
  )
}

export function StatusLine({ icon, label, detail, tone }: { icon: React.ReactNode; label: string; detail: string; tone: string }) {
  return (
    <div className={`status-line ${tone}`}>
      <span className="status-icon">{icon}</span>
      <strong>{label}</strong>
      <span>{detail}</span>
    </div>
  )
}
