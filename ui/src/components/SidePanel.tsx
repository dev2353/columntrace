import { Fragment, useMemo, useState } from 'react'
import { colId, parseColId, type ColRef, type ColumnGraph } from '../lib/colgraph'
import type { FlowGraph } from '../lib/flow'
import type { ColumnEdge, LNode } from '../lib/types'
import { familyOf, usedBy, type UseRow } from '../lib/usage'
import type { Mode, View, ViewEdge } from '../lib/view'

interface Props {
  fg: FlowGraph
  view: View
  mode: Mode
  cg: ColumnGraph | null
  colFocus: ColRef | null
  selectedId: string | null
  edge: ViewEdge | null
  focusId: string | null
  onFocus: (id: string) => void       // a table
  onPickColumn: (table: string, column: string) => void
  onOpenJob: (id: string) => void     // job lens: the tables one notebook/pipeline touches
  onImpact: (id: string) => void
  onHide: (name: string) => void      // add the table's name to the exclusion list
}

const fmtDate = (iso?: string) => (iso ? iso.slice(0, 10) : 'unknown')

function JobBadges({ n }: { n: LNode }) {
  const m = n.meta
  return (
    <>
      {m.liveness ? <span className={`badge ${m.liveness}`}>{m.liveness}</span> : null}
      {m.stale ? <span className="badge stale">stale</span> : null}
      {m.bulkCommit ? <span className="badge" title={`${m.commitSpread} items share this commit`}>bulk commit</span> : null}
    </>
  )
}

interface SetAside { names: number; byItem: Record<string, string[]> }
const setAside = (fg: FlowGraph) => fg.data.meta.annotations?.unqualifiedSetAside as SetAside | undefined
const lakehouse = (fg: FlowGraph) => fg.data.meta.annotations?.lakehouse as { checked: number; missing: number } | undefined

function JobFacts({ fg, job }: { fg: FlowGraph; job: LNode }) {
  const m = job.meta
  const family = familyOf(fg, job)
  const bare = setAside(fg)?.byItem[job.id] ?? []
  return (
    <>
      <dl>
        <dt>Type</dt><dd>{job.type}</dd>
        {job.path ? <><dt>Path</dt><dd>{job.path}</dd></> : null}
        <dt>Liveness</dt><dd><JobBadges n={job} /></dd>
        {bare.length ? (
          <><dt>Not tables</dt><dd title="Names without a schema that nothing writes and the default schema does not have: CTEs, temp views or DuckDB relations">
            {bare.slice(0, 12).join(', ')}{bare.length > 12 ? ` and ${bare.length - 12} more` : ''}</dd></>
        ) : null}
        {m.lastChange ? <><dt>Last change</dt><dd>{fmtDate(m.lastChange)} ({m.ageDays} days ago)</dd></> : null}
        {m.schedules ? <><dt>Schedules</dt><dd>{m.schedules.enabled} enabled of {m.schedules.total}</dd></> : null}
      </dl>
      {family.length > 1 ? (
        <>
          <h3>Variants ({family.length})</h3>
          <p className="summary">Same name apart from a suffix. Check which one is current before removing any.</p>
          <table className="members">
            <thead><tr><th>Variant</th><th>Liveness</th><th>Last change</th></tr></thead>
            <tbody>
              {family.map((v) => (
                <tr key={v.id}>
                  <td>{v.name}{v.meta.newestInFamily ? ' ★' : ''}{v.id === job.id ? ' (this)' : ''}</td>
                  <td>{v.meta.liveness}</td>
                  <td>{fmtDate(v.meta.lastChange)}{v.meta.stale ? ' (stale)' : ''}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="summary">★ newest by last change</p>
        </>
      ) : null}
    </>
  )
}

function Rows({ rows, onOpenJob }: { rows: UseRow[]; onOpenJob: (id: string) => void }) {
  return (
    <ul className="usedby">
      {rows.map((r) => (
        <li key={`${r.job.id}|${r.how}`}>
          <div className="usedby-head">
            <button className="btn" title="Show the tables this job touches" onClick={() => onOpenJob(r.job.id)}>{r.job.name}</button>
            <span className="tag">{r.job.type}</span>
            <JobBadges n={r.job} />
            {r.config ? <span className="tag" title="Attached by expanding a config table, not seen in the job's code">via config</span> : null}
            {r.how === 'references' ? <span className="tag" title="Mentioned as a string; read or write unknown">mentions</span> : null}
            {r.conf !== 'parsed' ? <span className="tag">{r.conf}</span> : null}
          </div>
          <div className="summary">
            {r.job.meta.lastChange ? `changed ${fmtDate(r.job.meta.lastChange)}` : ''}
            {r.evidence?.file ? `${r.job.meta.lastChange ? ' · ' : ''}${r.evidence.file}${r.evidence.line ? `:${r.evidence.line}` : ''}` : ''}
            {r.evidence?.activity ? ` · activity ${r.evidence.activity}` : ''}
          </div>
          {r.evidence?.snippet ? (
            <details><summary>code</summary><code className="snippet">{r.evidence.snippet}</code></details>
          ) : null}
        </li>
      ))}
    </ul>
  )
}

function UsedBy({ fg, tableId, onOpenJob, onFocus }: { fg: FlowGraph; tableId: string; onOpenJob: (id: string) => void; onFocus: (id: string) => void }) {
  const [q, setQ] = useState('')
  const [hideConfig, setHideConfig] = useState(false)
  const [all, setAll] = useState(false)
  const usage = useMemo(() => usedBy(fg, tableId), [fg, tableId])
  const keep = (r: UseRow) => (!hideConfig || !r.config) && r.job.name.toLowerCase().includes(q.trim().toLowerCase())
  const writers = usage.writers.filter(keep)
  const readers = usage.readers.filter(keep)
  const LIMIT = 40
  const configCount = [...usage.writers, ...usage.readers].filter((r) => r.config).length
  const show = (rows: UseRow[]) => (all ? rows : rows.slice(0, LIMIT))

  return (
    <>
      <h3>Used by</h3>
      <div className="used-controls">
        <input type="search" placeholder="Filter notebooks and pipelines" aria-label="Filter jobs" value={q} onChange={(e) => setQ(e.target.value)} />
        {configCount > 0 ? (
          <label><input type="checkbox" checked={hideConfig} onChange={(e) => setHideConfig(e.target.checked)} />hide config-attached ({configCount})</label>
        ) : null}
      </div>
      <h4>Written by ({writers.length})</h4>
      {writers.length ? <Rows rows={show(writers)} onOpenJob={onOpenJob} /> : <p className="summary">nothing found</p>}
      <h4>Read by ({readers.length})</h4>
      {readers.length ? <Rows rows={show(readers)} onOpenJob={onOpenJob} /> : <p className="summary">nothing found</p>}
      {!all && (writers.length > LIMIT || readers.length > LIMIT) ? (
        <button className="btn" onClick={() => setAll(true)}>Show all</button>
      ) : null}
      {usage.binds.length ? (
        <>
          <h4>Semantic tables built on it ({usage.binds.length})</h4>
          <div className="row-actions">{usage.binds.map((s) => <button key={s.id} className="btn" onClick={() => onFocus(s.id)}>{s.name}</button>)}</div>
        </>
      ) : null}
      {usage.boundTo.length ? (
        <>
          <h4>Built on</h4>
          <div className="row-actions">{usage.boundTo.map((s) => <button key={s.id} className="btn" onClick={() => onFocus(s.id)}>{s.name}</button>)}</div>
        </>
      ) : null}
    </>
  )
}

const TRANSFORM_NOTE: Record<string, string> = {
  direct: 'copied as is', expression: 'computed by an expression', aggregate: 'aggregated', star: 'SELECT * that could not be expanded (schema unknown)',
}

function ColumnLinks({ fg, title, links, onPickColumn, onOpenJob }: {
  fg: FlowGraph; title: string; links: Map<string, ColumnEdge[]> | undefined
  onPickColumn: (t: string, c: string) => void; onOpenJob: (id: string) => void
}) {
  const rows = [...(links ?? [])]
  return (
    <>
      <h3>{title} ({rows.length})</h3>
      {rows.length === 0 ? <p className="summary">none recorded</p> : (
        <ul className="usedby">
          {rows.map(([other, edges]) => {
            const r = parseColId(other)
            return (
              <li key={other}>
                <div className="usedby-head">
                  <button className="btn" title="Trace this column" onClick={() => onPickColumn(r.table, r.column)}>
                    {(fg.nodes.get(r.table)?.name ?? r.table)}.{r.column}
                  </button>
                  {[...new Set(edges.map((e) => e.transform))].map((t) => <span key={t} className="tag" title={TRANSFORM_NOTE[t]}>{t}</span>)}
                  {edges.every((e) => e.confidence === 'inferred') ? <span className="tag">inferred</span> : null}
                </div>
                {edges.slice(0, 3).map((e, i) => (
                  <div key={i} className="summary">
                    <button className="btn" onClick={() => onOpenJob(e.job)}>{fg.nodes.get(e.job)?.name ?? e.job}</button>
                    {e.line ? ` line ${e.line}` : ''}
                    {e.expr && e.transform !== 'direct' ? <code className="snippet">{e.expr}</code> : null}
                  </div>
                ))}
              </li>
            )
          })}
        </ul>
      )}
    </>
  )
}

/** Why a column has no lineage: who writes its table, and what usually hides a column. */
function NoLineage({ fg, table, column, onOpenJob }: { fg: FlowGraph; table: string; column: string; onOpenJob: (id: string) => void }) {
  const writers = usedBy(fg, table).writers.filter((r) => !r.config)
  return (
    <div className="warn-note" role="note">
      <strong>No lineage recorded for {column}.</strong>{' '}
      {writers.length ? (
        <>
          Its table is written by{' '}
          {writers.slice(0, 3).map((w, i) => (
            <span key={w.job.id}>{i ? ', ' : ''}<button className="link" onClick={() => onOpenJob(w.job.id)}>{w.job.name}</button>
              {w.evidence?.line ? ` (line ${w.evidence.line})` : ''}</span>
          ))}
          {writers.length > 3 ? ` and ${writers.length - 3} more` : ''}. The column is probably added there by DataFrame
          code the extractor does not follow (withColumn, a join or union built in Python) or under a name built at run time.
        </>
      ) : (
        <>No notebook or pipeline in the repo writes this table, so its columns have no recorded source.</>
      )}
    </div>
  )
}

function ColumnPanel({ fg, cg, focus, traced, onPickColumn, onFocus, onOpenJob }: {
  fg: FlowGraph; cg: ColumnGraph; focus: ColRef; traced: boolean
  onPickColumn: (t: string, c: string) => void; onFocus: (id: string) => void; onOpenJob: (id: string) => void
}) {
  const id = colId(focus)
  const tname = fg.nodes.get(focus.table)?.name ?? focus.table
  return (
    <aside className="side" aria-label="Details">
      <p className="eyebrow">Column</p>
      <h2>{tname}.{focus.column}</h2>
      <div className="row-actions">
        {!traced ? <button className="btn primary" onClick={() => onPickColumn(focus.table, focus.column)}>Trace from this column</button> : null}
        <button className="btn" onClick={() => onFocus(focus.table)}>Table lineage of {tname}</button>
      </div>
      {!cg.up.get(id)?.size && !cg.down.get(id)?.size ? <NoLineage fg={fg} table={focus.table} column={focus.column} onOpenJob={onOpenJob} /> : null}
      <ColumnLinks fg={fg} title="Computed from" links={cg.up.get(id)} onPickColumn={onPickColumn} onOpenJob={onOpenJob} />
      <ColumnLinks fg={fg} title="Feeds" links={cg.down.get(id)} onPickColumn={onPickColumn} onOpenJob={onOpenJob} />
      <p className="summary">
        Column lineage follows the SQL a notebook runs. Join keys and filters decide which rows flow, not which
        columns, so they are not shown.
      </p>
    </aside>
  )
}

function ColumnsSection({ cg, tableId, onPickColumn }: { cg: ColumnGraph; tableId: string; onPickColumn: (t: string, c: string) => void }) {
  const [q, setQ] = useState('')
  const [onlyTraced, setOnlyTraced] = useState(false)
  const [all, setAll] = useState(false)
  const rows = useMemo(
    () => cg.columnsOf(tableId).filter((c) => c !== '*').map((c) => ({ c, ...cg.counts(tableId, c) })),
    [cg, tableId],
  )
  if (rows.length === 0) return null
  const traced = rows.filter((r) => r.sources || r.consumers)
  const untraced = rows.length - traced.length
  const shown = (onlyTraced ? traced : rows).filter((r) => r.c.toLowerCase().includes(q.trim().toLowerCase()))
  const LIMIT = 80
  return (
    <>
      <h3>Columns ({rows.length})</h3>
      <p className="summary">
        {traced.length} with lineage{untraced ? `, ${untraced} without` : ''}.
        {untraced ? ' A column without lineage is computed by code the extractor cannot follow (DataFrame steps, dynamic names) or only read here.' : ''}
      </p>
      <div className="used-controls">
        {rows.length > 8 ? (
          <input type="search" placeholder="Filter columns" aria-label="Filter columns" value={q} onChange={(e) => setQ(e.target.value)} />
        ) : null}
        {untraced && traced.length ? (
          <label><input type="checkbox" checked={onlyTraced} onChange={(e) => setOnlyTraced(e.target.checked)} />only columns with lineage</label>
        ) : null}
      </div>
      <div className="row-actions" style={{ flexDirection: 'column', alignItems: 'stretch' }}>
        {(all ? shown : shown.slice(0, LIMIT)).map((r) => {
          const has = r.sources || r.consumers
          return (
            <button key={r.c} className={`btn colrow ${has ? '' : 'untraced'}`} onClick={() => onPickColumn(tableId, r.c)}
                    title={has ? 'Trace this column' : 'No lineage recorded: open it to see why'}>
              <span>{r.c}</span>
              <span className="summary">
                {has ? <>{r.sources ? `← ${r.sources}` : ''} {r.consumers ? `→ ${r.consumers}` : ''}</> : 'no lineage'}
              </span>
            </button>
          )
        })}
      </div>
      {!all && shown.length > LIMIT ? (
        <button className="btn" onClick={() => setAll(true)}>Show all {shown.length} columns</button>
      ) : null}
    </>
  )
}

export default function SidePanel({ fg, view, mode, cg, colFocus, selectedId, edge, focusId, onFocus, onPickColumn, onOpenJob, onImpact, onHide }: Props) {
  const nameOf = (id: string) => {
    if (id.includes('#')) {
      const r = parseColId(id)
      return `${fg.nodes.get(r.table)?.name ?? r.table}.${r.column}`
    }
    return fg.nodes.get(id)?.name ?? view.nodes.find((n) => n.id === id)?.label ?? id
  }

  // columns mode: a column, a column edge, or the focus column (a table card falls through)
  if (mode === 'columns' && cg && !(selectedId && !selectedId.includes('#'))) {
    if (edge?.columnEdges && !selectedId) {
      const es = edge.columnEdges
      return (
        <aside className="side" aria-label="Details">
          <p className="eyebrow">Column link</p>
          <h2>{nameOf(edge.sourceCol ?? edge.source)} → {nameOf(edge.targetCol ?? edge.target)}</h2>
          <p className="summary">{[...new Set(es.map((e) => TRANSFORM_NOTE[e.transform]))].join('; ')} · <span className="tag">{edge.conf}</span></p>
          <h3>SQL ({es.length})</h3>
          <ul className="usedby">
            {es.map((e, i) => (
              <li key={i}>
                <div className="usedby-head">
                  <button className="btn" onClick={() => onOpenJob(e.job)}>{fg.nodes.get(e.job)?.name ?? e.job}</button>
                  <span className="tag">{e.transform}</span>
                  <span className="tag">{e.confidence}</span>
                  {e.line ? <span className="summary">line {e.line}</span> : null}
                </div>
                {e.expr ? <code className="snippet">{e.expr}</code> : null}
              </li>
            ))}
          </ul>
        </aside>
      )
    }
    const ref = selectedId && selectedId.includes('#') ? parseColId(selectedId) : colFocus
    if (ref) {
      const traced = !!colFocus && colId(colFocus) === colId(ref)
      return <ColumnPanel key={colId(ref)} fg={fg} cg={cg} focus={ref} traced={traced} onPickColumn={onPickColumn} onFocus={onFocus} onOpenJob={onOpenJob} />
    }
    return (
      <aside className="side" aria-label="Details">
        <h2>Column lineage</h2>
        <p className="summary">Open a table, then pick one of its columns to trace where it comes from and what it feeds.</p>
      </aside>
    )
  }

  if (edge && !selectedId) {
    const isSchema = edge.pairs !== undefined
    const jobs = edge.via ?? []
    return (
      <aside className="side" aria-label="Details">
        <h2>{nameOf(edge.source)} → {nameOf(edge.target)}</h2>
        <p className="summary">
          {isSchema ? `${edge.pairs!.length} table flows between these schemas` : edge.kind === 'binds' ? 'semantic table built on this table' : 'data flows through the jobs below'}
          {' · '}<span className="tag">{edge.conf}</span>{edge.config ? ' · config-resolved' : ''}
        </p>
        {isSchema ? (
          <>
            <h3>Table flows ({edge.pairs!.length})</h3>
            <table className="members">
              <tbody>
                {edge.pairs!.slice(0, 30).map(([a, b]) => (
                  <tr key={`${a}|${b}`}>
                    <td>{nameOf(a)}</td><td>→</td>
                    <td><button className="btn" onClick={() => onFocus(b)}>{nameOf(b)}</button></td>
                  </tr>
                ))}
              </tbody>
            </table>
            {edge.pairs!.length > 30 ? <p className="summary">…and {edge.pairs!.length - 30} more</p> : null}
          </>
        ) : null}
        {jobs.length > 0 ? (
          <>
            <h3>Jobs moving the data ({jobs.length})</h3>
            <p className="summary">
              A job that reads several tables and writes several cannot say which input feeds which output,
              so each pair is attributed to it.
            </p>
            <table className="members">
              <thead><tr><th>Job</th><th>Liveness</th><th>Last change</th></tr></thead>
              <tbody>
                {jobs.slice(0, 40).map((j) => (
                  <tr key={j.id}>
                    <td><button className="btn" title="Show the tables this job touches" onClick={() => onOpenJob(j.id)}>{j.name}</button>
                      {j.basis === 'columns' ? <span className="tag" title="This job reads and writes too many tables to pair them all; this link comes from its column lineage. Tables it uses only in joins or filters are not linked.">by columns</span> : null}</td>
                    <td>{j.liveness}</td>
                    <td>{j.lastChange}{j.stale ? ' (stale)' : ''}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {jobs.length > 40 ? <p className="summary">…and {jobs.length - 40} more</p> : null}
          </>
        ) : null}
      </aside>
    )
  }

  const schemaNode = selectedId ? view.nodes.find((n) => n.id === selectedId && n.type === 'schema') : undefined
  if (schemaNode?.tableIds) {
    return (
      <aside className="side" aria-label="Details">
        <h2>{schemaNode.label}</h2>
        <p className="summary">Tables with lineage in this schema, busiest first. Pick one to see its flows.</p>
        <div className="row-actions" style={{ flexDirection: 'column', alignItems: 'stretch' }}>
          {schemaNode.tableIds.slice(0, 40).map((id) => (
            <button key={id} className="btn" onClick={() => onFocus(id)}>{nameOf(id)}</button>
          ))}
        </div>
        {schemaNode.tableIds.length > 40 ? <p className="summary">…and {schemaNode.tableIds.length - 40} more (use search)</p> : null}
      </aside>
    )
  }

  const selected = selectedId ? fg.nodes.get(selectedId) : null
  if (selected) {
    return (
      <aside className="side" aria-label="Details">
        <p className="eyebrow">
          {selected.type === 'semantic_table' ? 'Semantic table' : selected.type === 'unknown' ? 'Unresolved name' : 'Table'}
        </p>
        <h2>{selected.name}</h2>
        {selected.type === 'unknown' || selected.meta.unqualified || selected.meta.external ? (
          <p className="summary">
            {selected.type === 'unknown' ? 'A name built at run time; the extractor could not resolve it. ' : ''}
            {selected.meta.unqualified ? 'Unqualified name: may be a temp view or a default-schema table. ' : ''}
            {selected.meta.external ? 'A table in an external source system.' : ''}
          </p>
        ) : null}
        {selected.meta.inLakehouse === false ? (
          <p className="warn-note" role="note"><strong>Not in the lakehouse.</strong> The code uses this table, but the schema
            export does not list it: renamed or dropped, made and dropped within a run, or created after the export.</p>
        ) : selected.meta.inLakehouse ? <p className="summary">In the lakehouse (schema export).</p> : null}
        {selected.meta.configWarning ? (
          <p className="warn-note" role="note"><strong>Config row:</strong> {selected.meta.configWarning}</p>
        ) : null}
        <div className="row-actions">
          {selected.id !== focusId || mode !== 'tables' ? <button className="btn primary" onClick={() => onFocus(selected.id)}>Show its lineage</button> : null}
          {selected.type !== 'unknown' ? <button className="btn" onClick={() => onImpact(selected.id)}>Impact report</button> : null}
          {selected.id !== focusId ? (
            <button className="btn ghost" title="Add this table to the exclusion list (Filters)" onClick={() => onHide(selected.name)}>Hide</button>
          ) : null}
        </div>
        {cg ? <ColumnsSection key={`c-${selected.id}`} cg={cg} tableId={selected.id} onPickColumn={onPickColumn} /> : null}
        <UsedBy key={selected.id} fg={fg} tableId={selected.id} onOpenJob={onOpenJob} onFocus={onFocus} />
      </aside>
    )
  }

  // nothing selected
  if (view.job) {
    const ins = view.nodes.filter((n) => n.depth === 0)
    const outs = view.nodes.filter((n) => n.depth === 1)
    return (
      <aside className="side" aria-label="Details">
        <h2>{view.job.name}</h2>
        <JobFacts fg={fg} job={view.job} />
        <h3>Reads ({ins.length})</h3>
        <div className="row-actions">{ins.map((n) => <button key={n.id} className="btn" onClick={() => onFocus(n.id)}>{n.label}</button>)}</div>
        <h3>Writes ({outs.length})</h3>
        <div className="row-actions">{outs.map((n) => <button key={n.id} className="btn" onClick={() => onFocus(n.id)}>{n.label}</button>)}</div>
        <p className="summary">Tables both read and written by the job are listed under Reads.</p>
      </aside>
    )
  }

  return <Overview fg={fg} view={view} mode={mode} browsing={mode === 'tables' && !focusId} />
}

/** When each input was taken: an input much older than the extract makes the lineage older too. */
function Inputs({ fg }: { fg: FlowGraph }) {
  const inp = fg.data.meta.inputs
  if (!inp) return null
  const extracted = fg.data.meta.generatedAt ? Date.parse(fg.data.meta.generatedAt) : NaN
  const age = (iso?: string) => {
    if (!iso || Number.isNaN(extracted)) return null
    const days = Math.floor((extracted - Date.parse(iso)) / 86_400_000)
    return days > 7 ? <span className="badge stale" title="Taken this many days before the extract">{days} days older</span> : null
  }
  const rows: [string, string, string | undefined][] = []
  if (inp.repo) rows.push(['Repo', `${inp.repo.branch ?? 'HEAD'} @ ${inp.repo.commit.slice(0, 7)}, ${fmtDate(inp.repo.date)}`, inp.repo.date])
  if (inp.schema) rows.push(['Schema', `exported ${fmtDate(inp.schema.exportedAt)}`, inp.schema.exportedAt])
  for (const c of inp.configTables ?? []) {
    const via = c.source === 'onelake-delta' ? 'OneLake' : c.source === 'sql-endpoint-table' ? 'SQL endpoint' : c.source
    rows.push([c.table ?? 'config table', `exported ${fmtDate(c.exportedAt)}${via ? ` (${via})` : ''}`, c.exportedAt])
  }
  if (inp.dates) rows.push(['Last-change dates', `fetched ${fmtDate(inp.dates.fetchedAt)}`, inp.dates.fetchedAt])
  if (!rows.length) return null
  return (
    <>
      <h3>Inputs</h3>
      <dl className="inputs">
        {rows.map(([k, v, iso]) => <Fragment key={k}><dt>{k}</dt><dd>{v} {age(iso)}</dd></Fragment>)}
      </dl>
      <p className="summary">Refresh them all with <code>columntrace refresh</code>.</p>
    </>
  )
}

function Overview({ fg, view, mode, browsing }: { fg: FlowGraph; view: View; mode: Mode; browsing: boolean }) {
  const counts = useMemo(() => {
    const c = { table: 0, notebook: 0, pipeline: 0, semantic_table: 0, unknown: 0 }
    for (const n of fg.nodes.values()) c[n.type]++
    return c
  }, [fg])
  const cs = fg.data.columns?.stats
  const warnings = fg.data.warnings ?? []
  return (
    <aside className="side" aria-label="Details">
      <p className="eyebrow">How to read it</p>
      <h2>{browsing ? 'All tables' : mode === 'schemas' ? 'Schemas overview' : mode === 'columns' ? 'Column lineage' : 'Table lineage'}</h2>
      <ul className="howto">
        {browsing ? (
          <>
            <li>Every table in the file. Filter by name or schema, sort by how connected it is, or show only sources, tables nothing reads, or tables with column lineage.</li>
            <li>Click a table to see its lineage. Back (←) and Forward (→) step through what you looked at; so does the browser's back button.</li>
          </>
        ) : mode === 'schemas' ? (
          <li>Each box is a schema; a link counts the table flows between two schemas. Click a schema to list its tables.</li>
        ) : (
          <>
            <li>Sources are on the left, consumers on the right; the focus is outlined.</li>
            <li>Hover a {mode === 'columns' ? 'column' : 'table'} to light up its path. Click it for details, double-click to {mode === 'columns' ? 'trace from it' : 'focus on it'}.</li>
            {mode === 'columns'
              ? <li>A mark on a link shows how the column is computed: <b className="mk">f</b> an expression, <b className="mk">Σ</b> an aggregate. Hover it for the SQL.</li>
              : <li><b className="mk sq">+3</b> on a card opens that many more tables on that side. Click a link to see the jobs that move the data.</li>}
          </>
        )}
      </ul>
      {browsing ? null : <h3>In this graph</h3>}
      {browsing ? null : (
      <dl>
        <dt>{mode === 'schemas' ? 'Schemas' : 'Tables'}</dt><dd>{view.nodes.length}</dd>
        {mode === 'columns' ? <><dt>Columns</dt><dd>{view.nodes.reduce((a, n) => a + (n.columns?.length ?? 0), 0)}</dd></> : null}
        <dt>Links</dt><dd>{view.edges.length}</dd>
      </dl>
      )}
      <h3>In the file</h3>
      <dl>
        <dt>Tables</dt><dd>{counts.table.toLocaleString()}{counts.semantic_table ? ` + ${counts.semantic_table} semantic` : ''}</dd>
        <dt>Notebooks</dt><dd>{counts.notebook.toLocaleString()}</dd>
        <dt>Pipelines</dt><dd>{counts.pipeline.toLocaleString()}</dd>
        {lakehouse(fg) ? (
          <><dt>Not in lakehouse</dt><dd title="Extracted tables that the schema export does not list">
            {lakehouse(fg)!.missing.toLocaleString()} of {lakehouse(fg)!.checked.toLocaleString()} tables</dd></>
        ) : null}
        {setAside(fg) ? (
          <><dt>Not tables</dt><dd title="Names without a schema that nothing writes and the default schema does not have: CTEs, temp views or DuckDB relations. Listed on each notebook instead of drawn.">
            {setAside(fg)!.names.toLocaleString()} names in {Object.keys(setAside(fg)!.byItem).length} notebooks</dd></>
        ) : null}
        {cs ? <><dt>Column links</dt><dd>{(cs.edges ?? 0).toLocaleString()} from {cs.notebooksWithColumnEdges ?? '?'} notebooks</dd></> : null}
        <dt>Extracted</dt><dd>{fg.data.meta.generatedAt ? fmtDate(fg.data.meta.generatedAt) : 'unknown'}</dd>
      </dl>
      <Inputs fg={fg} />
      {warnings.length ? (
        <details>
          <summary>Extractor warnings ({warnings.length})</summary>
          <ul className="warnings">{warnings.slice(0, 50).map((w, i) => <li key={i}><strong>{w.item}</strong>: {w.message}</li>)}</ul>
          {warnings.length > 50 ? <p className="summary">…and {warnings.length - 50} more in lineage.json</p> : null}
        </details>
      ) : null}
    </aside>
  )
}
