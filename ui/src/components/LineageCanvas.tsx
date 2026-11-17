// The lineage canvas: table cards laid out left to right (sources on the left, consumers on
// the right), joined by smooth curves. In the columns view a card lists the traced columns
// of its table and the curves join column rows. Hovering a card or a column highlights its
// whole path; a click pins it (and opens the details); a double click re-centers on it.

import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { layered, orderRows, type Box } from '../lib/layout'
import type { Mode, View, ViewEdge, ViewNode } from '../lib/view'

interface Props {
  view: View
  mode: Mode
  selectedId: string | null
  selectedEdgeId: string | null
  onSelect: (id: string | null) => void
  onSelectEdge: (id: string) => void
  onFocus: (id: string) => void
  onExpand?: (id: string, dir: 'up' | 'down') => void
  onLoops?: (n: number) => void         // how many loop links were left out
}

const CARD_W = 236
const CARD_H = 50
const HEADER = 46
const ROW = 24
const FOOT = 22
const SCHEMA_W = 210

const MARK: Record<string, string> = { expression: 'f', aggregate: 'Σ', star: '*', mixed: 'f' }
const TRANSFORM_TEXT: Record<string, string> = {
  expression: 'computed by an expression', aggregate: 'aggregated', star: 'SELECT * (schema unknown)', mixed: 'several transforms',
}

function cardHeight(n: ViewNode): number {
  if (!n.columns) return CARD_H
  return HEADER + n.columns.length * ROW + (n.otherColumns ? FOOT : 6)
}

function splitName(n: ViewNode): [string, string] {
  if (n.type === 'schema') {
    const m = /^(.*) \((\d+)\)$/.exec(n.label)
    return m ? [`${m[2]} tables`, m[1]] : ['schema', n.label]
  }
  const i = n.label.indexOf('.')
  if (i <= 0) return [n.type === 'unknown' ? 'unresolved name' : '', n.label]
  return [n.label.slice(0, i), n.label.slice(i + 1)]
}

const curve = (x1: number, y1: number, x2: number, y2: number) => {
  const dx = Math.max(36, Math.abs(x2 - x1) * 0.5)
  return `M${x1},${y1} C${x1 + dx},${y1} ${x2 - dx},${y2} ${x2},${y2}`
}
const midpoint = (x1: number, y1: number, x2: number, y2: number) => {
  const dx = Math.max(36, Math.abs(x2 - x1) * 0.5)
  return { x: (x1 + 3 * (x1 + dx) + 3 * (x2 - dx) + x2) / 8, y: (y1 + y2) / 2 }
}

function Icon({ type }: { type: ViewNode['type'] }) {
  if (type === 'schema') {
    return <svg viewBox="0 0 16 16" aria-hidden="true"><ellipse cx="8" cy="4" rx="5.5" ry="2.2" /><path d="M2.5 4v8c0 1.2 2.5 2.2 5.5 2.2s5.5-1 5.5-2.2V4M2.5 8c0 1.2 2.5 2.2 5.5 2.2s5.5-1 5.5-2.2" /></svg>
  }
  if (type === 'semantic_table') {
    return <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 1.8 14 5v6l-6 3.2L2 11V5zM2 5l6 3.2L14 5M8 8.2v6" /></svg>
  }
  if (type === 'unknown') {
    return <svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="6" /><path d="M6.2 6.2a1.9 1.9 0 1 1 2.6 1.8c-.5.2-.8.6-.8 1.1v.6M8 11.6v.2" /></svg>
  }
  return <svg viewBox="0 0 16 16" aria-hidden="true"><rect x="2" y="2.5" width="12" height="11" rx="1.6" /><path d="M2 6.2h12M2 9.8h12M6.5 6.2v7.3" /></svg>
}

interface Geometry {
  boxes: Map<string, Box>
  width: number
  height: number
  rows: Map<string, string[]>            // card -> column ids in drawing order
  paths: Map<string, { d: string; mid: { x: number; y: number } }>
  loops: number                          // links that close a cycle around a focus: not drawn
}

function useGeometry(view: View): Geometry {
  return useMemo(() => {
    const columns = view.nodes.some((n) => n.columns)
    const lay = layered(
      view.nodes.map((n) => ({ id: n.id, w: n.type === 'schema' ? SCHEMA_W : CARD_W, h: cardHeight(n) })),
      view.edges.map((e) => ({ s: e.source, t: e.target })),
      { anchor: view.anchor, gapX: columns ? 120 : 110, gapY: columns ? 22 : 14 },
    )
    const { boxes } = lay
    // rows: the focus column first, then by name; then reordered so links cross less
    let rows = new Map<string, string[]>()
    const rowCard = new Map<string, string>()
    for (const n of view.nodes) {
      if (!n.columns) continue
      const list = [...n.columns].sort((a, b) => Number(!!b.focus) - Number(!!a.focus) || a.name.localeCompare(b.name)).map((c) => c.id)
      rows.set(n.id, list)
      for (const c of list) rowCard.set(c, n.id)
    }
    const rowY = (card: string, i: number) => boxes.get(card)!.y + HEADER + i * ROW + ROW / 2
    if (columns) {
      rows = orderRows(rows, rowCard, view.edges.flatMap((e) => (e.sourceCol && e.targetCol ? [[e.sourceCol, e.targetCol] as [string, string]] : [])), rowY)
    }
    const portY = (col: string) => {
      const card = rowCard.get(col)!
      return rowY(card, rows.get(card)!.indexOf(col))
    }

    // table cards: spread the edge ends along each side, ordered by the other end's height
    const spread = new Map<string, number>()
    if (!columns) {
      const ins = new Map<string, ViewEdge[]>()
      const outs = new Map<string, ViewEdge[]>()
      for (const e of view.edges) {
        if (!ins.has(e.target)) ins.set(e.target, [])
        if (!outs.has(e.source)) outs.set(e.source, [])
        ins.get(e.target)!.push(e)
        outs.get(e.source)!.push(e)
      }
      const cy = (id: string) => { const b = boxes.get(id)!; return b.y + b.h / 2 }
      const place = (m: Map<string, ViewEdge[]>, end: 'in' | 'out') => {
        for (const [id, list] of m) {
          const b = boxes.get(id)
          if (!b) continue
          list.sort((a, c) => cy(end === 'in' ? a.source : a.target) - cy(end === 'in' ? c.source : c.target))
          const span = Math.min(b.h - 16, 6 * (list.length - 1))
          list.forEach((e, i) => spread.set(`${end}:${e.id}`, b.y + b.h / 2 - span / 2 + (list.length > 1 ? (span * i) / (list.length - 1) : 0)))
        }
      }
      place(ins, 'in')
      place(outs, 'out')
    }

    const paths = new Map<string, { d: string; mid: { x: number; y: number } }>()
    let loops = 0
    for (const e of view.edges) {
      const a = boxes.get(e.source), b = boxes.get(e.target)
      if (!a || !b) continue
      // around a focus a loop between two upstream (or two downstream) tables would run backwards
      // across the picture; it is counted instead. The schema overview keeps its loops.
      if (view.anchor && lay.back.has(`${e.source}>${e.target}`)) { loops++; continue }
      const x1 = a.x + a.w, x2 = b.x
      const y1 = e.sourceCol ? portY(e.sourceCol) : spread.get(`out:${e.id}`) ?? a.y + a.h / 2
      const y2 = e.targetCol ? portY(e.targetCol) : spread.get(`in:${e.id}`) ?? b.y + b.h / 2
      paths.set(e.id, { d: curve(x1, y1, x2 - 3, y2), mid: midpoint(x1, y1, x2, y2) })
    }
    return { boxes, width: lay.width, height: lay.height, rows, paths, loops }
  }, [view])
}

/** The ids on every path through `start` (nodes or columns), following the drawn edges. */
function tracePath(view: View, start: string, byColumn: boolean): { ids: Set<string>; edges: Set<string> } {
  const end = (e: ViewEdge, s: 'source' | 'target') => (byColumn ? (s === 'source' ? e.sourceCol : e.targetCol) : e[s]) ?? ''
  const down = new Map<string, ViewEdge[]>()
  const up = new Map<string, ViewEdge[]>()
  for (const e of view.edges) {
    const a = end(e, 'source'), b = end(e, 'target')
    if (!down.has(a)) down.set(a, [])
    if (!up.has(b)) up.set(b, [])
    down.get(a)!.push(e)
    up.get(b)!.push(e)
  }
  const ids = new Set([start])
  const edges = new Set<string>()
  for (const [adj, next] of [[down, 'target'], [up, 'source']] as const) {
    const seen = new Set([start])
    const queue = [start]
    for (let i = 0; i < queue.length; i++) {
      for (const e of adj.get(queue[i]) ?? []) {
        edges.add(e.id)
        const n = end(e, next)
        if (seen.has(n)) continue
        seen.add(n)
        ids.add(n)
        queue.push(n)
      }
    }
  }
  return { ids, edges }
}

interface Tip { x: number; y: number; title: string; body?: string }

export default function LineageCanvas({ view, mode, selectedId, selectedEdgeId, onSelect, onSelectEdge, onFocus, onExpand, onLoops }: Props) {
  const host = useRef<HTMLDivElement>(null)
  const geo = useGeometry(view)
  const [t, setT] = useState({ x: 0, y: 0, k: 1 })
  const tRef = useRef(t)
  tRef.current = t
  const [hover, setHover] = useState<string | null>(null)
  const [tip, setTip] = useState<Tip | null>(null)
  const columns = mode === 'columns'
  const nodeById = useMemo(() => new Map(view.nodes.map((n) => [n.id, n])), [view])

  /** 'all' fits everything; 'focus' centers the focus at a readable size; 'auto' fits unless that gets too small. */
  const fit = useCallback((how: 'auto' | 'all' | 'focus') => {
    const el = host.current
    if (!el || !geo.width) return
    const W = el.clientWidth, H = el.clientHeight, pad = 48
    const k = Math.min(1.1, (W - 2 * pad) / geo.width, (H - 2 * pad - 40) / geo.height)
    const anchor = view.anchor ? geo.boxes.get(view.anchor) : undefined
    if (anchor && (how === 'focus' || (how === 'auto' && k < 0.55))) {
      const kk = Math.max(k, 0.85)
      setT({ k: kk, x: W / 2 - (anchor.x + anchor.w / 2) * kk, y: H / 2 - (anchor.y + anchor.h / 2) * kk })
      return
    }
    const kk = Math.max(0.12, k)
    setT({ k: kk, x: (W - geo.width * kk) / 2, y: (H - geo.height * kk) / 2 + 16 })
  }, [geo, view.anchor])

  useLayoutEffect(() => { fit('auto'); setHover(null); setTip(null) }, [fit])
  useEffect(() => { onLoops?.(geo.loops) }, [geo, onLoops])

  // wheel zoom around the cursor (a native listener: React's wheel handler is passive)
  useEffect(() => {
    const el = host.current
    if (!el) return
    const wheel = (e: WheelEvent) => {
      e.preventDefault()
      const r = el.getBoundingClientRect()
      const mx = e.clientX - r.left, my = e.clientY - r.top
      const cur = tRef.current
      const k = Math.min(2.5, Math.max(0.1, cur.k * Math.exp(-e.deltaY * (e.ctrlKey ? 0.01 : 0.0015))))
      setT({ k, x: mx - (mx - cur.x) * (k / cur.k), y: my - (my - cur.y) * (k / cur.k) })
    }
    el.addEventListener('wheel', wheel, { passive: false })
    return () => el.removeEventListener('wheel', wheel)
  }, [])

  const zoomBy = (f: number) => {
    const el = host.current
    if (!el) return
    const mx = el.clientWidth / 2, my = el.clientHeight / 2
    setT((cur) => {
      const k = Math.min(2.5, Math.max(0.1, cur.k * f))
      return { k, x: mx - (mx - cur.x) * (k / cur.k), y: my - (my - cur.y) * (k / cur.k) }
    })
  }

  // drag the background to pan; a click without a drag clears the selection
  const drag = useRef<{ x: number; y: number; tx: number; ty: number; moved: boolean } | null>(null)
  const onPointerDown = (e: React.PointerEvent) => {
    if (e.button !== 0 || (e.target as Element).closest('[data-hit]')) return
    drag.current = { x: e.clientX, y: e.clientY, tx: t.x, ty: t.y, moved: false }
    ;(e.currentTarget as HTMLElement).setPointerCapture(e.pointerId)
  }
  const onPointerMove = (e: React.PointerEvent) => {
    const d = drag.current
    if (!d) return
    const dx = e.clientX - d.x, dy = e.clientY - d.y
    if (Math.abs(dx) + Math.abs(dy) > 3) d.moved = true
    if (d.moved) setT((cur) => ({ ...cur, x: d.tx + dx, y: d.ty + dy }))
  }
  const onPointerUp = () => {
    if (drag.current && !drag.current.moved) onSelect(null)
    drag.current = null
  }

  const onKey = (e: React.KeyboardEvent) => {
    if ((e.target as Element) !== host.current) return
    if (e.key === '+' || e.key === '=') zoomBy(1.2)
    else if (e.key === '-') zoomBy(1 / 1.2)
    else if (e.key === '0') fit('all')
  }

  // highlight: what the pointer is on, else the selection
  const active = hover ?? selectedId ?? null
  const hi = useMemo(() => {
    if (selectedEdgeId && !hover) {
      const e = view.edges.find((x) => x.id === selectedEdgeId)
      if (!e) return null
      return { ids: new Set([e.source, e.target, e.sourceCol ?? '', e.targetCol ?? '']), edges: new Set([e.id]) }
    }
    if (!active) return null
    if (active.startsWith('edge:')) {
      const e = view.edges.find((x) => x.id === active.slice(5))
      return e ? { ids: new Set([e.source, e.target, e.sourceCol ?? '', e.targetCol ?? '']), edges: new Set([e.id]) } : null
    }
    const byColumn = active.includes('#')
    if (!byColumn && !nodeById.has(active)) return null
    const r = tracePath(view, active, byColumn)
    if (byColumn) for (const e of view.edges) if (r.edges.has(e.id)) { r.ids.add(e.source); r.ids.add(e.target) }
    return r
  }, [active, hover, selectedEdgeId, view, nodeById])
  const dimNode = (id: string) => !!hi && !hi.ids.has(id)
  const dimCol = (id: string) => !!hi && active?.includes('#') === true && !hi.ids.has(id)
  // job names on the lit links, only while there are few enough to read
  const labelEdges = !columns && mode !== 'schemas' && hi && hi.edges.size <= 10

  const showTip = (e: React.MouseEvent, title: string, body?: string) => {
    const r = host.current!.getBoundingClientRect()
    setTip({ x: e.clientX - r.left + 14, y: e.clientY - r.top + 14, title, body })
  }
  const edgeTip = (ev: React.MouseEvent, e: ViewEdge) => {
    const jobs = e.via?.map((v) => v.name) ?? []
    if (e.columnEdges) {
      const expr = e.columnEdges.find((c) => c.expr && c.transform !== 'direct')?.expr
      const how = e.label ? TRANSFORM_TEXT[e.label] ?? e.label : 'copied as is'
      showTip(ev, `${how} · ${e.conf}`, [expr, jobs.length ? `by ${jobs.join(', ')}` : ''].filter(Boolean).join('\n'))
    } else if (e.pairs) {
      showTip(ev, `${e.pairs.length} table flow(s) · ${e.conf}`, `${jobs.length} job(s)`)
    } else {
      showTip(ev, e.kind === 'binds' ? 'semantic table built on this table' : `${jobs.length} job(s) · ${e.conf}${e.config ? ' · config-resolved' : ''}`,
        jobs.slice(0, 8).join('\n') + (jobs.length > 8 ? `\n… and ${jobs.length - 8} more` : ''))
    }
  }

  return (
    <div ref={host} className="lc" tabIndex={0} onKeyDown={onKey}
         onPointerDown={onPointerDown} onPointerMove={onPointerMove} onPointerUp={onPointerUp}
         aria-label={`Lineage graph: ${view.nodes.length} ${view.nodes[0]?.type === 'schema' ? 'schemas' : 'tables'} and ${view.edges.length} flows. Tab through the cards; Enter opens the details. Keys + - 0 zoom.`}>
      <div className="lc-world" style={{ transform: `translate(${t.x}px, ${t.y}px) scale(${t.k})` }}>
        <svg className="lc-edges" width={Math.max(1, geo.width)} height={Math.max(1, geo.height)}>
          <defs>
            <marker id="lc-arrow" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
              <path className="lc-arrow" d="M0,0.5 L8,4 L0,7.5 z" />
            </marker>
            <marker id="lc-arrow-hi" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
              <path className="lc-arrow hi" d="M0,0.5 L8,4 L0,7.5 z" />
            </marker>
          </defs>
          {view.edges.map((e) => {
            const p = geo.paths.get(e.id)
            if (!p) return null
            const lit = !!hi?.edges.has(e.id)
            const sel = e.id === selectedEdgeId
            const cls = ['lc-edge', `c-${e.conf}`, e.kind === 'binds' ? 'binds' : '', e.config ? 'config' : '',
              hi ? (lit ? 'lit' : 'dim') : '', sel ? 'sel' : ''].filter(Boolean).join(' ')
            return (
              <g key={e.id}>
                <path className={cls} d={p.d} markerEnd={`url(#${lit || sel ? 'lc-arrow-hi' : 'lc-arrow'})`} />
                <path className="lc-hit" data-hit="" d={p.d}
                      onClick={(ev) => { ev.stopPropagation(); onSelectEdge(e.id) }}
                      onMouseEnter={(ev) => { setHover(`edge:${e.id}`); edgeTip(ev, e) }}
                      onMouseMove={(ev) => edgeTip(ev, e)}
                      onMouseLeave={() => { setHover(null); setTip(null) }} />
                {columns && e.label ? (
                  <g className={`lc-mark ${hi && !lit ? 'dim' : ''}`} data-hit="" transform={`translate(${p.mid.x},${p.mid.y})`}
                     onClick={(ev) => { ev.stopPropagation(); onSelectEdge(e.id) }}
                     onMouseEnter={(ev) => { setHover(`edge:${e.id}`); edgeTip(ev, e) }}
                     onMouseLeave={() => { setHover(null); setTip(null) }}>
                    <circle r="9" />
                    <text dy="3.5">{MARK[e.label] ?? 'f'}</text>
                  </g>
                ) : null}
                {mode === 'schemas' && e.label ? (
                  <g className={`lc-count ${hi && !lit ? 'dim' : ''}`} transform={`translate(${p.mid.x},${p.mid.y})`}>
                    <rect x={-4 - 3.6 * e.label.length} y="-9" width={8 + 7.2 * e.label.length} height="18" rx="9" />
                    <text dy="4">{e.label}</text>
                  </g>
                ) : null}
              </g>
            )
          })}
        </svg>

        {labelEdges ? view.edges.filter((e) => hi!.edges.has(e.id) && e.label).map((e) => {
          const p = geo.paths.get(e.id)
          return p ? <span key={`l-${e.id}`} className="lc-label" style={{ left: p.mid.x, top: p.mid.y }}>{e.label}</span> : null
        }) : null}

        {view.nodes.map((n) => {
          const b = geo.boxes.get(n.id)
          if (!b) return null
          const [top, name] = splitName(n)
          const cls = ['card', `t-${n.type}`, n.focus ? 'focus' : '', selectedId === n.id ? 'sel' : '', dimNode(n.id) ? 'dim' : '',
            n.columns ? 'with-rows' : ''].filter(Boolean).join(' ')
          return (
            <div key={n.id} className={cls} style={{ left: b.x, top: b.y, width: b.w, height: b.h }}
                 data-hit="" role="button" tabIndex={0} aria-pressed={selectedId === n.id}
                 aria-label={`${n.label}${n.focus ? ' (focus)' : ''}`}
                 onClick={(e) => { e.stopPropagation(); onSelect(n.id) }}
                 onDoubleClick={(e) => { e.stopPropagation(); if (n.type !== 'schema') onFocus(n.id) }}
                 onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onSelect(n.id) } }}
                 onMouseEnter={() => setHover(n.id)} onMouseLeave={() => setHover(null)}>
              <div className="card-head" title={n.label}>
                <span className="card-icon"><Icon type={n.type} /></span>
                <span className="card-text">
                  {top ? <span className="card-top">{top}</span> : null}
                  <span className="card-name">{name}</span>
                </span>
                {n.warning ? <span className="card-warn" title={n.warning} aria-label={`Warning: ${n.warning}`}>!</span> : null}
                {n.missing ? <span className="card-missing" title="Not in the lakehouse: the code uses this table, but the schema export does not list it">missing</span> : null}
                {n.focus ? <span className="card-pill">focus</span> : null}
              </div>
              {n.columns ? (
                <div className="card-rows">
                  {(geo.rows.get(n.id) ?? []).map((cid) => {
                    const c = n.columns!.find((x) => x.id === cid)!
                    const rcls = ['card-row', c.focus ? 'focus' : '', selectedId === cid ? 'sel' : '', dimCol(cid) ? 'dim' : ''].filter(Boolean).join(' ')
                    return (
                      <div key={cid} className={rcls} role="button" tabIndex={0} title={`${n.label}.${c.name} · double-click to trace from here`}
                           onClick={(e) => { e.stopPropagation(); onSelect(cid) }}
                           onDoubleClick={(e) => { e.stopPropagation(); onFocus(cid) }}
                           onKeyDown={(e) => { if (e.key === 'Enter') { e.stopPropagation(); e.preventDefault(); onFocus(cid) } }}
                           onMouseEnter={(e) => { e.stopPropagation(); setHover(cid) }}
                           onMouseLeave={(e) => { e.stopPropagation(); setHover(n.id) }}>
                        <span className="row-name">{c.name}</span>
                      </div>
                    )
                  })}
                  {n.otherColumns ? <div className="card-foot">+ {n.otherColumns} other column{n.otherColumns === 1 ? '' : 's'}</div> : null}
                </div>
              ) : null}
              {onExpand && n.more?.up ? (
                <button className="lc-more left" data-hit="" title={`Show ${n.more.up} more upstream table(s)`}
                        onClick={(e) => { e.stopPropagation(); onExpand(n.id, 'up') }} onDoubleClick={(e) => e.stopPropagation()}>+{n.more.up}</button>
              ) : null}
              {onExpand && n.more?.down ? (
                <button className="lc-more right" data-hit="" title={`Show ${n.more.down} more downstream table(s)`}
                        onClick={(e) => { e.stopPropagation(); onExpand(n.id, 'down') }} onDoubleClick={(e) => e.stopPropagation()}>+{n.more.down}</button>
              ) : null}
            </div>
          )
        })}
      </div>

      {view.anchor && geo.boxes.get(view.anchor) ? (
        <div className="lc-sides" aria-hidden="true">
          <span>← upstream</span><span>downstream →</span>
        </div>
      ) : null}

      <div className="lc-zoom" data-hit="">
        <button className="btn icon" onClick={() => zoomBy(1.25)} aria-label="Zoom in" title="Zoom in (+)">+</button>
        <button className="btn icon" onClick={() => zoomBy(0.8)} aria-label="Zoom out" title="Zoom out (−)">−</button>
        <button className="btn" onClick={() => fit('all')} title="Fit everything (0)">Fit</button>
        {view.anchor ? <button className="btn" onClick={() => fit('focus')} title="Center on the focus">Focus</button> : null}
      </div>

      {tip ? (
        <div className="lc-tip" style={{ left: tip.x, top: tip.y }} role="tooltip">
          <strong>{tip.title}</strong>
          {tip.body ? <pre>{tip.body}</pre> : null}
        </div>
      ) : null}
    </div>
  )
}
