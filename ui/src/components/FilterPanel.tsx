// The Filters drawer: everything that decides what the graph leaves out, grouped and
// explained. The exclusion list is an editable list of rules (kept in the browser).

import { useEffect, useMemo, useRef, useState } from 'react'
import { addKeep, addPatterns, compile, removeKeep, removeRule, toggleRule, type ExcludeSettings } from '../lib/exclude'
import type { FlowGraph } from '../lib/flow'
import { ALL_CONF, type Conf, type Liveness } from '../lib/types'
import { ACTIVE_LIVENESS, ALL_LIVENESS, type Filters, type TableType } from '../lib/view'

interface Props {
  fg: FlowGraph
  filters: Filters
  onChange: (patch: Partial<Filters>) => void
  onClose: () => void
  base: Filters                 // what "Reset" returns to: the team defaults, else the built-in ones
  teamDefaults: boolean         // a team-defaults.json was loaded
}

const LIVE_HELP: Record<Liveness, string> = {
  scheduled: 'has an enabled schedule',
  orchestrated: 'run by a scheduled pipeline or notebook',
  called: 'run by another item, but nothing scheduled reaches it',
  'entry-point': 'never run by another item, but runs others (likely started by hand)',
  'feeds-others': 'never run by another item, writes tables others read',
  'dead-end': 'never run by another item, and nothing reads what it writes',
  uncalled: 'a pipeline that is neither scheduled nor run by anything',
}
const CONF_HELP: Record<Conf, string> = {
  parsed: 'read from the code or JSON directly',
  inferred: 'a likely link, e.g. a table name resolved through a variable',
  unresolved: 'a link to a name built at run time',
}

function toggled<T>(set: Set<T>, v: T): Set<T> {
  const next = new Set(set)
  if (next.has(v)) next.delete(v)
  else next.add(v)
  return next
}

/** Tables shown even when a rule above would hide them (e.g. one temp table a team relies on). */
function KeepList({ names, value, onChange }: { names: string[]; value: ExcludeSettings; onChange: (s: ExcludeSettings) => void }) {
  const [text, setText] = useState('')
  const keep = value.keep ?? []
  const count = (p: string) => { const m = compile([p]); return names.reduce((a, n) => a + (m(n) ? 1 : 0), 0) }
  return (
    <div className="keep">
      <h4>Always show</h4>
      {keep.length ? (
        <ul className="rules" aria-label="Always shown">
          {keep.map((k) => (
            <li key={k} className="on keep-rule">
              <code>{k}</code>
              <span className="rule-count" title="Tables whose name matches">{count(k)}</span>
              <button className="btn icon ghost" aria-label={`Stop always showing ${k}`} title="Remove" onClick={() => onChange(removeKeep(value, k))}>×</button>
            </li>
          ))}
        </ul>
      ) : <p className="hint">Nothing yet. Add a table the rules above hide but you want to see, e.g. one temp table.</p>}
      <form className="rule-add" onSubmit={(e) => { e.preventDefault(); if (text.trim()) { onChange(addKeep(value, text)); setText('') } }}>
        <input type="text" placeholder="e.g. silver_x.tmp_orders" aria-label="Table to always show" value={text} onChange={(e) => setText(e.target.value)} />
        <button className="btn" type="submit" disabled={!text.trim()}>Add</button>
      </form>
    </div>
  )
}

function ExcludeEditor({ fg, value, base, onChange }: { fg: FlowGraph; value: ExcludeSettings; base: ExcludeSettings; onChange: (s: ExcludeSettings) => void }) {
  const [text, setText] = useState('')
  const names = useMemo(
    () => [...fg.nodes.values()].filter((n) => n.type === 'table' || n.type === 'semantic_table').map((n) => n.name),
    [fg],
  )
  const counts = useMemo(() => new Map(value.rules.map((r) => {
    const m = compile(r.patterns)
    return [r.id, names.reduce((a, n) => a + (m(n) ? 1 : 0), 0)]
  })), [value, names])
  const preview = useMemo(() => {
    const p = text.split(',').map((x) => x.trim()).filter(Boolean)
    if (!p.length) return null
    const m = compile(p)
    const hits = names.filter(m)
    return { n: hits.length, sample: hits.slice(0, 3) }
  }, [text, names])
  const add = () => {
    if (!text.trim()) return
    onChange(addPatterns(value, text))
    setText('')
  }
  return (
    <>
      <ul className="rules" aria-label="Exclusion rules">
        {value.rules.map((r) => (
          <li key={r.id} className={r.on ? 'on' : ''}>
            <label title={r.patterns.join(', ')}>
              <input type="checkbox" checked={r.on} onChange={() => onChange(toggleRule(value, r.id))} />
              <span className="rule-label">{r.label}</span>
              {r.patterns.join(' ') !== r.label ? <code>{r.patterns.join(' ')}</code> : null}
            </label>
            <span className="rule-count" title="Tables whose name matches">{counts.get(r.id) ?? 0}</span>
            <button className="btn icon ghost" aria-label={`Remove ${r.label}`} title="Remove from the list"
                    onClick={() => onChange(removeRule(value, r.id))}>×</button>
          </li>
        ))}
        {value.rules.length === 0 ? <li className="empty-rule">No rules: every table is shown.</li> : null}
      </ul>
      <form className="rule-add" onSubmit={(e) => { e.preventDefault(); add() }}>
        <input type="text" placeholder="Add a pattern, e.g. staging or *_old" aria-label="New exclusion pattern"
               value={text} onChange={(e) => setText(e.target.value)} />
        <button className="btn" type="submit" disabled={!text.trim()}>Add</button>
      </form>
      {preview ? (
        <p className="hint">
          {preview.n === 0 ? 'Matches no table yet.' : `Matches ${preview.n} table${preview.n === 1 ? '' : 's'}: ${preview.sample.join(', ')}${preview.n > 3 ? ', …' : ''}`}
        </p>
      ) : null}
      <KeepList names={names} value={value} onChange={onChange} />
      <p className="hint">
        A pattern matches anywhere in <code>schema.table</code>; <code>*</code> is a wildcard (<code>silver_ml.*</code>,
        <code>*_old</code>). Separate several with commas. The list is kept in this browser.{' '}
        <button className="link" onClick={() => onChange(base)}>Restore defaults</button>
      </p>
    </>
  )
}

export default function FilterPanel({ fg, filters, base, teamDefaults, onChange, onClose }: Props) {
  const ref = useRef<HTMLDivElement>(null)
  const allJobs = filters.liveness.size === ALL_LIVENESS.length
  const activeOnly = filters.liveness.size === ACTIVE_LIVENESS.length && ACTIVE_LIVENESS.every((l) => filters.liveness.has(l))
  const [custom, setCustom] = useState(!allJobs && !activeOnly)
  const jobCounts = useMemo(() => {
    const c = new Map<Liveness, number>()
    for (const n of fg.nodes.values()) {
      const l = n.meta.liveness
      if (l) c.set(l, (c.get(l) ?? 0) + 1)
    }
    return c
  }, [fg])
  const stale = useMemo(() => [...fg.nodes.values()].filter((n) => n.meta.stale).length, [fg])

  useEffect(() => {
    const key = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    const click = (e: MouseEvent) => {
      const t = e.target as Element
      if (ref.current && !ref.current.contains(t) && !t.closest('[data-filters-toggle]')) onClose()
    }
    window.addEventListener('keydown', key)
    window.addEventListener('mousedown', click)
    ref.current?.querySelector<HTMLElement>('input, button')?.focus()
    return () => { window.removeEventListener('keydown', key); window.removeEventListener('mousedown', click) }
  }, [onClose])

  const types: [TableType, string, string][] = [
    ['semantic_table', 'Semantic model tables', 'tables of Power BI semantic models, downstream of the lakehouse table they bind to'],
    ['unknown', 'Unresolved names', 'table names built at run time, which the extractor could not resolve'],
  ]

  return (
    <div className="drawer" ref={ref} role="dialog" aria-label="Filters">
      <header>
        <h2>Filters</h2>
        <button className="btn ghost" title={teamDefaults ? 'Back to the team defaults' : 'Back to the built-in defaults'}
                onClick={() => onChange({ ...base, mode: filters.mode, up: filters.up, down: filters.down })}>
          Reset filters
        </button>
        <button className="btn icon ghost" aria-label="Close" onClick={onClose}>×</button>
      </header>

      <section>
        <h3>Hide tables by name</h3>
        <p className="hint">Hidden tables are not drawn and lineage does not pass through them.</p>
        <ExcludeEditor fg={fg} value={filters.exclude} base={base.exclude} onChange={(exclude) => onChange({ exclude })} />
      </section>

      <section>
        <h3>Jobs that move the data</h3>
        <p className="hint">A flow between two tables is drawn only if a job below moves it. Status comes from the repo (schedules and calls), so it is a hint.</p>
        <div className="radios" role="radiogroup" aria-label="Jobs">
          <label><input type="radio" checked={allJobs && !custom} onChange={() => { setCustom(false); onChange({ liveness: new Set(ALL_LIVENESS) }) }} />All jobs</label>
          <label><input type="radio" checked={activeOnly && !custom} onChange={() => { setCustom(false); onChange({ liveness: new Set(ACTIVE_LIVENESS) }) }} />Only jobs something runs</label>
          <label><input type="radio" checked={custom || (!allJobs && !activeOnly)} onChange={() => setCustom(true)} />Pick statuses</label>
        </div>
        {custom || (!allJobs && !activeOnly) ? (
          <ul className="checks">
            {ALL_LIVENESS.map((l) => (
              <li key={l}>
                <label>
                  <input type="checkbox" checked={filters.liveness.has(l)} onChange={() => onChange({ liveness: toggled(filters.liveness, l) })} />
                  <span className={`badge ${l}`}>{l}</span><span className="count">{jobCounts.get(l) ?? 0}</span>
                </label>
                <span className="hint">{LIVE_HELP[l]}</span>
              </li>
            ))}
          </ul>
        ) : null}
        <label className="check-line">
          <input type="checkbox" checked={filters.staleOnly} onChange={(e) => onChange({ staleOnly: e.target.checked })} disabled={!stale} />
          Only stale jobs <span className="hint">(not changed for a long time; {stale} found{stale ? '' : ', extract with --dates'})</span>
        </label>
        <label className="check-line">
          Skip broad jobs over
          <select value={filters.maxPairs} onChange={(e) => onChange({ maxPairs: +e.target.value })} aria-label="Skip jobs over this many input and output pairs">
            {[6, 12, 24, 60, 150, 1000].map((n) => <option key={n} value={n}>{n} pairs</option>)}
          </select>
        </label>
        <p className="hint">A job that reads several tables and writes several cannot say which input feeds which output, so every pair is drawn. Generic loaders would connect everything: a job that writes more than 3 tables and has more pairs than this is paired through its column lineage where it has some, and otherwise skipped and counted.</p>
      </section>

      <section>
        <h3>Evidence</h3>
        <ul className="checks">
          {ALL_CONF.map((c) => (
            <li key={c}>
              <label><input type="checkbox" checked={filters.conf.has(c)} onChange={() => onChange({ conf: toggled(filters.conf, c) })} />
                <span className={`line-sample c-${c}`} aria-hidden="true" />{c}</label>
              <span className="hint">{CONF_HELP[c]}</span>
            </li>
          ))}
          <li>
            <label><input type="checkbox" checked={filters.showConfig} onChange={(e) => onChange({ showConfig: e.target.checked })} />config-resolved links</label>
            <span className="hint">tables a generic loader may touch, guessed from config tables (over-approximates; off by default)</span>
          </li>
        </ul>
      </section>

      <section>
        <h3>Also show</h3>
        <ul className="checks">
          {types.map(([t, label, help]) => (
            <li key={t}>
              <label><input type="checkbox" checked={filters.types.has(t)} onChange={() => onChange({ types: toggled(filters.types, t) })} />{label}</label>
              <span className="hint">{help}</span>
            </li>
          ))}
        </ul>
      </section>
    </div>
  )
}
