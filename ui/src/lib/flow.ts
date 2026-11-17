// Data-flow view of lineage.json. Port of columntrace/impact.py, with filters.
//
// Stored edges read like sentences (notebook --reads--> table). For impact analysis we
// walk *data flow*: a table flows into the notebooks/pipelines that read it and out of
// those that write it; semantic tables sit downstream of the table they bind to. A
// pipeline that merely calls an affected notebook is reported but does not fan out to
// its other children.

import type { Conf, LEdge, LNode, Lineage } from './types'

export const CONF_ORDER: Record<Conf, number> = { parsed: 0, inferred: 1, unresolved: 2 }

export interface FlowEdge {
  to: string
  conf: Conf
  via: string
  config: boolean
}

export interface WalkRow {
  id: string
  type: LNode['type']
  name: string
  depth: number
  confidence: Conf
  via: string
  from: string
}

export interface WalkOptions {
  maxDepth?: number
  /** weakest edge allowed */
  allowConf?: (c: Conf) => boolean
  /** include edges that came from the config resolver */
  includeConfig?: boolean
  /** nodes failing this are not entered (the start node is always kept) */
  allowNode?: (n: LNode) => boolean
}

export function isConfigEdge(e: LEdge): boolean {
  return !!e.evidence?.some((ev) => ev.via === 'config resolver')
}

export class FlowGraph {
  readonly nodes = new Map<string, LNode>()
  readonly down = new Map<string, FlowEdge[]>()
  readonly up = new Map<string, FlowEdge[]>()
  readonly edges: LEdge[]
  readonly degree = new Map<string, number>()

  constructor(public readonly data: Lineage) {
    for (const n of data.nodes) this.nodes.set(n.id, n)
    this.edges = data.edges
    for (const e of data.edges) {
      const config = isConfigEdge(e)
      if (!config) {   // config-resolved edges would make every loader's tables look like hubs
        this.degree.set(e.from, (this.degree.get(e.from) ?? 0) + 1)
        this.degree.set(e.to, (this.degree.get(e.to) ?? 0) + 1)
      }
      if (e.kind === 'reads' || e.kind === 'references') this.link(e.to, e.from, e, 'reads', config)
      else if (e.kind === 'writes') this.link(e.from, e.to, e, 'writes', config)
      else if (e.kind === 'binds') this.link(e.to, e.from, e, 'binds', config)
      else if (e.kind === 'calls') this.push(this.down, e.to, { to: e.from, conf: e.confidence, via: 'called-by', config })
    }
    // row-level flows from config tables: table -> table, precise and never "config-resolved"
    for (const f of data.flows ?? []) {
      if (!this.nodes.has(f.from) || !this.nodes.has(f.to)) continue
      this.degree.set(f.from, (this.degree.get(f.from) ?? 0) + 1)
      this.degree.set(f.to, (this.degree.get(f.to) ?? 0) + 1)
      this.link(f.from, f.to, { from: f.from, to: f.to, kind: 'writes', confidence: f.confidence }, 'flows', false)
    }
  }

  private push(m: Map<string, FlowEdge[]>, k: string, v: FlowEdge) {
    const list = m.get(k)
    if (list) list.push(v)
    else m.set(k, [v])
  }

  private link(a: string, b: string, e: LEdge, via: string, config: boolean) {
    this.push(this.down, a, { to: b, conf: e.confidence, via, config })
    this.push(this.up, b, { to: a, conf: e.confidence, via, config })
  }

  resolve(query: string): string | null {
    if (this.nodes.has(query)) return query
    const q = query.toLowerCase()
    for (const cand of [`table:${q}`, `notebook:${query}`, `pipeline:${query}`]) {
      if (this.nodes.has(cand)) return cand
    }
    const exact = [...this.nodes.values()].filter((n) => n.name.toLowerCase() === q)
    if (exact.length === 1) return exact[0].id
    const part = [...this.nodes.values()].filter((n) => n.name.toLowerCase().includes(q))
    return part.length === 1 ? part[0].id : null
  }

  search(query: string, limit = 12): LNode[] {
    const q = query.trim().toLowerCase()
    if (!q) return []
    const scored: [number, LNode][] = []
    for (const n of this.nodes.values()) {
      if (n.type === 'unknown') continue
      const name = n.name.toLowerCase()
      const i = name.indexOf(q)
      if (i < 0) continue
      scored.push([name === q ? 0 : i === 0 ? 1 : 2, n])
    }
    scored.sort((a, b) => a[0] - b[0] || a[1].name.length - b[1].name.length || a[1].name.localeCompare(b[1].name))
    return scored.slice(0, limit).map((s) => s[1])
  }

  walk(start: string, direction: 'down' | 'up', opts: WalkOptions = {}): WalkRow[] {
    const { maxDepth = 20, allowConf = () => true, includeConfig = true, allowNode = () => true } = opts
    const adj = direction === 'down' ? this.down : this.up
    const seen = new Set<string>([start])
    const out: WalkRow[] = []
    const queue: { id: string; depth: number; conf: Conf; via: string }[] = [
      { id: start, depth: 0, conf: 'parsed', via: '' },
    ]
    for (let qi = 0; qi < queue.length; qi++) {
      const cur = queue[qi]
      if (cur.depth >= maxDepth) continue
      const node = this.nodes.get(cur.id)
      if (!node) continue
      // a pipeline reached only because it calls an affected notebook does not fan out
      if (node.type === 'pipeline' && cur.via === 'called-by') continue
      for (const e of adj.get(cur.id) ?? []) {
        if (seen.has(e.to) || !allowConf(e.conf) || (!includeConfig && e.config)) continue
        const next = this.nodes.get(e.to)
        if (!next || !allowNode(next)) continue
        seen.add(e.to)
        const worst = CONF_ORDER[cur.conf] >= CONF_ORDER[e.conf] ? cur.conf : e.conf
        out.push({ id: e.to, type: next.type, name: next.name, depth: cur.depth + 1, confidence: worst, via: e.via, from: cur.id })
        queue.push({ id: e.to, depth: cur.depth + 1, conf: worst, via: e.via })
      }
    }
    return out
  }

  /**
   * Busiest tables, used as suggestions and the default focus. Configuration tables
   * (schema `cfg` or `config`) are skipped: nearly everything reads them, so a view
   * centered on one is a hairball rather than a useful starting point.
   */
  hubs(limit = 8, excluded: (name: string) => boolean = () => false): LNode[] {
    return [...this.nodes.values()]
      .filter((n) => n.type === 'table' && !n.meta.external && !/^(cfg|config)\./i.test(n.name) && !excluded(n.name))
      .sort((a, b) => (this.degree.get(b.id) ?? 0) - (this.degree.get(a.id) ?? 0) || a.name.localeCompare(b.name))
      .slice(0, limit)
  }
}
