// Impact report: everything downstream (breaks if it changes) and upstream (feeds it).

import { FlowGraph, type WalkRow } from './flow'
import type { Conf, LNode } from './types'

export interface ReportRow extends WalkRow {
  liveness: string
  stale: boolean
  lastChange: string
  family: string
  path: string
}

export interface ImpactReport {
  focus: LNode
  confidence: 'parsed' | 'inferred'
  downstream: ReportRow[]
  upstream: ReportRow[]
  strictDownstream: number
  looseDownstream: number
}

function enrich(fg: FlowGraph, rows: WalkRow[]): ReportRow[] {
  return rows
    .filter((r) => r.type !== 'unknown')
    .map((r) => {
      const n = fg.nodes.get(r.id)!
      return {
        ...r,
        liveness: n.meta.liveness ?? '',
        stale: !!n.meta.stale,
        lastChange: n.meta.lastChange ? n.meta.lastChange.slice(0, 10) : '',
        family: n.meta.family ?? '',
        path: n.path ?? '',
      }
    })
    .sort((a, b) => a.depth - b.depth || a.type.localeCompare(b.type) || a.name.localeCompare(b.name))
}

export function buildImpact(fg: FlowGraph, focusId: string, confidence: 'parsed' | 'inferred' = 'inferred',
                            includeConfig = true): ImpactReport {
  const focus = fg.nodes.get(focusId)!
  const allow = (limit: Conf) => (c: Conf) =>
    limit === 'parsed' ? c === 'parsed' : c === 'parsed' || c === 'inferred'
  const run = (dir: 'down' | 'up', limit: 'parsed' | 'inferred') =>
    enrich(fg, fg.walk(focusId, dir, { allowConf: allow(limit), includeConfig }))
  return {
    focus,
    confidence,
    downstream: run('down', confidence),
    upstream: run('up', confidence),
    strictDownstream: run('down', 'parsed').length,
    looseDownstream: run('down', 'inferred').length,
  }
}

const HEADER = ['direction', 'depth', 'type', 'name', 'confidence', 'reached_via', 'liveness', 'stale', 'last_change', 'family', 'path']

export function csvEscape(v: string | number | boolean): string {
  const s = String(v)
  return /[",\n\r]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s
}

export function toCsv(r: ImpactReport): string {
  const line = (dir: string, x: ReportRow) =>
    [dir, x.depth, x.type, x.name, x.confidence, x.via, x.liveness, x.stale, x.lastChange, x.family, x.path]
      .map(csvEscape).join(',')
  return [HEADER.join(','), ...r.downstream.map((x) => line('downstream', x)), ...r.upstream.map((x) => line('upstream', x))].join('\n')
}

export function toMarkdown(r: ImpactReport): string {
  const table = (title: string, rows: ReportRow[]) => {
    const out = [`## ${title} (${rows.length})`]
    if (!rows.length) return [...out, '_none found_', ''].join('\n')
    out.push('| Depth | Type | Name | Confidence | Reached via | Liveness | Last change |', '| --- | --- | --- | --- | --- | --- | --- |')
    for (const x of rows) {
      const flags = [x.liveness, x.stale ? 'stale' : ''].filter(Boolean).join(', ')
      out.push(`| ${x.depth} | ${x.type} | ${x.name} | ${x.confidence} | ${x.via} | ${flags} | ${x.lastChange} |`)
    }
    return [...out, ''].join('\n')
  }
  return [
    `# Impact report: ${r.focus.name}`,
    '',
    `Downstream size by strictness: ${r.strictDownstream} using parsed edges only, ${r.looseDownstream} including inferred edges. Showing: **${r.confidence}**.`,
    '',
    table('Downstream: breaks if this changes', r.downstream),
    table('Upstream: feeds this', r.upstream),
    '_Confidence is the weakest edge on the path. `inferred` edges come from config resolution or unqualified names; `unresolved` items are never silently dropped. Liveness and last change are repo-only hints, not proof of use._',
  ].join('\n')
}
