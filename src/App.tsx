import { useState } from 'react'
import {
  Badge,
  Button,
  Dialog,
  DropdownMenu,
  Select,
  Separator,
  TextField,
} from '@radix-ui/themes'
import {
  ArrowsClockwise,
  Bank,
  CaretRight,
  Check,
  CheckCircle,
  CloudArrowUp,
  Database,
  FileText,
  FolderOpen,
  House,
  Info,
  ListChecks,
  MagnifyingGlass,
  Paperclip,
  ShieldCheck,
  SlidersHorizontal,
  Table,
  Warning,
  X,
} from '@phosphor-icons/react'
import {
  createColumnHelper,
  flexRender,
  getCoreRowModel,
  useReactTable,
} from '@tanstack/react-table'
import { initialCandidates, reconciliationRows } from './data'
import type { Candidate, Notice, Page } from './types'

const navigation: Array<{ id: Page; label: string; icon: typeof House }> = [
  { id: 'overview', label: '概览', icon: House },
  { id: 'review', label: '待审核', icon: ListChecks },
  { id: 'reconciliation', label: '月度对账', icon: Table },
  { id: 'files', label: '文件与连接', icon: FolderOpen },
]

const currency = new Intl.NumberFormat('zh-CN', {
  style: 'currency',
  currency: 'CNY',
  minimumFractionDigits: 2,
})

const categoryTone: Record<string, 'blue' | 'green' | 'amber' | 'purple' | 'gray'> = {
  布草: 'purple',
  瓶装水: 'blue',
  水费: 'green',
  银行收款: 'amber',
  税费: 'gray',
}

function App() {
  const [page, setPage] = useState<Page>('overview')
  const [candidates, setCandidates] = useState(initialCandidates)
  const [selectedCandidate, setSelectedCandidate] = useState<Candidate | null>(null)
  const [notice, setNotice] = useState<Notice | null>(null)

  const pendingCandidates = candidates.filter((candidate) => candidate.status === 'pending')
  const confirmedCandidates = candidates.filter((candidate) => candidate.status === 'confirmed')

  const updateCandidate = (id: string, status: Candidate['status']) => {
    setCandidates((items) => items.map((item) => (item.id === id ? { ...item, status } : item)))
    setSelectedCandidate(null)
    setNotice({
      tone: 'success',
      message: status === 'confirmed' ? `${id} 已确认并进入本月草稿数据` : `${id} 已忽略，原始证据仍保留`,
    })
  }

  const renderPage = () => {
    if (page === 'overview') {
      return (
        <Overview
          pending={pendingCandidates}
          confirmed={confirmedCandidates}
          onNavigate={setPage}
          onOpenCandidate={setSelectedCandidate}
        />
      )
    }
    if (page === 'review') {
      return (
        <ReviewQueue
          candidates={pendingCandidates}
          onOpenCandidate={setSelectedCandidate}
          onUpdate={updateCandidate}
        />
      )
    }
    if (page === 'reconciliation') {
      return <Reconciliation confirmed={confirmedCandidates} onNavigate={setPage} />
    }
    return <FilesAndConnections />
  }

  return (
    <div className="app-shell">
      <aside className="sidebar" aria-label="主导航">
        <Brand />
        <nav className="side-nav">
          {navigation.map((item) => {
            const Icon = item.icon
            return (
              <button
                className={`nav-item ${page === item.id ? 'active' : ''}`}
                key={item.id}
                onClick={() => setPage(item.id)}
                type="button"
              >
                <Icon size={19} weight={page === item.id ? 'fill' : 'regular'} />
                <span>{item.label}</span>
                {item.id === 'review' && pendingCandidates.length > 0 ? (
                  <span className="nav-count">{pendingCandidates.length}</span>
                ) : null}
              </button>
            )
          })}
        </nav>
        <div className="sidebar-foot">
          <div className="secure-line">
            <ShieldCheck size={17} weight="fill" />
            <span>仅限本人访问</span>
          </div>
          <span>Hermes 内网服务</span>
        </div>
      </aside>

      <div className="main-column">
        <header className="topbar">
          <div className="mobile-brand"><Brand compact /></div>
          <div className="prototype-flag">
            <span className="flag-dot" />
            原型环境 · 合成数据
          </div>
          <DropdownMenu.Root>
            <DropdownMenu.Trigger>
              <Button variant="soft" color="gray" size="2">
                <span className="avatar">W</span>
                <span className="account-label">财务管理员</span>
              </Button>
            </DropdownMenu.Trigger>
            <DropdownMenu.Content align="end">
              <DropdownMenu.Item>通行密钥设置</DropdownMenu.Item>
              <DropdownMenu.Item>操作记录</DropdownMenu.Item>
            </DropdownMenu.Content>
          </DropdownMenu.Root>
        </header>

        {notice ? (
          <div className="notice" role="status">
            <CheckCircle size={18} weight="fill" />
            <span>{notice.message}</span>
            <button aria-label="关闭提示" onClick={() => setNotice(null)} type="button"><X size={16} /></button>
          </div>
        ) : null}

        <main className="content">{renderPage()}</main>
      </div>

      <nav className="bottom-nav" aria-label="移动端主导航">
        {navigation.map((item) => {
          const Icon = item.icon
          return (
            <button
              className={page === item.id ? 'active' : ''}
              key={item.id}
              onClick={() => setPage(item.id)}
              type="button"
            >
              <span className="bottom-icon-wrap">
                <Icon size={21} weight={page === item.id ? 'fill' : 'regular'} />
                {item.id === 'review' && pendingCandidates.length > 0 ? <i>{pendingCandidates.length}</i> : null}
              </span>
              {item.label}
            </button>
          )
        })}
      </nav>

      <CandidateDialog
        candidate={selectedCandidate}
        onClose={() => setSelectedCandidate(null)}
        onUpdate={updateCandidate}
      />
    </div>
  )
}

function Brand({ compact = false }: { compact?: boolean }) {
  return (
    <div className={`brand ${compact ? 'compact' : ''}`}>
      <div className="brand-mark"><Bank size={21} weight="fill" /></div>
      <div>
        <strong>LedgerBridge</strong>
        {!compact ? <span>财务工作台</span> : null}
      </div>
    </div>
  )
}

function PageHeader({ eyebrow, title, description, action }: { eyebrow: string; title: string; description: string; action?: React.ReactNode }) {
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

function Overview({
  pending,
  confirmed,
  onNavigate,
  onOpenCandidate,
}: {
  pending: Candidate[]
  confirmed: Candidate[]
  onNavigate: (page: Page) => void
  onOpenCandidate: (candidate: Candidate) => void
}) {
  const confirmedTotal = confirmed.reduce((total, candidate) => total + candidate.amount, 0)
  return (
    <>
      <PageHeader
        eyebrow="2026 年 8 月"
        title="早上好，今天有几项需要确认"
        description="消息只会形成候选数据。经过你确认后，才会进入月度对账草稿。"
        action={<Button onClick={() => onNavigate('review')}><ListChecks size={17} />开始审核</Button>}
      />

      <section className="metric-grid" aria-label="本月概览">
        <Metric label="待审核候选" value={`${pending.length} 条`} detail="其中 1 条存在冲突" tone="attention" icon={<ListChecks size={20} />} />
        <Metric label="本月已确认" value={currency.format(confirmedTotal)} detail={`${confirmed.length} 条可用于草稿`} icon={<CheckCircle size={20} />} />
        <Metric label="覆盖营业单元" value="3 家" detail="城南店、江景店、机场店" icon={<Database size={20} />} />
        <Metric label="数据连接" value="2 / 3" detail="消息入口与计算服务正常" icon={<CloudArrowUp size={20} />} />
      </section>

      <div className="overview-grid">
        <section className="panel queue-preview">
          <div className="panel-heading">
            <div>
              <h2>待审核</h2>
              <p>按风险和完整度排序</p>
            </div>
            <Button variant="ghost" onClick={() => onNavigate('review')}>查看全部<CaretRight size={15} /></Button>
          </div>
          <div className="preview-list">
            {pending.slice(0, 3).map((candidate) => (
              <button className="preview-row" key={candidate.id} onClick={() => onOpenCandidate(candidate)} type="button">
                <SourceIcon source={candidate.source} />
                <span className="preview-main">
                  <strong>{candidate.summary}</strong>
                  <small>{candidate.businessUnit} · {candidate.receivedAt} · {candidate.id}</small>
                </span>
                <span className="preview-value">
                  <strong>{currency.format(candidate.amount)}</strong>
                  {candidate.conflict ? <Badge color="red">冲突</Badge> : candidate.incomplete ? <Badge color="amber">缺月份</Badge> : <Badge color="blue">待确认</Badge>}
                </span>
                <CaretRight className="row-caret" size={17} />
              </button>
            ))}
          </div>
        </section>

        <section className="panel readiness-panel">
          <div className="panel-heading">
            <div>
              <h2>本月对账就绪度</h2>
              <p>在生成草稿前解决阻断项</p>
            </div>
            <span className="readiness-score">76%</span>
          </div>
          <div className="progress-track"><span style={{ width: '76%' }} /></div>
          <div className="readiness-list">
            <StatusLine icon={<Check size={17} />} label="江景店" detail="数据完整" tone="ok" />
            <StatusLine icon={<Warning size={17} />} label="城南店" detail="1 条收款冲突" tone="warn" />
            <StatusLine icon={<Info size={17} />} label="机场店" detail="1 条记录缺月份" tone="info" />
          </div>
          <Button className="full-button" variant="soft" onClick={() => onNavigate('reconciliation')}>查看月度对账</Button>
        </section>
      </div>

      <section className="panel audit-strip">
        <div className="audit-icon"><ShieldCheck size={23} weight="fill" /></div>
        <div>
          <h2>每个数字都能回到原始消息</h2>
          <p>确认、更正和忽略均以追加记录保存，不覆盖原始证据。</p>
        </div>
        <Button variant="outline" color="gray">查看操作记录</Button>
      </section>
    </>
  )
}

function Metric({ label, value, detail, icon, tone }: { label: string; value: string; detail: string; icon: React.ReactNode; tone?: 'attention' }) {
  return (
    <article className={`metric ${tone === 'attention' ? 'attention' : ''}`}>
      <div className="metric-icon">{icon}</div>
      <span>{label}</span>
      <strong>{value}</strong>
      <small>{detail}</small>
    </article>
  )
}

function StatusLine({ icon, label, detail, tone }: { icon: React.ReactNode; label: string; detail: string; tone: string }) {
  return (
    <div className={`status-line ${tone}`}>
      <span className="status-icon">{icon}</span>
      <strong>{label}</strong>
      <span>{detail}</span>
    </div>
  )
}

function ReviewQueue({ candidates, onOpenCandidate, onUpdate }: { candidates: Candidate[]; onOpenCandidate: (candidate: Candidate) => void; onUpdate: (id: string, status: Candidate['status']) => void }) {
  const [filter, setFilter] = useState('all')
  const filtered = candidates.filter((candidate) => filter === 'all' || candidate.source === filter)
  return (
    <>
      <PageHeader
        eyebrow="人工确认队列"
        title="待审核候选"
        description="逐条核对字段与原始证据。确认不会直接生成正式凭证。"
        action={<Button variant="outline" color="gray"><ArrowsClockwise size={17} />刷新</Button>}
      />
      <div className="filter-bar">
        <div className="filter-tabs" role="group" aria-label="来源筛选">
          {[
            ['all', '全部'],
            ['Telegram', 'Telegram'],
            ['钉钉', '钉钉'],
            ['微信', '微信'],
          ].map(([value, label]) => (
            <button className={filter === value ? 'active' : ''} key={value} onClick={() => setFilter(value)} type="button">{label}</button>
          ))}
        </div>
        <TextField.Root className="search-field" placeholder="搜索候选编号、门店或科目">
          <TextField.Slot><MagnifyingGlass size={16} /></TextField.Slot>
        </TextField.Root>
        <Button variant="soft" color="gray"><SlidersHorizontal size={17} />筛选</Button>
      </div>

      <section className="review-list" aria-label="候选数据列表">
        {filtered.length === 0 ? (
          <div className="empty-state">
            <CheckCircle size={34} weight="light" />
            <h2>当前筛选下没有待审核项</h2>
            <p>新的财务候选会在这里出现。</p>
          </div>
        ) : filtered.map((candidate) => (
          <article className={`candidate-card ${candidate.conflict ? 'has-conflict' : ''}`} key={candidate.id}>
            <div className="candidate-source">
              <SourceIcon source={candidate.source} />
              <div>
                <strong>{candidate.source}</strong>
                <span>{candidate.receivedAt}</span>
              </div>
            </div>
            <button className="candidate-body" onClick={() => onOpenCandidate(candidate)} type="button">
              <div className="candidate-tags">
                <Badge color={categoryTone[candidate.category] ?? 'gray'}>{candidate.category}</Badge>
                {candidate.conflict ? <Badge color="red">金额或凭证冲突</Badge> : null}
                {candidate.incomplete ? <Badge color="amber">缺少归属月份</Badge> : null}
              </div>
              <h2>{candidate.summary}</h2>
              <p>{candidate.evidence}</p>
              <div className="candidate-meta">
                <span>{candidate.businessUnit}</span>
                <span>{candidate.accountingMonth ?? '建议归入 2026-08'}</span>
                <span>置信度 {Math.round(candidate.confidence * 100)}%</span>
                {candidate.attachment ? <span><Paperclip size={14} />{candidate.attachment}</span> : null}
              </div>
            </button>
            <div className="candidate-amount">
              <span>提取金额</span>
              <strong>{currency.format(candidate.amount)}</strong>
              <small>{candidate.id}</small>
            </div>
            <div className="candidate-actions">
              <Button variant="soft" color="gray" onClick={() => onUpdate(candidate.id, 'ignored')}><X size={16} />忽略</Button>
              <Button disabled={candidate.conflict || candidate.incomplete} onClick={() => onUpdate(candidate.id, 'confirmed')}><Check size={16} />确认</Button>
            </div>
          </article>
        ))}
      </section>
    </>
  )
}

function SourceIcon({ source }: { source: Candidate['source'] }) {
  const initials: Record<Candidate['source'], string> = { Telegram: 'T', 钉钉: '钉', 微信: '微' }
  return <span className={`source-icon source-${source}`}>{initials[source]}</span>
}

function CandidateDialog({ candidate, onClose, onUpdate }: { candidate: Candidate | null; onClose: () => void; onUpdate: (id: string, status: Candidate['status']) => void }) {
  return (
    <Dialog.Root open={Boolean(candidate)} onOpenChange={(open) => { if (!open) onClose() }}>
      <Dialog.Content className="candidate-dialog" maxWidth="680px">
        {candidate ? (
          <>
            <div className="dialog-kicker"><SourceIcon source={candidate.source} /><span>{candidate.source} · {candidate.receivedAt} · {candidate.id}</span></div>
            <Dialog.Title>核对候选数据</Dialog.Title>
            <Dialog.Description>字段来自规则与模型提取，原始消息保持不变。</Dialog.Description>
            <div className="evidence-box">
              <span className="section-label">原始消息证据</span>
              <blockquote>{candidate.evidence}</blockquote>
              {candidate.attachment ? <Button variant="soft" color="gray"><Paperclip size={16} />{candidate.attachment}</Button> : null}
            </div>
            <div className="field-grid">
              <label><span>营业单元</span><TextField.Root defaultValue={candidate.businessUnit} /></label>
              <label><span>科目</span><TextField.Root defaultValue={candidate.category} /></label>
              <label><span>金额</span><TextField.Root defaultValue={candidate.amount.toFixed(2)} /></label>
              <label>
                <span>归属月份</span>
                <Select.Root defaultValue={candidate.accountingMonth ?? '2026-08'}>
                  <Select.Trigger />
                  <Select.Content><Select.Item value="2026-08">2026 年 8 月</Select.Item><Select.Item value="2026-07">2026 年 7 月</Select.Item></Select.Content>
                </Select.Root>
              </label>
            </div>
            {candidate.conflict ? <div className="blocking-note"><Warning size={18} weight="fill" /><span><strong>需要先处理冲突</strong>另一条候选使用了相同凭证号但金额不同。</span></div> : null}
            {candidate.incomplete ? <div className="blocking-note amber"><Info size={18} weight="fill" /><span><strong>月份为系统建议</strong>请确认归属月份后再提交。</span></div> : null}
            <Separator my="4" size="4" />
            <div className="dialog-actions">
              <Button variant="soft" color="gray" onClick={onClose}>取消</Button>
              <Button variant="outline" color="gray" onClick={() => onUpdate(candidate.id, 'ignored')}>忽略候选</Button>
              <Button disabled={candidate.conflict} onClick={() => onUpdate(candidate.id, 'confirmed')}>保存更正并确认</Button>
            </div>
          </>
        ) : null}
      </Dialog.Content>
    </Dialog.Root>
  )
}

type ReconciliationRow = (typeof reconciliationRows)[number]
const columnHelper = createColumnHelper<ReconciliationRow>()
const columns = [
  columnHelper.accessor('unit', { header: '营业单元', cell: (info) => <strong>{info.getValue()}</strong> }),
  columnHelper.accessor('water', { header: '水费', cell: (info) => currency.format(info.getValue()) }),
  columnHelper.accessor('tax', { header: '税费', cell: (info) => currency.format(info.getValue()) }),
  columnHelper.accessor('linen', { header: '布草', cell: (info) => currency.format(info.getValue()) }),
  columnHelper.accessor('bottledWater', { header: '瓶装水', cell: (info) => currency.format(info.getValue()) }),
  columnHelper.accessor('receipts', { header: '银行收款', cell: (info) => currency.format(info.getValue()) }),
  columnHelper.accessor('readiness', {
    header: '状态',
    cell: (info) => {
      const value = info.getValue()
      return <Badge color={value === '可生成' ? 'green' : value === '有冲突' ? 'red' : 'amber'}>{value}</Badge>
    },
  }),
]

function Reconciliation({ confirmed, onNavigate }: { confirmed: Candidate[]; onNavigate: (page: Page) => void }) {
  const table = useReactTable({ data: reconciliationRows, columns, getCoreRowModel: getCoreRowModel() })
  const monthTotal = reconciliationRows.reduce((sum, row) => sum + row.water + row.tax + row.linen + row.bottledWater + row.receipts, 0)
  return (
    <>
      <PageHeader
        eyebrow="酒店月度对账"
        title="2026 年 8 月对账草稿"
        description="当前是预览层。确认保存仍由原程序完成，候选不会直接入正式账。"
        action={<Select.Root defaultValue="2026-08"><Select.Trigger /><Select.Content><Select.Item value="2026-08">2026 年 8 月</Select.Item><Select.Item value="2026-07">2026 年 7 月</Select.Item></Select.Content></Select.Root>}
      />

      <div className="blocking-banner" role="alert">
        <Warning size={21} weight="fill" />
        <div><strong>本月草稿尚不可生成</strong><span>1 条收款冲突和 1 条缺失月份记录需要处理。</span></div>
        <Button color="red" variant="soft" onClick={() => onNavigate('review')}>处理阻断项</Button>
      </div>

      <section className="metric-grid reconciliation-metrics">
        <Metric label="本月汇总" value={currency.format(monthTotal)} detail="跨 3 个营业单元" icon={<Bank size={20} />} />
        <Metric label="已确认来源" value={`${confirmed.length} 条`} detail="均可回溯至原始证据" icon={<CheckCircle size={20} />} />
        <Metric label="待处理" value="2 条" detail="冲突 1 条，缺字段 1 条" icon={<Warning size={20} />} tone="attention" />
        <Metric label="计算验证" value="待运行" detail="草稿生成后由 LibreOffice 校验" icon={<ArrowsClockwise size={20} />} />
      </section>

      <section className="panel table-panel">
        <div className="panel-heading">
          <div><h2>营业单元汇总</h2><p>只展示审核通过或既有数据库中的数据</p></div>
          <Button disabled><FileText size={17} />生成对账草稿</Button>
        </div>
        <div className="desktop-table-wrap">
          <table>
            <thead>{table.getHeaderGroups().map((group) => <tr key={group.id}>{group.headers.map((header) => <th key={header.id}>{flexRender(header.column.columnDef.header, header.getContext())}</th>)}</tr>)}</thead>
            <tbody>{table.getRowModel().rows.map((row) => <tr key={row.id}>{row.getVisibleCells().map((cell) => <td key={cell.id}>{flexRender(cell.column.columnDef.cell, cell.getContext())}</td>)}</tr>)}</tbody>
            <tfoot><tr><td>本月合计</td><td colSpan={4}>按营业单元与科目汇总</td><td>{currency.format(reconciliationRows.reduce((sum, row) => sum + row.receipts, 0))}</td><td><Badge color="red">阻断</Badge></td></tr></tfoot>
          </table>
        </div>
        <div className="mobile-reconciliation-list">
          {reconciliationRows.map((row) => (
            <article key={row.unit}>
              <div><strong>{row.unit}</strong><Badge color={row.readiness === '可生成' ? 'green' : row.readiness === '有冲突' ? 'red' : 'amber'}>{row.readiness}</Badge></div>
              <dl><dt>运营支出</dt><dd>{currency.format(row.water + row.tax + row.linen + row.bottledWater)}</dd><dt>银行收款</dt><dd>{currency.format(row.receipts)}</dd></dl>
            </article>
          ))}
          <p className="mobile-grid-note"><Info size={16} />完整科目网格请在平板或电脑查看。</p>
        </div>
      </section>

      <section className="panel provenance-panel">
        <div className="panel-heading"><div><h2>数据来源说明</h2><p>每次生成都记录输入版本与计算结果</p></div></div>
        <div className="provenance-steps">
          <div><span>1</span><strong>人工确认</strong><small>消息候选</small></div>
          <CaretRight size={18} />
          <div><span>2</span><strong>写入草稿</strong><small>不可直接入账</small></div>
          <CaretRight size={18} />
          <div><span>3</span><strong>公式校验</strong><small>LibreOffice</small></div>
          <CaretRight size={18} />
          <div><span>4</span><strong>原程序确认</strong><small>正式保存</small></div>
        </div>
      </section>
    </>
  )
}

function FilesAndConnections() {
  return (
    <>
      <PageHeader
        eyebrow="服务与文件"
        title="文件与连接"
        description="连接状态仅为原型展示。尚未请求真实账户权限，也没有存储凭据。"
        action={<Button variant="outline" color="gray"><ArrowsClockwise size={17} />重新检查</Button>}
      />

      <div className="connection-grid">
        <section className="panel connection-card">
          <div className="connection-title"><div className="service-icon onedrive"><CloudArrowUp size={24} weight="fill" /></div><div><h2>OneDrive Personal</h2><p>应用专用文件夹</p></div><Badge color="amber">未连接</Badge></div>
          <p>仅访问 <code>Apps/LedgerBridge</code>，不读取 OneDrive 中的其他文件。</p>
          <div className="permission-line"><ShieldCheck size={17} /><span>计划权限：Files.ReadWrite.AppFolder</span></div>
          <Button>连接 OneDrive</Button>
        </section>
        <section className="panel connection-card">
          <div className="connection-title"><div className="service-icon hermes"><Database size={24} weight="fill" /></div><div><h2>Hermes 消息入口</h2><p>Telegram、钉钉、微信</p></div><Badge color="green">原型正常</Badge></div>
          <p>只处理启用后的主账号私聊。家庭账号、群聊和历史消息均不在范围内。</p>
          <div className="permission-line"><ShieldCheck size={17} /><span>附件在消息入口即时提取与留证</span></div>
          <Button variant="soft" color="gray">查看入口规则</Button>
        </section>
        <section className="panel connection-card">
          <div className="connection-title"><div className="service-icon office"><FileText size={24} weight="fill" /></div><div><h2>LibreOffice 计算服务</h2><p>Hermes 后台进程</p></div><Badge color="gray">尚未安装</Badge></div>
          <p>在临时副本上重算工作簿，检查公式错误和关键值，不覆盖原始文件。</p>
          <div className="permission-line"><Info size={17} /><span>结果标记为 LibreOffice 已验证</span></div>
          <Button variant="soft" color="gray">查看验证策略</Button>
        </section>
      </div>

      <section className="panel files-panel">
        <div className="panel-heading"><div><h2>最近的工作簿</h2><p>连接 OneDrive 后显示应用文件夹中的版本</p></div><Button variant="outline" color="gray" disabled><CloudArrowUp size={17} />上传副本</Button></div>
        <div className="empty-state compact-empty"><FolderOpen size={34} weight="light" /><h2>尚未连接文件来源</h2><p>连接后，系统会显示可用于月度对账的工作簿副本。</p></div>
      </section>
    </>
  )
}

export default App
