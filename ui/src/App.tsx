import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import FilterPanel from './components/FilterPanel'
import ImpactModal from './components/ImpactModal'
import LineageCanvas from './components/LineageCanvas'
import SidePanel from './components/SidePanel'
import TableCatalog from './components/TableCatalog'
import { ColumnGraph, parseColId, type ColRef } from './lib/colgraph'
import { addPatterns, loadExclude, saveExclude } from './lib/exclude'
import { fixedDepth, parseTeamDefaults, perSide, stamp, teamExclude, teamFilters, type TeamDefaults } from './lib/teamDefaults'
import { FlowGraph } from './lib/flow'
import { HOME, navKey, parseHash, toHash, type Nav } from './lib/nav'
import type { Lineage } from './lib/types'
import {
  autoColumnDepth, autoDepth, buildAnyView, changedFilters, defaultFilters, matcherFor, type Expand, type Filters, type Mode,
} from './lib/view'

interface Source { name: string; sample: boolean }

function parseLineage(text: string): Lineage {
  const data = JSON.parse(text) as Lineage
  if (data?.meta?.schemaVersion !== 1 || !Array.isArray(data.nodes) || !Array.isArray(data.edges)) {
    throw new Error('Not a ColumnTrace lineage.json (schemaVersion 1 expected).')
  }
  return data
}

const isJob = (fg: FlowGraph | null, id: string | null) => {
  const t = fg && id ? fg.nodes.get(id)?.type : undefined
  return t === 'notebook' || t === 'pipeline'
}
const noExpand = (): Expand => ({ up: new Set(), down: new Set() })

const MODES: [Mode, string, string][] = [
  ['tables', 'Tables', 'A table, what flows into it and what it feeds. Jobs are folded into the links.'],
  ['columns', 'Columns', 'Trace one column through the SQL that computes it'],
  ['schemas', 'Schemas', 'Whole-warehouse overview: one node per schema'],
]

function Stepper({ label, value, onChange, disabled }: { label: string; value: number; onChange: (v: number) => void; disabled?: boolean }) {
  return (
    <span className={`stepper ${disabled ? 'off' : ''}`} role="group" aria-label={`${label} depth`}>
      <span className="stepper-label">{label}</span>
      <button className="btn icon" disabled={disabled || value <= 0} onClick={() => onChange(value - 1)} aria-label={`Fewer ${label} levels`}>−</button>
      <span className="stepper-value" aria-live="polite">{value}</span>
      <button className="btn icon" disabled={disabled || value >= 8} onClick={() => onChange(value + 1)} aria-label={`More ${label} levels`}>+</button>
    </span>
  )
}

export default function App() {
  const [data, setData] = useState<Lineage | null>(null)
  const [source, setSource] = useState<Source>({ name: 'sample (synthetic)', sample: true })
  const [error, setError] = useState<string | null>(null)
  const [focusId, setFocusId] = useState<string | null>(null)
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [selectedEdgeId, setSelectedEdgeId] = useState<string | null>(null)
  const [colFocus, setColFocus] = useState<ColRef | null>(null)
  const [filters, setFilters] = useState<Filters>(() => defaultFilters(loadExclude()))
  const [expand, setExpand] = useState<Expand>(noExpand)
  const [impactFor, setImpactFor] = useState<string | null>(null)
  const [showFilters, setShowFilters] = useState(false)
  const [loops, setLoops] = useState(0)            // loop links the canvas left out
  const [query, setQuery] = useState('')
  const [active, setActive] = useState(0)
  const [themeMode, setThemeMode] = useState<'auto' | 'light' | 'dark'>('auto')
  const searchRef = useRef<HTMLInputElement>(null)
  const fileRef = useRef<HTMLInputElement>(null)
  const filtersRef = useRef(filters)
  filtersRef.current = filters
  const lastTable = useRef<string | null>(null)   // where "Back" returns to from a job view
  // navigation history (kept in the URL hash): position, furthest position, last recorded place
  const [navIdx, setNavIdx] = useState(0)
  const maxIdx = useRef(0)
  const lastKey = useRef<string | null>(null)

  const fg = useMemo(() => (data ? new FlowGraph(data) : null), [data])
  const cg = useMemo(() => (data?.columns ? new ColumnGraph(data.columns) : null), [data])

  // team defaults (team-defaults.json next to the data): everyone's starting point
  const [team, setTeam] = useState<TeamDefaults>({})
  const teamRef = useRef(team)
  teamRef.current = team
  const teamBase = useMemo(() => teamFilters(team), [team])
  /** Starting depth for a table: the team's fixed depth, or auto sized per side. */
  const startDepth = useCallback((g: FlowGraph, id: string, f: Filters) =>
    fixedDepth(teamRef.current) ?? autoDepth(g, id, f, perSide(teamRef.current)), [])

  // nothing is saved until the team defaults are in: saving earlier would overwrite the viewer's own rules
  const teamReady = useRef(false)
  useEffect(() => { if (teamReady.current) saveExclude(filters.exclude, undefined, stamp(team)) }, [filters.exclude, team])

  /** Go to a place (from the URL or the history); returns where it actually landed. */
  const applyNav = useCallback((n: Nav, g: FlowGraph, c: ColumnGraph | null): Nav => {
    const f = filtersRef.current
    let focus = n.focus && g.nodes.has(n.focus) ? n.focus : null
    let col: ColRef | null = null
    let patch: Partial<Filters> = { mode: n.mode }
    if (n.mode === 'columns') {
      if (c && n.col && g.nodes.has(n.col.table)) {
        col = n.col
        focus = n.col.table
        patch = { mode: 'columns', ...autoColumnDepth(g, c, col, { ...f, mode: 'columns' }) }
      } else {
        patch = { mode: 'tables' }
      }
    } else if (n.mode === 'tables' && focus && !isJob(g, focus)) {
      patch = { mode: 'tables', ...startDepth(g, focus, { ...f, mode: 'tables' }) }
    }
    setFilters((x) => ({ ...x, ...patch }))
    setFocusId(focus)
    setColFocus(col)
    setExpand(noExpand())
    setSelectedId(null)
    setSelectedEdgeId(null)
    return { mode: patch.mode ?? n.mode, focus, col }
  }, [])

  const load = useCallback((d: Lineage, src: Source, fromUrl = false) => {
    const g = new FlowGraph(d)
    setData(d)
    setSource(src)
    setError(null)
    setImpactFor(null)
    setQuery('')
    const wanted = fromUrl ? parseHash(window.location.hash) : null
    if (wanted) {
      applyNav(wanted, g, d.columns ? new ColumnGraph(d.columns) : null)
      return
    }
    // no place in the URL: start on the busiest table
    const teamStart = teamRef.current.start ? `table:${teamRef.current.start}` : null
    const start = (teamStart && g.nodes.has(teamStart) ? teamStart : null) ?? g.hubs(1, matcherFor(filtersRef.current))[0]?.id ?? null
    applyNav(start ? { mode: 'tables', focus: start, col: null } : HOME, g, null)
  }, [applyNav])

  // every new place becomes a history entry, so Back / Forward (and the browser's) step through them
  const here: Nav = { mode: filters.mode, focus: focusId, col: filters.mode === 'columns' ? colFocus : null }
  const hereKey = navKey(here)
  const hereRef = useRef(here)
  hereRef.current = here
  useEffect(() => {
    if (!data || hereKey === lastKey.current) return
    const first = lastKey.current === null
    lastKey.current = hereKey
    const idx = first ? 0 : navIdx + 1
    const state = { nav: hereRef.current, idx }
    try {
      if (first) window.history.replaceState(state, '', toHash(hereRef.current))
      else window.history.pushState(state, '', toHash(hereRef.current))
    } catch {
      return                                  // a sandboxed frame may refuse history: Back stays off
    }
    setNavIdx(idx)
    maxIdx.current = idx
  }, [data, hereKey])   // navIdx is read, not watched: only a new place records an entry

  const graphs = useRef<{ fg: FlowGraph | null; cg: ColumnGraph | null }>({ fg: null, cg: null })
  graphs.current = { fg, cg }
  useEffect(() => {
    const onPop = (e: PopStateEvent) => {
      const g = graphs.current.fg
      if (!g) return
      const n: Nav = e.state?.nav ?? parseHash(window.location.hash) ?? HOME
      const landed = applyNav(n, g, graphs.current.cg)
      lastKey.current = navKey(landed)        // restoring a place records nothing
      setNavIdx(typeof e.state?.idx === 'number' ? e.state.idx : 0)
    }
    window.addEventListener('popstate', onPop)
    return () => window.removeEventListener('popstate', onPop)
  }, [applyNav])

  useEffect(() => {
    // a single-page build may carry its data inline (scripts/single-page.mjs --embed): a page opened
    // from disk cannot fetch a file next to it
    // team defaults first: embedded in the page, or team-defaults.json next to the data file
    const applyTeam = (raw: unknown) => {
      const { defaults, problems } = parseTeamDefaults(raw)
      if (problems.length) setError(`team-defaults.json: ${problems.join('; ')}`)
      teamRef.current = defaults
      setTeam(defaults)
      const f = teamFilters(defaults)
      f.exclude = loadExclude(undefined, teamExclude(defaults), stamp(defaults))
      filtersRef.current = f
      setFilters(f)
      teamReady.current = true
    }
    const embeddedTeam = document.getElementById('columntrace-defaults')
    if (embeddedTeam?.textContent) {
      try { applyTeam(JSON.parse(embeddedTeam.textContent)) } catch { setError('The team defaults embedded in this page cannot be read.') }
    }
    const embedded = document.getElementById('columntrace-data')
    if (embedded?.textContent) {
      teamReady.current = true
      try {
        const label = embedded.dataset.label
        load(parseLineage(embedded.textContent), { name: label || 'embedded lineage', sample: embedded.dataset.sample === 'true' }, true)
      } catch (e) {
        setError(`The lineage embedded in this page cannot be read: ${(e as Error).message}`)
      }
      return
    }
    // ?data=<url> opens a lineage file served by the dev server instead of the sample
    // a build can ship its own file instead of the synthetic sample (VITE_DATA_FILE, e.g. for a shared page)
    const bundled = import.meta.env.VITE_DATA_FILE as string | undefined
    const custom = new URLSearchParams(window.location.search).get('data') ?? (bundled ? `${import.meta.env.BASE_URL}${bundled}` : null)
    const url = custom ?? `${import.meta.env.BASE_URL}sample-lineage.json`
    const src: Source = custom
      ? { name: (import.meta.env.VITE_DATA_LABEL as string | undefined) ?? decodeURIComponent(custom.split('/').pop() ?? 'lineage.json'), sample: false }
      : { name: 'sample (synthetic)', sample: true }
    const teamUrl = custom && !embeddedTeam ? `${url.slice(0, url.lastIndexOf('/') + 1)}team-defaults.json` : null
    const teamLoad = teamUrl
      ? fetch(teamUrl, { cache: 'no-cache' }).then((r) => (r.ok ? r.json() : null)).then((j) => { if (j) applyTeam(j) }).catch(() => undefined)
      : Promise.resolve()
    void teamLoad.then(() => { teamReady.current = true })
    teamLoad
      .then(() => fetch(url, { cache: 'no-cache' }))    // a re-extracted file shows up on reload
      .then((r) => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.text() })
      .then((t) => load(parseLineage(t), src, true))
      .catch((e) => setError(`Could not load ${custom ? 'the requested file' : 'the bundled sample'}: ${e.message}`))
  }, [load])

  const openFile = useCallback(async (file: File) => {
    try {
      load(parseLineage(await file.text()), { name: file.name, sample: false })
    } catch (e) {
      setError((e as Error).message)
    }
  }, [load])

  useEffect(() => {
    const over = (e: DragEvent) => e.preventDefault()
    const drop = (e: DragEvent) => {
      e.preventDefault()
      const f = e.dataTransfer?.files?.[0]
      if (f) void openFile(f)
    }
    const key = (e: KeyboardEvent) => {
      const tag = document.activeElement?.tagName
      if (e.key === '/' && tag !== 'INPUT' && tag !== 'SELECT' && tag !== 'TEXTAREA') { e.preventDefault(); searchRef.current?.focus() }
    }
    window.addEventListener('dragover', over)
    window.addEventListener('drop', drop)
    window.addEventListener('keydown', key)
    return () => {
      window.removeEventListener('dragover', over)
      window.removeEventListener('drop', drop)
      window.removeEventListener('keydown', key)
    }
  }, [openFile])

  useEffect(() => {
    const root = document.documentElement
    if (themeMode === 'auto') root.removeAttribute('data-theme')
    else root.setAttribute('data-theme', themeMode)
  }, [themeMode])

  const view = useMemo(
    () => (fg ? buildAnyView(fg, focusId, filters, { graph: cg, focus: colFocus }, expand) : null),
    [fg, focusId, filters, cg, colFocus, expand],
  )
  type Result = { kind: 'node'; id: string; label: string; tag: string; live?: string } | { kind: 'col'; ref: ColRef; label: string; traced: boolean }
  const results = useMemo<Result[]>(() => {
    if (!fg) return []
    const nodes: Result[] = fg.search(query).map((n) => ({
      kind: 'node', id: n.id, label: n.name, tag: n.type === 'semantic_table' ? 'semantic' : n.type, live: n.meta.liveness,
    }))
    const cols: Result[] = (cg?.search(query, (t) => fg.nodes.get(t)?.name ?? t, 6) ?? []).map((ref) => ({
      kind: 'col', ref, label: `${fg.nodes.get(ref.table)?.name ?? ref.table}.${ref.column}`,
      traced: !!cg && (({ sources, consumers }) => sources + consumers > 0)(cg.counts(ref.table, ref.column)),
    }))
    return [...nodes.slice(0, 8), ...cols]
  }, [fg, cg, query])
  const hubs = useMemo(() => (fg ? fg.hubs(8, matcherFor(filters)) : []), [fg, filters])
  const selectedEdge = useMemo(
    () => (view && selectedEdgeId ? view.edges.find((e) => e.id === selectedEdgeId) ?? null : null),
    [view, selectedEdgeId],
  )

  // a selection that the current filters, exclusions or mode removed from the graph is cleared
  useEffect(() => {
    if (!view) return
    const present = (id: string) => view.nodes.some((n) => n.id === id || n.columns?.some((c) => c.id === id))
    if (selectedId && !present(selectedId)) setSelectedId(null)
    if (selectedEdgeId && !view.edges.some((e) => e.id === selectedEdgeId)) setSelectedEdgeId(null)
  }, [view, selectedId, selectedEdgeId])

  /** Focus a table (auto depth) or a job (the tables it touches). */
  const pick = (id: string) => {
    if (focusId && fg && !isJob(fg, focusId)) lastTable.current = focusId
    if (fg && !isJob(fg, id)) {
      setFilters((f) => ({ ...f, mode: 'tables', ...startDepth(fg, id, { ...f, mode: 'tables' }) }))
    } else {
      setFilters((f) => ({ ...f, mode: 'tables' }))
    }
    setFocusId(id)
    setExpand(noExpand())
    setSelectedId(null)
    setSelectedEdgeId(null)
    setQuery('')
    setActive(0)
  }

  /** Trace one column: switches to the columns view with a depth that keeps it readable. */
  const pickColumn = (table: string, column: string) => {
    if (!fg || !cg) return
    const ref: ColRef = { table, column }
    setFilters((f) => ({ ...f, mode: 'columns', ...autoColumnDepth(fg, cg, ref, { ...f, mode: 'columns' }) }))
    setColFocus(ref)
    setFocusId(table)
    setSelectedId(null)
    setSelectedEdgeId(null)
  }
  const onGraphFocus = (id: string) => {
    if (id.includes('#')) {
      const r = parseColId(id)
      pickColumn(r.table, r.column)
    } else pick(id)
  }
  const onExpand = (id: string, dir: 'up' | 'down') => {
    setExpand((x) => ({ ...x, [dir]: new Set(x[dir]).add(id) }))
  }

  const selectNode = (id: string | null) => { setSelectedId(id); setSelectedEdgeId(null) }
  const selectEdge = (id: string) => { setSelectedEdgeId(id); setSelectedId(null) }
  const setMode = (mode: Mode) => {
    if (mode === 'columns' && fg && cg) {
      // entering the column view without a column: start from the busiest traced column of the
      // table in focus, else the busiest one anywhere (exclusions respected)
      const skip = (t: string) => matcherFor(filtersRef.current)(fg.nodes.get(t)?.name ?? t)
      const start = (colFocus && colFocus.table === focusId ? colFocus : null)
        ?? cg.topColumns(1, skip, focusId ?? undefined)[0]
        ?? colFocus
        ?? cg.topColumns(1, skip)[0]
      if (start) { pickColumn(start.table, start.column); return }
    }
    if (mode === 'tables' && fg && focusId && !isJob(fg, focusId)) {
      setFilters((f) => ({ ...f, mode, ...startDepth(fg, focusId, { ...f, mode }) }))
    } else {
      setFilters((f) => ({ ...f, mode }))
    }
    setExpand(noExpand())
    setSelectedId(null)
    setSelectedEdgeId(null)
  }
  const pickResult = (r: Result) => {
    if (r.kind === 'col') pickColumn(r.ref.table, r.ref.column)
    else pick(r.id)
    setQuery('')
    setActive(0)
  }
  const set = (patch: Partial<Filters>) => setFilters((f) => ({ ...f, ...patch }))
  const hideTable = (name: string) => setFilters((f) => ({ ...f, exclude: addPatterns(f.exclude, name) }))

  const focusNode = focusId && fg ? fg.nodes.get(focusId) : null
  const jobLens = !!view?.job
  const nRules = filters.exclude.rules.filter((r) => r.on).length
  const nChanged = changedFilters(filters, teamBase)

  // what is not drawn, in one line with the reasons on hover, so nothing is dropped silently
  const hiddenParts: { text: string; tip: string }[] = []
  if (view && (view.hiddenExcluded ?? 0) > 0) {
    hiddenParts.push({ text: `${view.hiddenExcluded} by name`, tip: `${view.hiddenExcluded} table(s) match the ${nRules} exclusion rule(s) under Filters.` })
  }
  if (view && (view.skippedJobs ?? 0) > 0 && filters.mode !== 'columns') {
    hiddenParts.push({ text: `${view.skippedJobs} broad jobs`, tip: `${view.skippedJobs} job(s) read and write too many tables to say which feeds which (generic loaders, utilities). Raise the limit under Filters to include them.` })
  }
  if (view && (view.hiddenUnresolved ?? 0) > 0) {
    hiddenParts.push({ text: `${view.hiddenUnresolved} unresolved`, tip: `${view.hiddenUnresolved} table name(s) are built at run time. Turn on “Unresolved names” under Filters to see them.` })
  }
  const side = (view?.sideLinks ?? 0) + (view?.nodes.length ? loops : 0)
  if (side > 0) {
    hiddenParts.push({ text: `${side} side links`, tip: `${side} link(s) between tables on the graph do not run along a path through the focus: an upstream table that also feeds a downstream one, or a loop between two tables. Focus on either table to see them.` })
  }
  const banners: { key: string; text: string }[] = []
  if (error) banners.push({ key: 'err', text: error })
  if (view?.truncated) banners.push({ key: 'trunc', text: `Showing the ${view.nodes.length} items closest to the focus; ${view.hidden} more are hidden. Lower the depth or add filters.` })
  if (view && view.hiddenEdges > 0) banners.push({ key: 'edges', text: `${view.hiddenEdges} weaker links are hidden to keep the graph fast (config-resolved and inferred first).` })

  const browsing = filters.mode === 'tables' && !focusId     // the "All tables" page
  const goHome = () => {
    setFilters((f) => ({ ...f, mode: 'tables' }))
    setFocusId(null)
    setColFocus(null)
    setExpand(noExpand())
    setSelectedId(null)
    setSelectedEdgeId(null)
  }
  const focusLabel = browsing ? 'All tables' : filters.mode === 'schemas' ? 'All schemas'
    : filters.mode === 'columns' && colFocus ? `${fg?.nodes.get(colFocus.table)?.name ?? colFocus.table}.${colFocus.column}`
      : focusNode?.name ?? 'nothing yet'
  const focusKind = browsing ? 'Browse' : filters.mode === 'schemas' ? 'Overview' : filters.mode === 'columns' ? 'Column' : jobLens ? (focusNode?.type === 'pipeline' ? 'Pipeline' : 'Notebook') : 'Table'

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand"><span className="logo" aria-hidden="true" />ColumnTrace<small>lineage for Microsoft Fabric</small></div>
        <div className="search">
          <input
            ref={searchRef} type="search" placeholder="Search tables, columns, notebooks, pipelines   /"
            aria-label="Search" aria-controls="search-results" aria-expanded={results.length > 0}
            value={query}
            onChange={(e) => { setQuery(e.target.value); setActive(0) }}
            onKeyDown={(e) => {
              if (e.key === 'ArrowDown') { e.preventDefault(); setActive((a) => Math.min(a + 1, results.length - 1)) }
              else if (e.key === 'ArrowUp') { e.preventDefault(); setActive((a) => Math.max(a - 1, 0)) }
              else if (e.key === 'Enter' && results[active]) pickResult(results[active])
              else if (e.key === 'Escape') setQuery('')
            }}
          />
          {results.length > 0 ? (
            <ul className="results" id="search-results" role="listbox">
              {results.map((r, i) => (
                <li key={r.kind === 'col' ? `${r.ref.table}#${r.ref.column}` : r.id} role="option" aria-selected={i === active}
                    onMouseDown={() => pickResult(r)}>
                  <span className="tag">{r.kind === 'col' ? 'column' : r.tag}</span><span className="res-name">{r.label}</span>
                  {r.kind === 'node' && r.live ? <span className={`badge ${r.live}`}>{r.live}</span> : null}
                  {r.kind === 'col' && !r.traced ? <span className="tag" title="No column lineage recorded for this column">no lineage</span> : null}
                </li>
              ))}
            </ul>
          ) : null}
        </div>
        <nav className="tabs" aria-label="View">
          {MODES.map(([m, label, tip]) => (
            <button key={m} className={filters.mode === m ? 'on' : ''} title={m === 'columns' && !cg ? 'No column data: extract with --schema' : tip}
                    disabled={m === 'columns' && !cg} aria-pressed={filters.mode === m} onClick={() => setMode(m)}>{label}</button>
          ))}
        </nav>
        <span className="spacer" />
        <span className={`provenance ${source.sample ? '' : 'private'}`} title="Files are read in your browser and never uploaded">
          {source.sample ? 'Synthetic sample' : source.name}
          {data ? ` · ${data.nodes.length.toLocaleString()} nodes` : ''}
        </span>
        <input ref={fileRef} type="file" accept=".json,application/json" hidden
               onChange={(e) => { const f = e.target.files?.[0]; if (f) void openFile(f); e.target.value = '' }} />
        <button className="btn" onClick={() => fileRef.current?.click()} title="Open a lineage.json made by columntrace extract (or drop it on the page)">Open…</button>
        <button className="btn icon" aria-label={`Theme: ${themeMode}`} title={`Theme: ${themeMode}`}
                onClick={() => setThemeMode((m) => (m === 'auto' ? 'dark' : m === 'dark' ? 'light' : 'auto'))}>
          {themeMode === 'dark' ? '☾' : themeMode === 'light' ? '☀' : '◐'}
        </button>
      </header>

      <section className="context" aria-label="Focus and depth">
        <div className="history" role="group" aria-label="History">
          <button className="btn icon" disabled={navIdx <= 0} onClick={() => window.history.back()} title="Back (Alt+←)" aria-label="Back">←</button>
          <button className="btn icon" disabled={navIdx >= maxIdx.current} onClick={() => window.history.forward()} title="Forward (Alt+→)" aria-label="Forward">→</button>
          <button className={`btn small ${browsing ? 'primary' : ''}`} onClick={goHome} aria-pressed={browsing} title="Browse every table, by schema, use and column lineage">All tables</button>
        </div>
        <div className="crumb">
          <span className="crumb-kind">{focusKind}</span>
          <strong className="crumb-name" title={focusLabel}>{focusLabel}</strong>
          {focusNode && focusNode.type !== 'unknown' && filters.mode !== 'schemas' && !jobLens ? (
            <button className="btn small" onClick={() => setImpactFor(focusNode.id)} title="What breaks if this table changes, and what it depends on">Impact report</button>
          ) : null}
        </div>
        {filters.mode !== 'schemas' && !jobLens && !browsing ? (
          <div className="depth" title={filters.mode === 'columns' ? 'One level = one statement that computes a column' : 'One level = one job between two tables'}>
            <Stepper label="upstream" value={filters.up} onChange={(up) => { set({ up }); setExpand(noExpand()) }} />
            <Stepper label="downstream" value={filters.down} onChange={(down) => { set({ down }); setExpand(noExpand()) }} />
          </div>
        ) : null}
        <span className="spacer" />
        {hiddenParts.length ? (
          <button className="hidden-chip" onClick={() => setShowFilters(true)} data-filters-toggle=""
                  title={hiddenParts.map((p) => p.tip).join('\n')}>
            Not drawn: {hiddenParts.map((p) => p.text).join(' · ')}
          </button>
        ) : null}
        <button className={`btn ${showFilters ? 'primary' : ''}`} data-filters-toggle="" aria-expanded={showFilters}
                onClick={() => setShowFilters((s) => !s)}>
          Filters{nChanged + nRules ? <span className="count-badge" title={`${nRules} exclusion rule(s) on, ${nChanged} other setting(s) changed`}>{nChanged + nRules}</span> : null}
        </button>
      </section>

      <div className="main">
        <div className="canvas">
          {banners.length > 0 ? (
            <div className="banners">
              {banners.map((b) => <div key={b.key} className="banner" role="status">{b.text}</div>)}
            </div>
          ) : null}
          {jobLens && view?.job ? (
            <div className="lens-note" role="status">
              Tables read and written by <strong>{view.job.name}</strong>.{' '}
              {lastTable.current && fg?.nodes.has(lastTable.current) ? (
                <button className="link" onClick={() => pick(lastTable.current!)}>Back to {fg.nodes.get(lastTable.current)!.name}</button>
              ) : 'Pick a table to leave the job view.'}
            </div>
          ) : null}
          {browsing && fg ? (
            <TableCatalog fg={fg} cg={cg} filters={filters} onPick={pick} />
          ) : view && view.nodes.length > 0 ? (
            <LineageCanvas view={view} mode={filters.mode} selectedId={selectedId} selectedEdgeId={selectedEdgeId}
                           onSelect={selectNode} onSelectEdge={selectEdge} onFocus={onGraphFocus} onLoops={setLoops}
                           onExpand={filters.mode === 'tables' && !jobLens ? onExpand : undefined} />
          ) : (
            <div className="empty">
              <div><strong>{data ? 'Nothing to show for this focus and these filters.' : error ? 'No data loaded.' : 'Loading…'}</strong></div>
              {fg && filters.mode === 'columns' && cg ? (
                <>
                  <div>Pick a column to trace (or search “table.column” above):</div>
                  <div className="suggest">
                    {cg.topColumns(8, (t) => matcherFor(filters)(fg.nodes.get(t)?.name ?? t)).map((r) => (
                      <button key={`${r.table}#${r.column}`} className="btn" onClick={() => pickColumn(r.table, r.column)}>
                        {fg.nodes.get(r.table)?.name ?? r.table}.{r.column}
                      </button>
                    ))}
                  </div>
                </>
              ) : fg ? (
                <>
                  <div>Search above, or start from a busy table:</div>
                  <div className="suggest">
                    {hubs.map((n) => <button key={n.id} className="btn" onClick={() => pick(n.id)}>{n.name}</button>)}
                  </div>
                </>
              ) : null}
            </div>
          )}
          <div className="legend" aria-label="Legend" hidden={browsing}>
            <span><i className="ln c-parsed" />parsed</span>
            <span><i className="ln c-inferred" />inferred</span>
            <span><i className="ln c-unresolved" />unresolved</span>
            {filters.mode === 'columns' ? (
              <span><b className="mk">f</b>expression <b className="mk">Σ</b>aggregate · no mark = copied</span>
            ) : filters.mode === 'tables' ? (
              <span><b className="mk sq">+3</b>more to open</span>
            ) : null}
            <span className="legend-help">hover to trace · click for details · double-click to focus</span>
          </div>
          {showFilters && fg ? <FilterPanel fg={fg} filters={filters} base={teamBase} teamDefaults={Object.keys(team).length > 0} onChange={set} onClose={() => setShowFilters(false)} /> : null}
        </div>
        {fg && view ? (
          <SidePanel fg={fg} view={view} mode={filters.mode} cg={cg} colFocus={colFocus} selectedId={selectedId}
                     edge={selectedEdge} focusId={focusId} onFocus={pick} onPickColumn={pickColumn}
                     onOpenJob={pick} onImpact={setImpactFor} onHide={hideTable} />
        ) : <aside className="side" />}
      </div>

      {fg && impactFor ? <ImpactModal fg={fg} focusId={impactFor} onClose={() => setImpactFor(null)} /> : null}
    </div>
  )
}
