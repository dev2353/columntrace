// Builds what is drawn. Only data is drawn (tables, semantic tables, schemas); notebooks and
// pipelines are folded into edges, or shown as a list in the side panel, never as nodes.
//
//   tables   lineage of a focus table: what flows into it (left) and out of it (right)
//   columns  lineage of one column; tables are cards listing the traced columns
//   schemas  whole-warehouse overview, one node per schema
//   job lens the tables one notebook or pipeline reads and writes (picked from "Used by")
//
// Around a focus only edges on a path through the focus are drawn: upstream tables feeding
// each other or the focus, and the focus feeding downstream tables. Edges that bypass the
// focus (an upstream table that also feeds a downstream one) or run back into it are left
// out and counted, which keeps the picture a clean left-to-right flow.

import { CONF_ORDER, FlowGraph } from './flow'
import { activePatterns, compileSettings, defaultExclude, type ExcludeSettings, type Matcher } from './exclude'
import { colId, confOf, parseColId, type ColRef, type ColumnGraph } from './colgraph'
import { contract, schemaOf, type Contracted, type Via } from './tablegraph'
import type { Conf, ColumnEdge, LNode, Liveness } from './types'

export type Mode = 'tables' | 'schemas' | 'columns'
export type TableType = 'table' | 'semantic_table' | 'unknown'
export type Side = 'up' | 'down' | 'focus'

export interface Filters {
  mode: Mode
  conf: Set<Conf>
  showConfig: boolean
  liveness: Set<Liveness>      // applies to the jobs that move the data
  staleOnly: boolean
  types: Set<TableType>
  exclude: ExcludeSettings
  up: number
  down: number
  maxNodes: number
  maxEdges: number
  maxPairs: number             // skip jobs with more input x output pairs than this
}

/** Tables opened one step further than the depth allows (the +N buttons on a card). */
export interface Expand {
  up: Set<string>
  down: Set<string>
}

export interface ViewColumn {
  id: string                   // column id: table id # column
  name: string
  focus?: boolean
  side?: Side
}

export interface ViewNode {
  id: string
  label: string
  type: TableType | 'schema'
  focus?: boolean
  tableIds?: string[]          // schema nodes: the tables inside, busiest first
  depth: number
  side?: Side
  more?: { up: number; down: number }   // neighbors not drawn yet (tables mode)
  columns?: ViewColumn[]       // columns mode: the traced columns of this table
  otherColumns?: number        // columns mode: columns of the table that are not drawn
  warning?: string             // e.g. its config row's query was cut by the SQL endpoint
  missing?: boolean            // the code uses it, but the schema export does not list it
}

export interface ViewEdge {
  id: string
  source: string               // node ids
  target: string
  sourceCol?: string           // columns mode: the column ids at either end
  targetCol?: string
  kind: 'flows' | 'binds'
  conf: Conf
  config: boolean
  label?: string
  via?: Via[]                  // jobs that move the data
  pairs?: [string, string][]   // schema edges: the table-to-table flows they summarize
  columnEdges?: ColumnEdge[]   // columns mode: the SQL that derives the target column
}

export interface View {
  nodes: ViewNode[]
  edges: ViewEdge[]
  anchor?: string              // node the layout centers on
  truncated: boolean
  hidden: number               // nodes dropped by the maxNodes cap
  hiddenEdges: number          // weakest edges dropped by the maxEdges cap
  sideLinks?: number           // edges that bypass the focus or run back into it (not drawn)
  skippedJobs?: number         // jobs too broad to attribute pairwise
  hiddenUnresolved?: number    // unresolved names left out (their type is switched off)
  hiddenExcluded?: number      // tables left out by the name exclusions
  job?: LNode                  // set for the job lens
}

export const ALL_LIVENESS: Liveness[] = [
  'scheduled', 'orchestrated', 'called', 'entry-point', 'feeds-others', 'dead-end', 'uncalled',
]
/** Jobs that something runs: on a schedule, from a pipeline, or from another notebook. */
export const ACTIVE_LIVENESS: Liveness[] = ['scheduled', 'orchestrated', 'called']

export function defaultFilters(exclude: ExcludeSettings = defaultExclude()): Filters {
  return {
    mode: 'tables',
    conf: new Set<Conf>(['parsed', 'inferred', 'unresolved']),
    showConfig: false,
    liveness: new Set<Liveness>(ALL_LIVENESS),
    staleOnly: false,
    // unresolved names (built at run time) are hidden by default and counted
    types: new Set<TableType>(['table', 'semantic_table']),
    exclude,
    up: 2,
    down: 2,
    maxNodes: 300,
    maxEdges: 700,
    maxPairs: 24,
  }
}

/** How many settings differ from the defaults (the badge on the Filters button). */
export function changedFilters(f: Filters, base: Filters = defaultFilters()): number {
  const d = base
  const sameSet = <T>(a: Set<T>, b: Set<T>) => a.size === b.size && [...a].every((x) => b.has(x))
  return [
    !sameSet(f.conf, d.conf), f.showConfig !== d.showConfig, !sameSet(f.liveness, d.liveness),
    f.staleOnly !== d.staleOnly, !sameSet(f.types, d.types), f.maxPairs !== d.maxPairs,
  ].filter(Boolean).length
}

const EMPTY: View = { nodes: [], edges: [], truncated: false, hidden: 0, hiddenEdges: 0 }
const NO_EXPAND: Expand = { up: new Set(), down: new Set() }

const matcherCache = new Map<string, Matcher>()
export function matcherFor(f: Filters): Matcher {
  const key = [...activePatterns(f.exclude), '\u0001', ...(f.exclude.keep ?? [])].join('\u0000')
  let m = matcherCache.get(key)
  if (!m) {
    if (matcherCache.size > 50) matcherCache.clear()
    matcherCache.set(key, (m = compileSettings(f.exclude)))
  }
  return m
}

/** Keeps an edge only if it lies on a path through the focus. */
function onFocusPath(a: Side | undefined, b: Side | undefined): boolean {
  return (a === 'up' && (b === 'up' || b === 'focus')) || ((a === 'focus' || a === 'down') && b === 'down')
}

// ---------------------------------------------------------------------------------------
// job folding

const contractCache = new WeakMap<FlowGraph, Map<string, Contracted>>()

/** Jobs are filtered by liveness and staleness only. */
function jobPasses(n: LNode, f: Filters): boolean {
  const live = n.meta.liveness
  if (live && !f.liveness.has(live)) return false
  return !(f.staleOnly && !n.meta.stale)
}

export function contractFor(fg: FlowGraph, f: Filters): Contracted {
  const key = JSON.stringify([[...f.conf].sort(), f.showConfig, [...f.liveness].sort(), f.staleOnly, f.maxPairs])
  let perGraph = contractCache.get(fg)
  if (!perGraph) contractCache.set(fg, (perGraph = new Map()))
  let c = perGraph.get(key)
  if (!c) {
    c = contract(fg, {
      allowItem: (n) => jobPasses(n, f), allowConf: (x) => f.conf.has(x), includeConfig: f.showConfig,
      maxPairs: f.maxPairs,
    })
    perGraph.set(key, c)
  }
  return c
}

const shortName = (s: string, n = 30) => (s.length > n ? `${s.slice(0, n - 1)}…` : s)
const edgeRank = (e: { config: boolean; conf: Conf }) => (e.config ? 10 : 0) + CONF_ORDER[e.conf]

function capEdges(list: ViewEdge[], max: number): { edges: ViewEdge[]; hidden: number } {
  if (list.length <= max) return { edges: list, hidden: 0 }
  const sorted = [...list].sort((a, b) => edgeRank(a) - edgeRank(b) || a.id.localeCompare(b.id))
  return { edges: sorted.slice(0, max), hidden: list.length - max }
}

const isJob = (n: LNode | undefined): n is LNode => !!n && (n.type === 'notebook' || n.type === 'pipeline')
const isDataType = (t: LNode['type']): t is TableType => t === 'table' || t === 'semantic_table' || t === 'unknown'

const viaOf = (n: LNode, conf: Conf): Via => ({
  id: n.id, name: n.name, type: n.type as 'notebook' | 'pipeline', liveness: n.meta.liveness ?? '',
  stale: !!n.meta.stale, lastChange: n.meta.lastChange ? n.meta.lastChange.slice(0, 10) : '', conf,
})

// ---------------------------------------------------------------------------------------
// tables mode

export function buildTableView(fg: FlowGraph, focusId: string, f: Filters, expand: Expand = NO_EXPAND): View {
  const focus = fg.nodes.get(focusId)
  if (!focus || !isDataType(focus.type)) return EMPTY
  const tg = contractFor(fg, f)
  const excluded = matcherFor(f)
  const hiddenUnknown = new Set<string>()
  const hiddenByName = new Set<string>()

  /** May this table be drawn? (records why not, so nothing is dropped silently) */
  const admit = (id: string, record: boolean): boolean => {
    const node = fg.nodes.get(id)
    if (!node || !isDataType(node.type)) return false
    if (!f.types.has(node.type)) {
      if (record && node.type === 'unknown') hiddenUnknown.add(id)
      return false
    }
    if (excluded(node.name)) {
      if (record) hiddenByName.add(id)
      return false
    }
    return true
  }
  type TE = typeof tg.edges[number]
  const walk = (adj: Map<string, TE[]>, next: (e: TE) => string, max: number, open: Set<string>, skip?: Map<string, number>) => {
    const dist = new Map<string, number>([[focusId, 0]])
    const queue = [focusId]
    for (let i = 0; i < queue.length; i++) {
      const id = queue[i]
      const d = dist.get(id)!
      if (d >= max && !open.has(id)) continue
      for (const e of adj.get(id) ?? []) {
        const n = next(e)
        if (dist.has(n) || skip?.has(n) || !admit(n, true)) continue
        dist.set(n, d + 1)
        queue.push(n)
      }
    }
    dist.delete(focusId)
    return dist
  }
  // downstream first; a table on a cycle through the focus is drawn once, downstream
  const down = walk(tg.down, (e) => e.to, f.down, expand.down)
  const up = walk(tg.up, (e) => e.from, f.up, expand.up, down)
  const side = new Map<string, Side>([[focusId, 'focus']])
  const depth = new Map<string, number>([[focusId, 0]])
  for (const [id, d] of down) { side.set(id, 'down'); depth.set(id, d) }
  for (const [id, d] of up) { side.set(id, 'up'); depth.set(id, d) }

  let ids = [...depth.keys()]
  let hidden = 0
  if (ids.length > f.maxNodes) {
    ids.sort((a, b) => depth.get(a)! - depth.get(b)! || a.localeCompare(b))
    hidden = ids.length - f.maxNodes
    ids = ids.slice(0, f.maxNodes)
  }
  const inView = new Set(ids)

  const countMore = (adj: Map<string, TE[]>, next: (e: TE) => string, id: string) => {
    const s = new Set<string>()
    for (const e of adj.get(id) ?? []) {
      const n = next(e)
      if (!inView.has(n) && admit(n, false)) s.add(n)
    }
    return s.size
  }
  const nodes: ViewNode[] = ids.map((id) => {
    const n = fg.nodes.get(id)!
    const s = side.get(id)!
    return {
      id, label: n.name, type: n.type as TableType, focus: id === focusId, depth: depth.get(id)!, side: s,
      warning: n.meta.configWarning,             // shown as a badge on the card
      missing: n.meta.inLakehouse === false,
      more: {
        up: s === 'down' ? 0 : countMore(tg.up, (e) => e.from, id),
        down: s === 'up' ? 0 : countMore(tg.down, (e) => e.to, id),
      },
    }
  })
  const list: ViewEdge[] = []
  let sideLinks = 0
  for (const id of ids) {
    for (const e of tg.down.get(id) ?? []) {
      if (!inView.has(e.to)) continue
      if (!onFocusPath(side.get(e.from), side.get(e.to))) { sideLinks++; continue }
      const names = e.via.map((v) => v.name)
      list.push({
        id: `${e.from}|${e.to}|${e.kind}`, source: e.from, target: e.to, kind: e.kind, conf: e.conf,
        config: e.config, via: e.via,
        label: e.kind === 'binds' ? 'binds' : names.length === 1 ? shortName(names[0]) : `${names.length} jobs`,
      })
    }
  }
  const capped = capEdges(list, f.maxEdges)
  return {
    nodes, edges: capped.edges, anchor: focusId, truncated: hidden > 0, hidden, hiddenEdges: capped.hidden, sideLinks,
    skippedJobs: tg.skipped.length, hiddenUnresolved: hiddenUnknown.size, hiddenExcluded: hiddenByName.size,
  }
}

// ---------------------------------------------------------------------------------------
// job lens: the tables one job touches

export function buildJobView(fg: FlowGraph, jobId: string, f: Filters): View {
  const job = fg.nodes.get(jobId)
  if (!isJob(job)) return EMPTY
  const excluded = matcherFor(f)
  const ins = new Map<string, Conf>()
  const outs = new Map<string, Conf>()
  const hiddenUnknown = new Set<string>()
  const hiddenByName = new Set<string>()
  for (const e of fg.edges) {
    if (e.from !== jobId) continue
    const config = !!e.evidence?.some((ev) => ev.via === 'config resolver')
    if (!f.conf.has(e.confidence) || (config && !f.showConfig)) continue
    const t = fg.nodes.get(e.to)
    if (!t || !isDataType(t.type)) continue
    if (!f.types.has(t.type)) { if (t.type === 'unknown') hiddenUnknown.add(t.id); continue }
    if (excluded(t.name)) { hiddenByName.add(t.id); continue }
    if (e.kind === 'writes') outs.set(t.id, e.confidence)
    else if (e.kind === 'reads' || e.kind === 'references') ins.set(t.id, e.confidence)
  }
  let ids = [...new Set([...ins.keys(), ...outs.keys()])].sort()
  let hidden = 0
  if (ids.length > f.maxNodes) { hidden = ids.length - f.maxNodes; ids = ids.slice(0, f.maxNodes) }
  const inView = new Set(ids)
  const list: ViewEdge[] = []
  for (const [i, ic] of ins) {
    for (const [o, oc] of outs) {
      if (i === o || !inView.has(i) || !inView.has(o)) continue
      const conf = CONF_ORDER[ic] >= CONF_ORDER[oc] ? ic : oc
      list.push({ id: `${i}|${o}|flows`, source: i, target: o, kind: 'flows', conf, config: false, via: [viaOf(job, conf)], label: '' })
    }
  }
  const capped = capEdges(list, f.maxEdges)
  const nodes: ViewNode[] = ids.map((id) => {
    const n = fg.nodes.get(id)!
    return { id, label: n.name, type: n.type as TableType, depth: outs.has(id) && !ins.has(id) ? 1 : 0 }
  })
  return {
    nodes, edges: capped.edges, truncated: hidden > 0, hidden, hiddenEdges: capped.hidden,
    hiddenUnresolved: hiddenUnknown.size, hiddenExcluded: hiddenByName.size, job,
  }
}

// ---------------------------------------------------------------------------------------
// schemas mode

export function buildSchemaView(fg: FlowGraph, f: Filters): View {
  const tg = contractFor(fg, f)
  const excluded = matcherFor(f)
  const hiddenByName = new Set<string>()
  const tables = new Map<string, Set<string>>()
  const agg = new Map<string, { a: string; b: string; pairs: [string, string][]; conf: Conf; config: boolean; jobs: Map<string, Via> }>()
  for (const e of tg.edges) {
    const na = fg.nodes.get(e.from), nb = fg.nodes.get(e.to)
    if (!na || !nb) continue
    const skip = [na, nb].filter((n) => excluded(n.name))
    if (skip.length) { skip.forEach((n) => hiddenByName.add(n.id)); continue }
    const sa = schemaOf(na), sb = schemaOf(nb)
    if (!sa || !sb) continue
    for (const [s, id] of [[sa, e.from], [sb, e.to]] as const) {
      const set = tables.get(s)
      if (set) set.add(id)
      else tables.set(s, new Set([id]))
    }
    if (sa === sb) continue
    const key = `${sa}|${sb}`
    const cur = agg.get(key)
    if (!cur) {
      agg.set(key, { a: sa, b: sb, pairs: [[e.from, e.to]], conf: e.conf, config: e.config, jobs: new Map(e.via.map((v) => [v.id, v])) })
    } else {
      cur.pairs.push([e.from, e.to])
      if (CONF_ORDER[e.conf] < CONF_ORDER[cur.conf]) cur.conf = e.conf
      cur.config = cur.config && e.config
      for (const v of e.via) cur.jobs.set(v.id, v)
    }
  }
  const nodes: ViewNode[] = [...tables.entries()].map(([s, ids]) => ({
    id: `schema:${s}`, label: `${s} (${ids.size})`, type: 'schema' as const, depth: 0,
    tableIds: [...ids].sort((x, y) => (fg.degree.get(y) ?? 0) - (fg.degree.get(x) ?? 0) || x.localeCompare(y)),
  }))
  const list: ViewEdge[] = [...agg.values()].map((x) => ({
    id: `schema:${x.a}|schema:${x.b}`, source: `schema:${x.a}`, target: `schema:${x.b}`, kind: 'flows' as const,
    conf: x.conf, config: x.config, label: String(x.pairs.length), via: [...x.jobs.values()], pairs: x.pairs,
  }))
  const capped = capEdges(list, f.maxEdges)
  return {
    nodes, edges: capped.edges, truncated: false, hidden: 0, hiddenEdges: capped.hidden,
    skippedJobs: tg.skipped.length, hiddenExcluded: hiddenByName.size,
  }
}

// ---------------------------------------------------------------------------------------
// columns mode: one column and the columns it is computed from / feeds, grouped into tables

export interface ColumnContext {
  graph: ColumnGraph | null
  focus: ColRef | null
}

const tableName = (fg: FlowGraph, tableId: string) => fg.nodes.get(tableId)?.name ?? tableId.replace(/^table:/, '')

export function buildColumnView(fg: FlowGraph, cg: ColumnGraph, focus: ColRef, f: Filters): View {
  const excluded = matcherFor(f)
  const start = colId(focus)
  const hiddenByName = new Set<string>()
  const jobOk = (e: ColumnEdge) => {
    const job = fg.nodes.get(e.job)
    return !job || jobPasses(job, f)
  }
  const allowEdge = (e: ColumnEdge) => f.conf.has(e.confidence) && jobOk(e)
  const allowTable = (id: string) => {
    const t = parseColId(id).table
    if (!excluded(tableName(fg, t))) return true
    hiddenByName.add(t)
    return false
  }
  const side = new Map<string, Side>([[start, 'focus']])
  const depth = new Map<string, number>([[start, 0]])
  for (const r of cg.walk(start, 'down', { maxDepth: f.down, allowEdge, allowColumn: allowTable })) {
    depth.set(r.id, r.depth)
    side.set(r.id, 'down')
  }
  const upOnly = (id: string) => !side.has(id) && allowTable(id)
  for (const r of cg.walk(start, 'up', { maxDepth: f.up, allowEdge, allowColumn: upOnly })) {
    depth.set(r.id, r.depth)
    side.set(r.id, 'up')
  }

  let ids = [...depth.keys()]
  let hidden = 0
  if (ids.length > f.maxNodes) {
    ids.sort((a, b) => depth.get(a)! - depth.get(b)! || a.localeCompare(b))
    hidden = ids.length - f.maxNodes
    ids = ids.slice(0, f.maxNodes)
  }
  const inView = new Set(ids)

  // one card per table, listing its traced columns
  const cards = new Map<string, ViewNode>()
  for (const id of ids) {
    const r = parseColId(id)
    let card = cards.get(r.table)
    if (!card) {
      const n = fg.nodes.get(r.table)
      const type: TableType = n && isDataType(n.type) ? n.type : 'table'
      card = {
        id: r.table, label: tableName(fg, r.table), type, depth: depth.get(id)!, columns: [],
        focus: r.table === focus.table, side: r.table === focus.table ? 'focus' : side.get(id),
        missing: n?.meta.inLakehouse === false,
      }
      cards.set(r.table, card)
    }
    card.depth = Math.min(card.depth, depth.get(id)!)
    card.columns!.push({ id, name: r.column, focus: id === start, side: side.get(id) })
  }
  for (const card of cards.values()) {
    card.otherColumns = Math.max(0, cg.columnsOf(card.id).filter((c) => c !== '*').length - card.columns!.filter((c) => c.name !== '*').length)
  }

  const list: ViewEdge[] = []
  let sideLinks = 0
  for (const id of ids) {
    for (const [other, edges] of cg.down.get(id) ?? []) {
      if (!inView.has(other)) continue
      const ok = edges.filter(allowEdge)
      if (!ok.length) continue
      const a = parseColId(id).table, b = parseColId(other).table
      if (a === b || !onFocusPath(side.get(id), side.get(other))) { sideLinks++; continue }
      const transforms = [...new Set(ok.map((e) => e.transform))]
      const jobs = [...new Set(ok.map((e) => e.job))]
      list.push({
        id: `${id}>${other}`, source: a, target: b, sourceCol: id, targetCol: other, kind: 'flows', conf: confOf(ok),
        config: false, label: transforms.length === 1 ? (transforms[0] === 'direct' ? '' : transforms[0]) : 'mixed',
        columnEdges: ok,
        via: jobs.flatMap((j) => {
          const n = fg.nodes.get(j)
          return n ? [viaOf(n, confOf(ok))] : []
        }),
      })
    }
  }
  const capped = capEdges(list, f.maxEdges)
  return {
    nodes: [...cards.values()], edges: capped.edges, anchor: focus.table, truncated: hidden > 0, hidden,
    hiddenEdges: capped.hidden, sideLinks, hiddenExcluded: hiddenByName.size,
  }
}

/** Number of columns drawn in a columns view. */
export const columnCount = (v: View) => v.nodes.reduce((a, n) => a + (n.columns?.length ?? 0), 0)

/** Starting depth for a column: deeper while the graph stays small enough to read. */
export function autoColumnDepth(fg: FlowGraph, cg: ColumnGraph, focus: ColRef, f: Filters, target = 40): { up: number; down: number } {
  for (const [up, down] of [[4, 4], [3, 3], [2, 2]] as [number, number][]) {
    if (columnCount(buildColumnView(fg, cg, focus, { ...f, up, down })) <= target) return { up, down }
  }
  return { up: 1, down: 1 }
}

export function buildAnyView(fg: FlowGraph, focusId: string | null, f: Filters, cols?: ColumnContext, expand?: Expand): View {
  if (f.mode === 'schemas') return buildSchemaView(fg, f)
  if (f.mode === 'columns') return cols?.graph && cols.focus ? buildColumnView(fg, cols.graph, cols.focus, f) : EMPTY
  if (!focusId) return EMPTY
  const focus = fg.nodes.get(focusId)
  return isJob(focus) ? buildJobView(fg, focusId, f) : buildTableView(fg, focusId, f, expand)
}

/** Tables shown on each side by default, before the depth controls or the +N buttons open more. */
export const SIDE_BUDGET = 50
const MAX_AUTO_DEPTH = 4

/**
 * Starting depth for a focus table, sized per side: each side shows at least its first hop, then goes
 * deeper while it stays within ``perSide`` tables, and stops once a further hop adds nothing. A busy
 * downstream therefore never squeezes the upstream to nothing (or the other way round).
 */
export function autoDepth(fg: FlowGraph, focusId: string, f: Filters, perSide = SIDE_BUDGET): { up: number; down: number } {
  const pick = (count: (d: number) => number): number => {
    let best = 1
    let prev = count(1)
    for (let d = 2; d <= MAX_AUTO_DEPTH; d++) {
      const n = count(d)
      if (n > perSide || n === prev) break       // over budget, or the hop adds nothing new
      best = d
      prev = n
    }
    return best
  }
  const side = (up: number, down: number) => buildTableView(fg, focusId, { ...f, up, down, maxNodes: Infinity }).nodes.length - 1
  return { up: pick((d) => side(d, 0)), down: pick((d) => side(0, d)) }
}
