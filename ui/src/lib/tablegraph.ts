// Table-level lineage: notebooks and pipelines are folded into the edges between tables.
//
//   bronze.a --reads--> nb_x --writes--> silver.b      becomes      bronze.a ==(nb_x)==> silver.b
//
// A job that reads N tables and writes M tables could feed any input into any output; the
// stored lineage cannot say which. We therefore attribute every pair, and skip jobs whose
// N x M is above a cap (generic loaders, utility notebooks): they are reported, never
// silently dropped, and stay visible in the Full view.

import { CONF_ORDER, FlowGraph } from './flow'
import type { Conf, LNode } from './types'

export interface Via {
  id: string
  name: string
  type: 'notebook' | 'pipeline'
  liveness: string
  stale: boolean
  lastChange: string
  conf: Conf
  basis?: 'columns'            // paired through column lineage (a job too broad to pair every input with every output)
}

export interface TEdge {
  from: string
  to: string
  conf: Conf
  config: boolean
  kind: 'flows' | 'binds'
  via: Via[]
}

export interface Contracted {
  edges: TEdge[]
  down: Map<string, TEdge[]>
  up: Map<string, TEdge[]>
  skipped: Via[]
}

export interface ContractOptions {
  allowItem: (n: LNode) => boolean
  allowConf: (c: Conf) => boolean
  includeConfig: boolean
  maxPairs?: number
}

export const MAX_PAIRS = 150
/** A job writing this many tables or fewer is never a generic loader: all its pairs are drawn. */
export const FEW_OUTPUTS = 3

const worst = (a: Conf, b: Conf): Conf => (CONF_ORDER[a] >= CONF_ORDER[b] ? a : b)
const best = (a: Conf, b: Conf): Conf => (CONF_ORDER[a] <= CONF_ORDER[b] ? a : b)

interface End { table: string; conf: Conf; config: boolean }

export function contract(fg: FlowGraph, opts: ContractOptions): Contracted {
  const maxPairs = opts.maxPairs ?? MAX_PAIRS
  const ins = new Map<string, End[]>()
  const outs = new Map<string, End[]>()
  const push = (m: Map<string, End[]>, k: string, v: End) => {
    const l = m.get(k)
    if (l) l.push(v)
    else m.set(k, [v])
  }
  const merged = new Map<string, TEdge>()
  const add = (from: string, to: string, conf: Conf, config: boolean, kind: TEdge['kind'], via?: Via) => {
    const key = `${from}|${to}|${kind}`
    const e = merged.get(key)
    if (!e) {
      merged.set(key, { from, to, conf, config, kind, via: via ? [via] : [] })
      return
    }
    e.conf = best(e.conf, conf)
    e.config = e.config && config
    if (via && !e.via.some((v) => v.id === via.id)) e.via.push(via)
  }

  for (const e of fg.edges) {
    const config = !!e.evidence?.some((ev) => ev.via === 'config resolver')
    if (!opts.allowConf(e.confidence) || (config && !opts.includeConfig)) continue
    if (e.kind === 'reads' || e.kind === 'references') push(ins, e.from, { table: e.to, conf: e.confidence, config })
    else if (e.kind === 'writes') push(outs, e.from, { table: e.to, conf: e.confidence, config })
    else if (e.kind === 'binds') add(e.to, e.from, e.confidence, config, 'binds')
  }

  // exact table pairs per job, from column lineage: job -> "source|target" -> best confidence
  const colPairs = new Map<string, Map<string, Conf>>()
  for (const e of fg.data.columns?.edges ?? []) {
    if (e.from === e.to || !opts.allowConf(e.confidence)) continue
    let m = colPairs.get(e.job)
    if (!m) colPairs.set(e.job, (m = new Map()))
    const k = `${e.from}|${e.to}`
    const cur = m.get(k)
    m.set(k, cur ? best(cur, e.confidence) : e.confidence)
  }

  const skipped: Via[] = []
  for (const [id, inputs] of ins) {
    const outputs = outs.get(id)
    if (!outputs) continue
    const node = fg.nodes.get(id)
    if (!node || (node.type !== 'notebook' && node.type !== 'pipeline') || !opts.allowItem(node)) continue
    const info = (conf: Conf, basis?: Via['basis']): Via => ({
      id, name: node.name, type: node.type as 'notebook' | 'pipeline', liveness: node.meta.liveness ?? '',
      stale: !!node.meta.stale, lastChange: node.meta.lastChange ? node.meta.lastChange.slice(0, 10) : '', conf,
      ...(basis ? { basis } : {}),
    })
    // Unresolved names (type 'unknown') are hidden unless asked for, so they must not push a job over
    // the limit: count the resolved pairs first and add the unresolved ones only if they still fit.
    const isUnknown = (e: End) => fg.nodes.get(e.table)?.type === 'unknown'
    const known = (l: End[]) => l.filter((e) => !isUnknown(e))
    let rest = outputs
    const broad = (outs: End[]) => known(outs).length > FEW_OUTPUTS && known(inputs).length * known(outs).length > maxPairs
    if (broad(outputs)) {
      // too broad to pair every input with every output: outputs with column lineage get their exact
      // source tables from it; the others are paired as usual if that fits, else the job is skipped
      const pairs = colPairs.get(id)
      const covered = new Set<string>()
      for (const [k, conf] of pairs ?? []) {
        const [src, dst] = k.split('|')
        const o = outputs.find((x) => x.table === dst)
        if (!o || !fg.nodes.has(src)) continue
        covered.add(dst)
        const c = worst(conf, o.conf)
        add(src, dst, c, o.config, 'flows', info(c, 'columns'))
      }
      rest = outputs.filter((o) => !covered.has(o.table))
      if (!rest.length) continue
      if (known(inputs).length * known(rest).length > maxPairs) {
        skipped.push(info('inferred'))
        continue
      }
    }
    const withUnknown = inputs.length * rest.length <= maxPairs
    for (const i of inputs) {
      for (const o of rest) {
        if (!withUnknown && (isUnknown(i) || isUnknown(o))) continue
        if (i.table === o.table) continue   // a merge that reads its own target is not lineage
        const conf = worst(i.conf, o.conf)
        add(i.table, o.table, conf, i.config || o.config, 'flows', info(conf))
      }
    }
  }

  // row-level flows from config tables: exact pairs, attributed to the loader that runs them
  for (const f of fg.data.flows ?? []) {
    if (!opts.allowConf(f.confidence) || !fg.nodes.has(f.from) || !fg.nodes.has(f.to)) continue
    const job = fg.nodes.get(f.job)
    if (job && !opts.allowItem(job)) continue
    const via: Via = job && (job.type === 'notebook' || job.type === 'pipeline')
      ? {
        id: job.id, name: job.name, type: job.type, liveness: job.meta.liveness ?? '', stale: !!job.meta.stale,
        lastChange: job.meta.lastChange ? job.meta.lastChange.slice(0, 10) : '', conf: f.confidence,
      }
      : { id: f.job, name: f.job.replace(/^config:/, 'config table '), type: 'notebook', liveness: '', stale: false, lastChange: '', conf: f.confidence }
    add(f.from, f.to, f.confidence, false, 'flows', via)
  }

  const edges = [...merged.values()]
  const down = new Map<string, TEdge[]>()
  const up = new Map<string, TEdge[]>()
  for (const e of edges) {
    const d = down.get(e.from)
    if (d) d.push(e)
    else down.set(e.from, [e])
    const u = up.get(e.to)
    if (u) u.push(e)
    else up.set(e.to, [e])
  }
  return { edges, down, up, skipped }
}

export function schemaOf(n: LNode): string | null {
  if (n.type === 'unknown') return null
  if (n.type === 'semantic_table') return `semantic: ${(n.meta.model as string | undefined) ?? n.name.split('.')[0]}`
  if (n.meta.external) return `source: ${(n.meta.system as string | undefined) ?? n.name.split(':')[0]}`
  const i = n.name.indexOf('.')
  return i > 0 ? n.name.slice(0, i) : '(unqualified)'
}
