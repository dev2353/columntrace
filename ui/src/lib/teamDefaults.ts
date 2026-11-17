// Team defaults: one small JSON file published next to lineage.json (team-defaults.json), so
// everyone who opens the page starts from the same settings. Each viewer can still change the
// filters; their own name rules are remembered until the team file changes.
//
//   {
//     "start": "silver_sales.orders",                 first table shown (default: the busiest)
//     "depth": "auto" | { "up": 2, "down": 2 },       starting depth for every table
//     "tablesPerSide": 50,                            budget per side for "auto"
//     "hide": { "views": true, "tmp": true, "bkp": true, "ml": true, "patterns": ["staging."] },
//     "alwaysShow": ["silver_x.tmp_orders"],          shown even when a rule hides them
//     "jobs": "all" | "active" | ["scheduled", "orchestrated"],
//     "evidence": ["parsed", "inferred", "unresolved"],
//     "configResolved": false,
//     "show": { "semanticTables": true, "unresolvedNames": false },
//     "broadJobPairs": 24
//   }
//
// Every key is optional; anything left out keeps the built-in default.

import { addKeep, addPatterns, defaultExclude, PRESETS, type ExcludeSettings } from './exclude'
import type { Conf, Liveness } from './types'
import { ACTIVE_LIVENESS, ALL_LIVENESS, defaultFilters, SIDE_BUDGET, type Filters, type TableType } from './view'

export interface TeamDefaults {
  start?: string
  depth?: 'auto' | { up: number; down: number }
  tablesPerSide?: number
  hide?: Partial<Record<string, boolean>> & { patterns?: string[] }
  alwaysShow?: string[]
  jobs?: 'all' | 'active' | Liveness[]
  evidence?: Conf[]
  configResolved?: boolean
  show?: { semanticTables?: boolean; unresolvedNames?: boolean }
  broadJobPairs?: number
}

const CONFS: Conf[] = ['parsed', 'inferred', 'unresolved']
const isInt = (n: unknown, lo: number, hi: number): n is number => Number.isInteger(n) && (n as number) >= lo && (n as number) <= hi

/** Reads a team-defaults document, keeping only the keys it understands; problems are listed, not thrown. */
export function parseTeamDefaults(raw: unknown): { defaults: TeamDefaults; problems: string[] } {
  const d: TeamDefaults = {}
  const problems: string[] = []
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return { defaults: d, problems: ['not a JSON object'] }
  const r = raw as Record<string, unknown>
  if (typeof r.start === 'string' && r.start.trim()) d.start = r.start.trim().toLowerCase()
  if (r.depth === 'auto') d.depth = 'auto'
  else if (r.depth && typeof r.depth === 'object') {
    const { up, down } = r.depth as Record<string, unknown>
    if (isInt(up, 0, 8) && isInt(down, 0, 8)) d.depth = { up, down }
    else problems.push('depth: up and down must be whole numbers from 0 to 8')
  }
  if (isInt(r.tablesPerSide, 5, 300)) d.tablesPerSide = r.tablesPerSide
  else if (r.tablesPerSide !== undefined) problems.push('tablesPerSide: a whole number from 5 to 300')
  if (r.hide && typeof r.hide === 'object') {
    const h = r.hide as Record<string, unknown>
    d.hide = {}
    for (const p of PRESETS) if (typeof h[p.id] === 'boolean') d.hide[p.id] = h[p.id] as boolean
    if (Array.isArray(h.patterns)) d.hide.patterns = h.patterns.filter((x): x is string => typeof x === 'string')
  }
  if (Array.isArray(r.alwaysShow)) d.alwaysShow = r.alwaysShow.filter((x): x is string => typeof x === 'string')
  if (r.jobs === 'all' || r.jobs === 'active') d.jobs = r.jobs
  else if (Array.isArray(r.jobs)) d.jobs = r.jobs.filter((x): x is Liveness => ALL_LIVENESS.includes(x as Liveness))
  if (Array.isArray(r.evidence)) d.evidence = r.evidence.filter((x): x is Conf => CONFS.includes(x as Conf))
  if (typeof r.configResolved === 'boolean') d.configResolved = r.configResolved
  if (r.show && typeof r.show === 'object') {
    const s = r.show as Record<string, unknown>
    d.show = {}
    if (typeof s.semanticTables === 'boolean') d.show.semanticTables = s.semanticTables
    if (typeof s.unresolvedNames === 'boolean') d.show.unresolvedNames = s.unresolvedNames
  }
  if (isInt(r.broadJobPairs, 1, 10000)) d.broadJobPairs = r.broadJobPairs
  return { defaults: d, problems }
}

/** The name rules the team starts from. */
export function teamExclude(d: TeamDefaults): ExcludeSettings {
  let s = defaultExclude()
  if (d.hide) s = { ...s, rules: s.rules.map((r) => (r.preset && typeof d.hide![r.id] === 'boolean' ? { ...r, on: d.hide![r.id]! } : r)) }
  if (d.hide?.patterns?.length) s = addPatterns(s, d.hide.patterns.join(','))
  if (d.alwaysShow?.length) s = addKeep(s, d.alwaysShow.join(','))
  return s
}

/** The filters the team starts from (depth is decided per table, see {@link startDepth}). */
export function teamFilters(d: TeamDefaults): Filters {
  const f = defaultFilters(teamExclude(d))
  if (d.jobs === 'active') f.liveness = new Set(ACTIVE_LIVENESS)
  else if (Array.isArray(d.jobs)) f.liveness = new Set(d.jobs)
  if (d.evidence?.length) f.conf = new Set(d.evidence)
  if (d.configResolved !== undefined) f.showConfig = d.configResolved
  if (d.show) {
    const types = new Set<TableType>(f.types)
    if (d.show.semanticTables === false) types.delete('semantic_table')
    if (d.show.semanticTables === true) types.add('semantic_table')
    if (d.show.unresolvedNames === true) types.add('unknown')
    if (d.show.unresolvedNames === false) types.delete('unknown')
    f.types = types
  }
  if (d.broadJobPairs) f.maxPairs = d.broadJobPairs
  return f
}

/** Fixed starting depth, or null for "auto" (then {@link perSide} is the budget). */
export const fixedDepth = (d: TeamDefaults): { up: number; down: number } | null =>
  (d.depth && d.depth !== 'auto' ? d.depth : null)
export const perSide = (d: TeamDefaults): number => d.tablesPerSide ?? SIDE_BUDGET

/** A short fingerprint of the team's name rules: a viewer's saved rules apply only while it matches. */
export function stamp(d: TeamDefaults): string {
  const text = JSON.stringify([d.hide ?? null, d.alwaysShow ?? null])
  let h = 0
  for (let i = 0; i < text.length; i++) h = (h * 31 + text.charCodeAt(i)) | 0
  return (h >>> 0).toString(36)
}
