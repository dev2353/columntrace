import { useEffect, useMemo, useRef, useState } from 'react'
import type { FlowGraph } from '../lib/flow'
import { buildImpact, toCsv, toMarkdown, type ReportRow } from '../lib/report'

interface Props {
  fg: FlowGraph
  focusId: string
  onClose: () => void
}

function download(name: string, mime: string, text: string) {
  const url = URL.createObjectURL(new Blob([text], { type: mime }))
  const a = document.createElement('a')
  a.href = url
  a.download = name
  a.click()
  URL.revokeObjectURL(url)
}

// a page embedded in another site's frame (a shared viewer) usually may not save files: offer copy only
const canSave = (() => { try { return window.self === window.top } catch { return false } })()

function Section({ title, rows }: { title: string; rows: ReportRow[] }) {
  return (
    <section>
      <h3>{title} ({rows.length})</h3>
      {rows.length === 0 ? <p className="summary">none found</p> : (
        <table>
          <thead>
            <tr><th>Depth</th><th>Type</th><th>Name</th><th>Confidence</th><th>Via</th><th>Liveness</th><th>Last change</th></tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.id}>
                <td>{r.depth}</td><td>{r.type}</td><td>{r.name}</td><td>{r.confidence}</td><td>{r.via}</td>
                <td>{r.liveness}{r.stale ? ' · stale' : ''}</td><td>{r.lastChange}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  )
}

export default function ImpactModal({ fg, focusId, onClose }: Props) {
  const [strict, setStrict] = useState(false)
  const [config, setConfig] = useState(true)
  const [note, setNote] = useState('')
  const [manual, setManual] = useState<string | null>(null)   // shown when the clipboard is refused
  const closeRef = useRef<HTMLButtonElement>(null)
  const report = useMemo(
    () => buildImpact(fg, focusId, strict ? 'parsed' : 'inferred', config),
    [fg, focusId, strict, config],
  )
  const base = report.focus.name.replace(/[^\w.-]+/g, '_')

  const copy = (text: string, what: string) => {
    // called straight from the click: some viewers allow the clipboard only then
    navigator.clipboard?.writeText(text)
      .then(() => { setNote(`${what} copied`); setManual(null) })
      .catch(() => setManual(text))
    if (!navigator.clipboard) setManual(text)
  }

  useEffect(() => {
    closeRef.current?.focus()
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" role="dialog" aria-modal="true" aria-label={`Impact report for ${report.focus.name}`}
           onClick={(e) => e.stopPropagation()}>
        <header>
          <h2>Impact report: {report.focus.name}</h2>
          <label><input type="checkbox" checked={strict} onChange={(e) => setStrict(e.target.checked)} /> parsed edges only</label>
          <label><input type="checkbox" checked={config} onChange={(e) => setConfig(e.target.checked)} /> include config-resolved</label>
          <button className="btn" onClick={() => copy(toMarkdown(report), 'Markdown')} title="Copy the report as Markdown">Copy Markdown</button>
          <button className="btn" onClick={() => copy(toCsv(report), 'CSV')} title="Copy the rows as CSV (paste into Excel)">Copy CSV</button>
          {canSave ? (
            <>
              <button className="btn ghost" onClick={() => download(`impact_${base}.md`, 'text/markdown', toMarkdown(report))} title="Save as a file">.md</button>
              <button className="btn ghost" onClick={() => download(`impact_${base}.csv`, 'text/csv', toCsv(report))} title="Save as a file">.csv</button>
            </>
          ) : null}
          {note ? <span className="summary" role="status">{note}</span> : null}
          <button className="btn" ref={closeRef} onClick={onClose}>Close</button>
        </header>
        <div className="body">
          {manual !== null ? (
            <div className="manual-copy">
              <p className="summary">Copying is blocked here. Select the text below and copy it with Ctrl+C.</p>
              <textarea readOnly value={manual} aria-label="Report text" ref={(el) => el?.select()} />
              <button className="btn" onClick={() => setManual(null)}>Done</button>
            </div>
          ) : null}
          <p className="summary">
            Downstream size: <strong>{report.strictDownstream}</strong> using parsed edges only,{' '}
            <strong>{report.looseDownstream}</strong> including inferred edges. Liveness and last change are repo-only hints,
            not proof of use.
          </p>
          <Section title="Downstream: breaks if this changes" rows={report.downstream} />
          <Section title="Upstream: feeds this" rows={report.upstream} />
        </div>
      </div>
    </div>
  )
}
