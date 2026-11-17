// Column-level lineage graph: nodes are table columns, edges are the SQL that derives one
// column from another. Built from the optional `columns` section of lineage.json.

import type { Conf, ColumnEdge, ColumnsData } from './types'

export interface ColRef {
  table: string     // table id, e.g. table:silver_x.orders
  column: string
}

export const colId = (r: ColRef) => `${r.table}#${r.column}`
export const parseColId = (id: string): ColRef => {
  const i = id.lastIndexOf('#')
  return { table: id.slice(0, i), column: id.slice(i + 1) }
}

export interface ColLink {
  to: string               // the other end (column id)
  edges: ColumnEdge[]      // one per job that derives it
}

export interface ColWalkRow {
  id: string
  depth: number
  via: ColumnEdge[]        // edges that led here from the previous column
  from: string
}

export class ColumnGraph {
  readonly down = new Map<string, Map<string, ColumnEdge[]>>()
  readonly up = new Map<string, Map<string, ColumnEdge[]>>()
  readonly tables: Record<string, string[]>
  private readonly mentioned = new Map<string, Set<string>>()   // table -> columns the lineage names

  constructor(readonly data: ColumnsData) {
    this.tables = data.tables
    for (const e of data.edges) {
      const a = colId({ table: e.from, column: e.fromColumn })
      const b = colId({ table: e.to, column: e.toColumn })
      this.add(this.down, a, b, e)
      this.add(this.up, b, a, e)
      this.mention(e.from, e.fromColumn)
      this.mention(e.to, e.toColumn)
    }
  }

  private mention(table: string, column: string) {
    const s = this.mentioned.get(table)
    if (s) s.add(column)
    else this.mentioned.set(table, new Set([column]))
  }

  private add(m: Map<string, Map<string, ColumnEdge[]>>, k: string, other: string, e: ColumnEdge) {
    let inner = m.get(k)
    if (!inner) m.set(k, (inner = new Map()))
    const list = inner.get(other)
    if (list) list.push(e)
    else inner.set(other, [e])
  }

  /** Columns of a table: the exported schema, plus any column the lineage mentions. */
  columnsOf(table: string): string[] {
    const known = this.tables[table]
    const extra = this.mentioned.get(table) ?? new Set<string>()
    if (!known) return [...extra].sort()
    const set = new Set(known)
    return [...known, ...[...extra].filter((c) => !set.has(c) && c !== '*').sort()]
  }

  counts(table: string, column: string): { sources: number; consumers: number } {
    const id = colId({ table, column })
    return { sources: this.up.get(id)?.size ?? 0, consumers: this.down.get(id)?.size ?? 0 }
  }

  /** Every column that has at least one source or consumer. */
  private tracedCache?: string[]
  private traced(): string[] {
    this.tracedCache ??= [...new Set([...this.down.keys(), ...this.up.keys()])].filter((id) => !id.endsWith('#*'))
    return this.tracedCache
  }

  private degree(id: string): number {
    return (this.up.get(id)?.size ?? 0) + (this.down.get(id)?.size ?? 0)
  }

  /** The best columns to start from: most connected first. ``skip`` filters tables (exclusions). */
  topColumns(limit: number, skip: (table: string) => boolean = () => false, onlyTable?: string): ColRef[] {
    return this.traced()
      .map(parseColId)
      .filter((r) => (!onlyTable || r.table === onlyTable) && !skip(r.table))
      .sort((a, b) => this.degree(colId(b)) - this.degree(colId(a)) || colId(a).localeCompare(colId(b)))
      .slice(0, limit)
  }

  /** Every column the file knows: traced ones plus the schema's columns of each table. */
  private allCache?: string[]
  private all(): string[] {
    if (!this.allCache) {
      const ids = new Set(this.traced())
      for (const [t, cols] of Object.entries(this.tables)) for (const c of cols) ids.add(colId({ table: t, column: c }))
      this.allCache = [...ids]
    }
    return this.allCache
  }

  /** Columns whose "table.column" name contains the query, most connected first; columns without
   *  lineage are included (after the traced ones) so they can still be found and opened. */
  search(query: string, nameOf: (table: string) => string, limit = 8): ColRef[] {
    const q = query.trim().toLowerCase()
    if (q.length < 3) return []
    return this.all()
      .map(parseColId)
      .filter((r) => `${nameOf(r.table)}.${r.column}`.toLowerCase().includes(q))
      .sort((a, b) => this.degree(colId(b)) - this.degree(colId(a)) || colId(a).localeCompare(colId(b)))
      .slice(0, limit)
  }

  hasLineage(table: string): boolean {
    return this.mentioned.has(table)
  }

  walk(start: string, direction: 'down' | 'up', opts: {
    maxDepth: number
    allowEdge: (e: ColumnEdge) => boolean
    allowColumn: (id: string) => boolean
  }): ColWalkRow[] {
    const adj = direction === 'down' ? this.down : this.up
    const seen = new Set<string>([start])
    const out: ColWalkRow[] = []
    const queue: [string, number][] = [[start, 0]]
    for (let i = 0; i < queue.length; i++) {
      const [id, d] = queue[i]
      if (d >= opts.maxDepth) continue
      for (const [other, edges] of adj.get(id) ?? []) {
        if (seen.has(other)) continue
        const ok = edges.filter(opts.allowEdge)
        if (!ok.length || !opts.allowColumn(other)) continue
        seen.add(other)
        out.push({ id: other, depth: d + 1, via: ok, from: id })
        queue.push([other, d + 1])
      }
    }
    return out
  }
}

export const confOf = (edges: ColumnEdge[]): Conf =>
  edges.some((e) => e.confidence === 'parsed') ? 'parsed' : 'inferred'
