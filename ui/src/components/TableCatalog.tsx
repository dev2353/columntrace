// "All tables": every table in the file, searchable and sortable, with how it is used.
// The start page to browse from, and where Back ends up after the first table.

import { useMemo, useState } from 'react'
import type { ColumnGraph } from '../lib/colgraph'
import { FlowGraph, isConfigEdge } from '../lib/flow'
import { contractFor, matcherFor, type Filters } from '../lib/view'

interface Props {
  fg: FlowGraph
  cg: ColumnGraph | null
  filters: Filters
  onPick: (id: string) => void
}

interface Row {
  id: string
  name: string
  schema: string
  table: string
  semantic: boolean
  writers: number
  readers: number
  up: number
  down: number
  traced: boolean
  excluded: boolean
  missing: boolean
}

type Show = 'all' | 'sources' | 'unused' | 'columns' | 'missing'
type Sort = 'busy' | 'name' | 'up' | 'down'

const SHOW: [Show, string, string][] = [
  ['all', 'All', 'every table'],
  ['sources', 'Sources', 'nothing in the repo writes them: loaded from outside, or written by code the extractor cannot see'],
  ['unused', 'Not read', 'nothing in the repo reads them: final tables for reports, or candidates for clean-up'],
  ['columns', 'With column lineage', 'at least one column traced through SQL'],
  ['missing', 'Not in lakehouse', 'the code uses them, but the schema export does not list them'],
]

const PAGE = 200
const isConfig = (name: string) => /^(cfg|config)\./i.test(name)

export default function TableCatalog({ fg, cg, filters, onPick }: Props) {
  const [q, setQ] = useState('')
  const [schema, setSchema] = useState('')
  const [show, setShow] = useState<Show>('all')
  const [sort, setSort] = useState<Sort>('busy')
  const [withExcluded, setWithExcluded] = useState(false)
  const [limit, setLimit] = useState(PAGE)

  const excluded = matcherFor(filters)
  const tg = contractFor(fg, filters)
  const rows = useMemo<Row[]>(() => {
    const writers = new Map<string, Set<string>>()
    const readers = new Map<string, Set<string>>()
    const add = (m: Map<string, Set<string>>, k: string, v: string) => {
      const s = m.get(k)
      if (s) s.add(v)
      else m.set(k, new Set([v]))
    }
    for (const e of fg.edges) {
      if (isConfigEdge(e)) continue
      if (e.kind === 'writes') add(writers, e.to, e.from)
      else if (e.kind === 'reads' || e.kind === 'references') add(readers, e.to, e.from)
    }
    for (const f of fg.data.flows ?? []) { add(writers, f.to, f.job); add(readers, f.from, f.job) }
    const out: Row[] = []
    for (const n of fg.nodes.values()) {
      if (n.type !== 'table' && n.type !== 'semantic_table') continue
      const i = n.name.indexOf('.')
      out.push({
        id: n.id, name: n.name, schema: i > 0 ? n.name.slice(0, i) : '(no schema)', table: i > 0 ? n.name.slice(i + 1) : n.name,
        semantic: n.type === 'semantic_table',
        writers: writers.get(n.id)?.size ?? 0, readers: readers.get(n.id)?.size ?? 0,
        up: new Set((tg.up.get(n.id) ?? []).map((e) => e.from)).size,
        down: new Set((tg.down.get(n.id) ?? []).map((e) => e.to)).size,
        traced: !!cg?.hasLineage(n.id), excluded: excluded(n.name), missing: n.meta.inLakehouse === false,
      })
    }
    return out
  }, [fg, cg, tg, excluded])

  const schemas = useMemo(() => {
    const c = new Map<string, number>()
    for (const r of rows) if (withExcluded || !r.excluded) c.set(r.schema, (c.get(r.schema) ?? 0) + 1)
    return [...c].sort((a, b) => a[0].localeCompare(b[0]))
  }, [rows, withExcluded])

  const needle = q.trim().toLowerCase()
  const shown = rows
    .filter((r) => (withExcluded || !r.excluded) && (!schema || r.schema === schema) && (!needle || r.name.toLowerCase().includes(needle)))
    .filter((r) => show === 'all' || (show === 'sources' ? r.writers === 0 && !r.semantic
      : show === 'unused' ? r.readers === 0 && r.down === 0 : show === 'missing' ? r.missing : r.traced))
    .sort((a, b) => {
      if (sort === 'name') return a.name.localeCompare(b.name)
      // config tables are read by nearly everything: they would top every list, so they go last
      if (sort === 'busy' && isConfig(a.name) !== isConfig(b.name)) return isConfig(a.name) ? 1 : -1
      const d = sort === 'up' ? b.up - a.up : sort === 'down' ? b.down - a.down
        : (b.up + b.down + b.writers + b.readers) - (a.up + a.down + a.writers + a.readers)
      return d || a.name.localeCompare(b.name)
    })
  const nExcluded = rows.filter((r) => r.excluded).length
  const reset = () => setLimit(PAGE)

  return (
    <div className="catalog">
      <header className="catalog-head">
        <div>
          <h2>All tables <span className="muted">{shown.length.toLocaleString()} of {rows.length.toLocaleString()}</span></h2>
          <p className="hint">Pick a table to see its lineage. Counts follow the current filters; From and Feeds count tables, not jobs.</p>
        </div>
        <div className="catalog-controls">
          <input type="search" placeholder="Filter by name" aria-label="Filter tables by name" value={q}
                 onChange={(e) => { setQ(e.target.value); reset() }} />
          <select value={schema} onChange={(e) => { setSchema(e.target.value); reset() }} aria-label="Schema">
            <option value="">All schemas ({schemas.length})</option>
            {schemas.map(([s, n]) => <option key={s} value={s}>{s} ({n})</option>)}
          </select>
          <select value={sort} onChange={(e) => setSort(e.target.value as Sort)} aria-label="Sort by">
            <option value="busy">Most connected</option>
            <option value="up">Most sources</option>
            <option value="down">Most consumers</option>
            <option value="name">Name</option>
          </select>
        </div>
        <div className="seg" role="radiogroup" aria-label="Show">
          {SHOW.map(([k, label, tip]) => (
            <button key={k} role="radio" aria-checked={show === k} className={show === k ? 'on' : ''} title={tip}
                    disabled={k === 'columns' && !cg} onClick={() => { setShow(k); reset() }}>{label}</button>
          ))}
          {nExcluded ? (
            <label className="check-line" title="Tables matching the exclusion rules under Filters">
              <input type="checkbox" checked={withExcluded} onChange={(e) => setWithExcluded(e.target.checked)} />
              include {nExcluded} hidden by name
            </label>
          ) : null}
        </div>
      </header>
      <table className="catalog-table">
        <thead>
          <tr>
            <th>Table</th>
            <th className="num" title="Tables it is computed from">From</th>
            <th className="num" title="Tables computed from it">Feeds</th>
            <th className="num" title="Notebooks and pipelines that write it">Written by</th>
            <th className="num" title="Notebooks and pipelines that read it">Read by</th>
            <th title="Columns traced through SQL">Columns</th>
          </tr>
        </thead>
        <tbody>
          {shown.slice(0, limit).map((r) => (
            <tr key={r.id} onClick={() => onPick(r.id)} className={r.excluded ? 'excluded' : ''}>
              <td>
                <button className="row-link" onClick={(e) => { e.stopPropagation(); onPick(r.id) }}>
                  <span className="muted">{r.schema}.</span><strong>{r.table}</strong>
                </button>
                {r.semantic ? <span className="tag">semantic</span> : null}
                {r.excluded ? <span className="tag" title="Matches an exclusion rule">hidden</span> : null}
              </td>
              <td className="num">{r.up || '–'}</td>
              <td className="num">{r.down || '–'}</td>
              <td className="num">{r.writers || '–'}</td>
              <td className="num">{r.readers || '–'}</td>
              <td>{r.traced ? <span className="badge">traced</span> : <span className="muted">–</span>}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {shown.length === 0 ? <p className="hint catalog-empty">No table matches.</p> : null}
      {shown.length > limit ? (
        <div className="catalog-more">
          <button className="btn" onClick={() => setLimit((l) => l + PAGE)}>Show {Math.min(PAGE, shown.length - limit)} more</button>
          <span className="hint">{(shown.length - limit).toLocaleString()} not shown yet</span>
        </div>
      ) : null}
    </div>
  )
}
