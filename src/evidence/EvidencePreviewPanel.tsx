import { useEffect, useState } from 'react'
import {
  ArrowsClockwise,
  DownloadSimple,
  FileText,
  FileXls,
  ImageSquare,
  Info,
  Warning,
} from '@phosphor-icons/react'

import { api } from '../api'
import type { EvidencePreview, EvidenceReference } from '../types'

function billIdentityFields(fields: Array<{ label: string; value: string }>) {
  const priorities = [
    ['交易时间', '交易日期', '交易日', '记账日期', '记账日', '日期', '时间'],
    ['金额(元)', '交易金额', '账单金额', '付款金额', '收款金额', '金额'],
    ['对方名称', '交易对方', '对方户名', '收款人', '付款人', '商户名称', '商户', '户名'],
  ]
  const selected: Array<{ label: string; value: string }> = []
  for (const aliases of priorities) {
    const match = fields.find((field) => aliases.some((alias) => field.label.trim().includes(alias)))
    if (match && !selected.includes(match)) selected.push(match)
  }
  return selected
}

export function EvidencePreviewPanel({ evidence, reference }: {
  evidence: EvidenceReference
  reference: string
}) {
  const requestKey = `${evidence.id}:${reference}`
  const [result, setResult] = useState<{
    key: string
    preview: EvidencePreview | null
    error: string | null
  }>({ key: '', preview: null, error: null })

  useEffect(() => {
    let active = true
    api.getEvidencePreview(evidence.id, reference)
      .then((value) => { if (active) setResult({ key: requestKey, preview: value, error: null }) })
      .catch((reason: unknown) => {
        if (active) setResult({
          key: requestKey,
          preview: null,
          error: reason instanceof Error ? reason.message : '证据内容暂时无法读取',
        })
      })
    return () => { active = false }
  }, [evidence.id, reference, requestKey])

  const preview = result.key === requestKey ? result.preview : null
  const error = result.key === requestKey ? result.error : null

  const filename = evidence.original_filename ?? (evidence.kind === 'message' ? '消息原文' : '原始文件')
  const downloadHref = `/api/v1/evidence/${encodeURIComponent(evidence.id)}/content`

  return (
    <article className="evidence-preview-card">
      <header>
        <div>
          {preview?.kind === 'image' ? <ImageSquare size={17} /> : preview?.kind === 'spreadsheet' ? <FileXls size={17} /> : <FileText size={17} />}
          <span>{preview?.filename ?? filename}</span>
        </div>
        <a href={downloadHref} aria-label={`下载原文件：${filename}`} title="下载原文件">
          <DownloadSimple size={16} />
        </a>
      </header>

      {!preview && !error ? (
        <div className="evidence-preview-state" role="status"><ArrowsClockwise className="state-spinner" size={17} />正在读取证据内容</div>
      ) : null}
      {error ? (
        <div className="evidence-preview-state error"><Warning size={17} />{error}</div>
      ) : null}
      {preview?.kind === 'image' ? (
        <img className="evidence-image" src={preview.data_url} alt={`${preview.filename} 原始证据`} />
      ) : null}
      {preview?.kind === 'text' ? (
        <pre className="evidence-text">{preview.text}</pre>
      ) : null}
      {preview?.kind === 'spreadsheet' && preview.matched ? (
        <div className="evidence-records">
          {preview.records.map((record) => (
            <section key={`${record.sheet}-${record.row_number}`}>
              <div className="evidence-record-meta"><span>账单 {preview.reference ?? reference}</span><small>识别摘要</small></div>
              <dl>
                {billIdentityFields(record.fields).map((field, index) => (
                  <div key={`${index}-${field.label}`}><dt>{field.label}</dt><dd>{field.value}</dd></div>
                ))}
              </dl>
            </section>
          ))}
        </div>
      ) : null}
      {preview?.kind === 'spreadsheet' && !preview.matched && preview.fallback ? (
        <div className="evidence-sheet-fallback">
          <div className="evidence-record-meta"><span>{preview.fallback.sheet}</span><small>内容预览</small></div>
          <div className="evidence-sheet-scroll">
            <table>
              <tbody>
                {preview.fallback.rows.map((row) => (
                  <tr key={row.row_number}><th scope="row">{row.row_number}</th>{row.cells.map((cell, index) => <td key={index}>{cell}</td>)}</tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      ) : null}
      {preview?.kind === 'unsupported' ? (
        <div className="evidence-preview-state"><Info size={17} />{preview.reason}</div>
      ) : null}
    </article>
  )
}
