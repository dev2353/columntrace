// Where the user is: the view, the focus table (or job) and the traced column. Kept in the URL
// hash, so Back / Forward (the app's buttons, the browser's, Alt+arrow) step through what was
// looked at, and a link opens the same place.

import { colId, parseColId, type ColRef } from './colgraph'
import type { Mode } from './view'

export interface Nav {
  mode: Mode
  focus: string | null         // null in the tables view: the "All tables" page
  col: ColRef | null
}

export const HOME: Nav = { mode: 'tables', focus: null, col: null }

export function navKey(n: Nav): string {
  return [n.mode, n.mode === 'schemas' ? '' : n.focus ?? '', n.mode === 'columns' && n.col ? colId(n.col) : ''].join('|')
}

export function toHash(n: Nav): string {
  const p = new URLSearchParams({ view: n.mode })
  if (n.mode === 'columns' && n.col) p.set('col', colId(n.col))
  else if (n.mode !== 'schemas' && n.focus) p.set('focus', n.focus)
  return `#${p.toString()}`
}

export function parseHash(hash: string): Nav | null {
  const p = new URLSearchParams(hash.replace(/^#/, ''))
  const view = p.get('view')
  if (view !== 'tables' && view !== 'columns' && view !== 'schemas') return null
  const col = p.get('col')
  if (view === 'columns') return col && col.includes('#') ? { mode: 'columns', focus: parseColId(col).table, col: parseColId(col) } : null
  return { mode: view, focus: view === 'schemas' ? null : p.get('focus'), col: null }
}
