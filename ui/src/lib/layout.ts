// Layered left-to-right layout for lineage graphs (a small Sugiyama-style layout).
//
//   1. break cycles: edges that close a cycle are left out of the ranking (and reported)
//   2. rank: with an anchor (the focus), every ancestor sits as close to it as its longest
//      path allows, on its left, and every descendant on its right. Without one, nodes take
//      the longest path from the sources and are then pulled next to their first consumer.
//   3. order each rank by the barycenter of its neighbors (a few left/right sweeps)
//   4. y: each node is pulled toward its neighbors without overlapping the others in its rank
//
// Every kept edge then runs from left to right, so edges can be drawn as smooth curves.

export interface LayoutNode { id: string; w: number; h: number }
export interface LayoutEdge { s: string; t: string }
export interface Box { x: number; y: number; w: number; h: number; rank: number }

export interface Layout {
  boxes: Map<string, Box>
  width: number
  height: number
  backEdges: number          // edges that close a cycle (not used for ranking)
  back: Set<string>          // those edges, as "source>target"
}

export interface LayoutOptions {
  anchor?: string
  gapX?: number
  gapY?: number
  sweeps?: number
}

export function layered(nodes: LayoutNode[], edges: LayoutEdge[], opts: LayoutOptions = {}): Layout {
  const { anchor, gapX = 110, gapY = 16, sweeps = 8 } = opts
  const ids = nodes.map((n) => n.id)
  const known = new Set(ids)
  const size = new Map(nodes.map((n) => [n.id, n]))
  const out = new Map<string, string[]>(ids.map((id) => [id, []]))
  const inc = new Map<string, string[]>(ids.map((id) => [id, []]))
  const real = edges.filter((e) => known.has(e.s) && known.has(e.t) && e.s !== e.t)

  // 1. cycle breaking: iterative DFS from the anchor first, then everything else
  const order = anchor && known.has(anchor) ? [anchor, ...ids.filter((i) => i !== anchor)] : ids
  const succ = new Map<string, string[]>(ids.map((id) => [id, []]))
  for (const e of real) succ.get(e.s)!.push(e.t)
  const state = new Map<string, 1 | 2>()     // 1 on the stack, 2 done
  const back = new Set<string>()
  for (const root of order) {
    if (state.has(root)) continue
    const stack: [string, number][] = [[root, 0]]
    state.set(root, 1)
    while (stack.length) {
      const top = stack[stack.length - 1]
      const next = succ.get(top[0])!
      if (top[1] >= next.length) { state.set(top[0], 2); stack.pop(); continue }
      const t = next[top[1]++]
      const st = state.get(t)
      if (st === 1) back.add(`${top[0]}>${t}`)
      else if (!st) { state.set(t, 1); stack.push([t, 0]) }
    }
  }
  let backEdges = 0
  for (const e of real) {
    if (back.has(`${e.s}>${e.t}`)) { backEdges++; continue }
    out.get(e.s)!.push(e.t)
    inc.get(e.t)!.push(e.s)
  }

  // topological order (Kahn) of the remaining DAG
  const indeg = new Map(ids.map((id) => [id, inc.get(id)!.length]))
  const topo: string[] = ids.filter((id) => indeg.get(id) === 0)
  for (let i = 0; i < topo.length; i++) {
    for (const t of out.get(topo[i])!) {
      indeg.set(t, indeg.get(t)! - 1)
      if (indeg.get(t) === 0) topo.push(t)
    }
  }

  // 2. ranks
  const rank = new Map<string, number>()
  if (anchor && known.has(anchor)) {
    const toAnchor = new Map<string, number>([[anchor, 0]])
    for (let i = topo.length - 1; i >= 0; i--) {
      const id = topo[i]
      for (const t of out.get(id)!) {
        const d = toAnchor.get(t)
        if (d !== undefined && (toAnchor.get(id) ?? -1) < d + 1 && id !== anchor) toAnchor.set(id, d + 1)
      }
    }
    const fromAnchor = new Map<string, number>([[anchor, 0]])
    for (const id of topo) {
      const d = fromAnchor.get(id)
      if (d === undefined) continue
      for (const t of out.get(id)!) if ((fromAnchor.get(t) ?? -1) < d + 1) fromAnchor.set(t, d + 1)
    }
    for (const [id, d] of toAnchor) rank.set(id, -d)
    for (const [id, d] of fromAnchor) if (!rank.has(id)) rank.set(id, d)
    // anything else (not connected through the anchor) hangs off its ranked neighbors
    for (let pass = 0; pass < 2; pass++) {
      for (const id of topo) {
        if (rank.has(id)) continue
        const p = inc.get(id)!.filter((x) => rank.has(x)).map((x) => rank.get(x)! + 1)
        const s = out.get(id)!.filter((x) => rank.has(x)).map((x) => rank.get(x)! - 1)
        if (p.length) rank.set(id, Math.max(...p))
        else if (s.length) rank.set(id, Math.min(...s))
      }
    }
    for (const id of ids) if (!rank.has(id)) rank.set(id, 0)
  } else {
    for (const id of topo) rank.set(id, Math.max(0, ...inc.get(id)!.map((p) => rank.get(p)! + 1)))
    for (let i = topo.length - 1; i >= 0; i--) {
      const id = topo[i]
      const s = out.get(id)!
      if (s.length) rank.set(id, Math.min(...s.map((t) => rank.get(t)!)) - 1)
    }
  }
  const minRank = Math.min(0, ...rank.values())
  for (const [id, r] of rank) rank.set(id, r - minRank)

  // 3. order within ranks: DFS discovery order to start, then barycenter sweeps
  const nRanks = Math.max(0, ...rank.values()) + 1
  const layers: string[][] = Array.from({ length: nRanks }, () => [])
  const seen = new Set<string>()
  const visit = (id: string) => {
    if (seen.has(id)) return
    seen.add(id)
    layers[rank.get(id)!].push(id)
    for (const t of out.get(id)!) visit(t)
    for (const p of inc.get(id)!) visit(p)
  }
  for (const id of order) visit(id)
  const pos = new Map<string, number>()
  const index = () => layers.forEach((l) => l.forEach((id, i) => pos.set(id, i - (l.length - 1) / 2)))
  index()
  for (let s = 0; s < sweeps; s++) {
    const forward = s % 2 === 0
    const seq = forward ? layers.map((_, i) => i) : layers.map((_, i) => nRanks - 1 - i)
    for (const r of seq) {
      const layer = layers[r]
      const bary = new Map<string, number>()
      for (const id of layer) {
        const nb = forward ? inc.get(id)! : out.get(id)!
        bary.set(id, nb.length ? nb.reduce((a, x) => a + pos.get(x)!, 0) / nb.length : pos.get(id)!)
      }
      layer.sort((a, b) => bary.get(a)! - bary.get(b)! || pos.get(a)! - pos.get(b)!)
      layer.forEach((id, i) => pos.set(id, i - (layer.length - 1) / 2))
    }
  }

  // 4. coordinates
  const colW = layers.map((l) => Math.max(0, ...l.map((id) => size.get(id)!.w)))
  const colX: number[] = []
  colW.reduce((x, w, i) => { colX[i] = x; return x + w + gapX }, 0)
  const cy = new Map<string, number>()     // center y
  for (const l of layers) {
    const total = l.reduce((a, id) => a + size.get(id)!.h, 0) + gapY * Math.max(0, l.length - 1)
    let y = -total / 2
    for (const id of l) { const h = size.get(id)!.h; cy.set(id, y + h / 2); y += h + gapY }
  }
  const neighbors = (id: string) => [...inc.get(id)!, ...out.get(id)!]
  for (let it = 0; it < 6; it++) {
    const seq = it % 2 === 0 ? layers : [...layers].reverse()
    for (const l of seq) {
      if (!l.length) continue
      const want = l.map((id) => {
        const nb = neighbors(id)
        return nb.length ? nb.reduce((a, x) => a + cy.get(x)!, 0) / nb.length : cy.get(id)!
      })
      // the feasible placement closest to the wanted one: average of a downward and an upward pack
      const h = l.map((id) => size.get(id)!.h)
      const down: number[] = []
      const up: number[] = []
      for (let i = 0; i < l.length; i++) {
        const top = want[i] - h[i] / 2
        down[i] = i === 0 ? top : Math.max(top, down[i - 1] + h[i - 1] + gapY)
      }
      for (let i = l.length - 1; i >= 0; i--) {
        const top = want[i] - h[i] / 2
        up[i] = i === l.length - 1 ? top : Math.min(top, up[i + 1] - h[i] - gapY)
      }
      l.forEach((id, i) => cy.set(id, (down[i] + up[i]) / 2 + h[i] / 2))
    }
  }

  const boxes = new Map<string, Box>()
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity
  layers.forEach((l, r) => {
    for (const id of l) {
      const n = size.get(id)!
      const x = colX[r] + (colW[r] - n.w) / 2
      const y = cy.get(id)! - n.h / 2
      boxes.set(id, { x, y, w: n.w, h: n.h, rank: r })
      minX = Math.min(minX, x); minY = Math.min(minY, y)
      maxX = Math.max(maxX, x + n.w); maxY = Math.max(maxY, y + n.h)
    }
  })
  if (!boxes.size) return { boxes, width: 0, height: 0, backEdges, back }
  for (const b of boxes.values()) { b.x -= minX; b.y -= minY }
  return { boxes, width: maxX - minX, height: maxY - minY, backEdges, back }
}

/**
 * Orders the rows inside each card (the columns of a table) so links cross less: each row
 * moves toward the mean height of the rows it is linked to. `rowY(card, row)` gives the
 * current height of a row; `links` are pairs of row ids.
 */
export function orderRows(
  rows: Map<string, string[]>,             // card id -> row ids, in their current order
  rowCard: Map<string, string>,            // row id -> card id
  links: [string, string][],
  rowY: (card: string, index: number) => number,
  passes = 3,
): Map<string, string[]> {
  const result = new Map([...rows].map(([k, v]) => [k, [...v]]))
  const adj = new Map<string, string[]>()
  for (const [a, b] of links) {
    if (!adj.has(a)) adj.set(a, [])
    if (!adj.has(b)) adj.set(b, [])
    adj.get(a)!.push(b)
    adj.get(b)!.push(a)
  }
  const yOf = (row: string) => {
    const card = rowCard.get(row)!
    return rowY(card, result.get(card)!.indexOf(row))
  }
  for (let p = 0; p < passes; p++) {
    for (const [card, list] of result) {
      const score = new Map(list.map((r, i) => {
        const nb = (adj.get(r) ?? []).filter((x) => rowCard.get(x) !== card)
        return [r, nb.length ? nb.reduce((a, x) => a + yOf(x), 0) / nb.length : rowY(card, i)]
      }))
      list.sort((a, b) => score.get(a)! - score.get(b)!)
    }
  }
  return result
}
