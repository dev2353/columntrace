// "Used by": the notebooks and pipelines that write or read a table, for the side panel.

import { FlowGraph, isConfigEdge } from './flow'
import type { Conf, Evidence, LNode } from './types'

export interface UseRow {
  job: LNode
  conf: Conf
  config: boolean          // attached through config expansion, not seen in the job's code
  how: 'writes' | 'reads' | 'references'
  evidence?: Evidence
}

export interface Usage {
  writers: UseRow[]
  readers: UseRow[]        // reads and string-literal references
  binds: LNode[]           // semantic tables built on this table
  boundTo: LNode[]         // (for a semantic table) the table it is built on
}

const LIVE_ORDER = ['scheduled', 'orchestrated', 'called', 'entry-point', 'feeds-others', 'uncalled', 'dead-end', '']

function sortRows(rows: UseRow[]): UseRow[] {
  return rows.sort((a, b) =>
    Number(a.config) - Number(b.config) ||
    LIVE_ORDER.indexOf(a.job.meta.liveness ?? '') - LIVE_ORDER.indexOf(b.job.meta.liveness ?? '') ||
    a.job.name.localeCompare(b.job.name))
}

export function usedBy(fg: FlowGraph, tableId: string): Usage {
  const writers: UseRow[] = []
  const readers: UseRow[] = []
  const binds: LNode[] = []
  const boundTo: LNode[] = []
  for (const e of fg.edges) {
    if (e.to === tableId) {
      const job = fg.nodes.get(e.from)
      if (!job) continue
      if (job.type === 'notebook' || job.type === 'pipeline') {
        const row: UseRow = {
          job, conf: e.confidence, config: isConfigEdge(e), how: e.kind as UseRow['how'], evidence: e.evidence?.[0],
        }
        if (e.kind === 'writes') writers.push(row)
        else if (e.kind === 'reads' || e.kind === 'references') readers.push(row)
      } else if (job.type === 'semantic_table' && e.kind === 'binds') {
        binds.push(job)
      }
    } else if (e.from === tableId && e.kind === 'binds') {
      const t = fg.nodes.get(e.to)
      if (t) boundTo.push(t)
    }
  }
  return { writers: sortRows(writers), readers: sortRows(readers), binds, boundTo }
}

/** Other items whose name differs only by a variant suffix (_python, _v2, _copy ...). */
export function familyOf(fg: FlowGraph, node: LNode): LNode[] {
  const fam = node.meta.family
  if (!fam) return []
  return [...fg.nodes.values()]
    .filter((n) => n.type === node.type && n.meta.family === fam)
    .sort((a, b) => (b.meta.lastChange ?? '').localeCompare(a.meta.lastChange ?? ''))
}
