// Name-based exclusions for tables (views, temp and backup tables, whole schemas ...).
//
// The exclusions are a list of rules, each switched on or off. A rule holds one or more
// patterns, matched case-insensitively against the full `schema.table` name. Without a `*`
// a pattern is a substring match ("vw_" hides every table with vw_ in its name); with `*` it
// is a wildcard ("silver_ml.*", "*_tmp"). The list is kept in the browser between visits.

export interface ExcludeRule {
  id: string
  label: string
  patterns: string[]
  on: boolean
  preset?: boolean
}

export interface ExcludeSettings {
  rules: ExcludeRule[]
  keep?: string[]          // always shown, whatever the rules say (exact names or patterns)
}

/** Names a build always shows (VITE_ALWAYS_SHOW, comma separated), e.g. for one team's exceptions. */
const BUILD_KEEP: string[] = splitPatterns(String(import.meta.env?.VITE_ALWAYS_SHOW ?? ''))

export const PRESETS: ExcludeRule[] = [
  { id: 'views', label: 'views', patterns: ['vw_'], on: true, preset: true },
  { id: 'tmp', label: 'temp tables', patterns: ['_tmp', 'tmp_'], on: true, preset: true },
  { id: 'bkp', label: 'backups', patterns: ['_bkp', 'bkp_', '_bak', '_backup'], on: true, preset: true },
  { id: 'ml', label: 'silver_ml schema', patterns: ['silver_ml.'], on: true, preset: true },
]

export function defaultExclude(): ExcludeSettings {
  return { rules: PRESETS.map((p) => ({ ...p, patterns: [...p.patterns] })), keep: [...BUILD_KEEP] }
}

export const addKeep = (s: ExcludeSettings, text: string): ExcludeSettings =>
  ({ ...s, keep: [...new Set([...(s.keep ?? []), ...splitPatterns(text)])] })

export const removeKeep = (s: ExcludeSettings, pattern: string): ExcludeSettings =>
  ({ ...s, keep: (s.keep ?? []).filter((k) => k !== pattern) })

/** Splits "a, b\nc" into clean, lower-case patterns. */
export function splitPatterns(text: string): string[] {
  return [...new Set(text.split(/[,\n]/).map((x) => x.trim().toLowerCase()).filter(Boolean))]
}

/** Rules for free-text patterns, one rule per pattern (used by tests and the "add" box). */
export function rulesFor(text: string): ExcludeRule[] {
  return splitPatterns(text).map((p) => ({ id: `custom:${p}`, label: p, patterns: [p], on: true }))
}

/** Adds patterns as new rules; a pattern already in the list is switched back on instead. */
export function addPatterns(s: ExcludeSettings, text: string): ExcludeSettings {
  const rules = s.rules.map((r) => ({ ...r }))
  const keep = s.keep
  for (const rule of rulesFor(text)) {
    const same = rules.find((r) => r.patterns.length === rule.patterns.length && r.patterns.every((p, i) => p === rule.patterns[i]))
    if (same) same.on = true
    else rules.push(rule)
  }
  return { rules, keep }
}

export const toggleRule = (s: ExcludeSettings, id: string): ExcludeSettings =>
  ({ ...s, rules: s.rules.map((r) => (r.id === id ? { ...r, on: !r.on } : r)) })

export const removeRule = (s: ExcludeSettings, id: string): ExcludeSettings =>
  ({ ...s, rules: s.rules.filter((r) => r.id !== id) })

export function activePatterns(s: ExcludeSettings): string[] {
  return s.rules.filter((r) => r.on).flatMap((r) => r.patterns).map((p) => p.toLowerCase())
}

export type Matcher = (name: string) => boolean

/** Hidden by the rules and not on the always-show list. */
export function compileSettings(s: ExcludeSettings): Matcher {
  const hide = compile(activePatterns(s))
  const keep = compile((s.keep ?? []).map((k) => k.toLowerCase()))
  return (name) => hide(name) && !keep(name)
}

export function compile(patterns: string[]): Matcher {
  const tests = patterns.map((raw): ((n: string) => boolean) => {
    const p = raw.toLowerCase()
    if (!p.includes('*')) return (n) => n.includes(p)
    const rx = new RegExp(`^${p.split('*').map((s) => s.replace(/[.+?^${}()|[\]\\]/g, '\\$&')).join('.*')}$`)
    return (n) => rx.test(n)
  })
  return (name) => {
    const n = name.toLowerCase()
    return tests.some((t) => t(n))
  }
}

export const never: Matcher = () => false

// ---------------------------------------------------------------------------------------
// persistence (per browser; a missing or broken entry falls back to the defaults)

const KEY = 'columntrace.exclude.v1'

function isRule(x: unknown): x is ExcludeRule {
  const r = x as ExcludeRule
  return !!r && typeof r.id === 'string' && typeof r.label === 'string' && typeof r.on === 'boolean'
    && Array.isArray(r.patterns) && r.patterns.every((p) => typeof p === 'string')
}

/** The viewer's saved rules, or ``fallback`` when there are none or they were saved against
 *  different team defaults (``stamp``): a change to the team file reaches everyone. */
export function loadExclude(storage: Pick<Storage, 'getItem'> | undefined = safeStorage(),
                            fallback: ExcludeSettings = defaultExclude(), stamp = ''): ExcludeSettings {
  try {
    const raw = storage?.getItem(KEY)
    if (!raw) return fallback
    const saved = JSON.parse(raw) as { rules?: unknown[]; keep?: unknown[]; stamp?: unknown }
    if ((typeof saved.stamp === 'string' ? saved.stamp : '') !== stamp) return fallback
    const rules = saved.rules
    if (!Array.isArray(rules) || !rules.every(isRule)) return fallback
    const keep = Array.isArray(saved.keep) ? saved.keep.filter((k): k is string => typeof k === 'string') : []
    return { rules, keep: [...new Set([...BUILD_KEEP, ...keep])] }
  } catch {
    return fallback
  }
}

export function saveExclude(s: ExcludeSettings, storage: Pick<Storage, 'setItem'> | undefined = safeStorage(), stamp = ''): void {
  try {
    storage?.setItem(KEY, JSON.stringify({ rules: s.rules, keep: s.keep ?? [], stamp }))
  } catch {
    // private windows and blocked storage: the list just is not remembered
  }
}

function safeStorage(): Storage | undefined {
  try {
    return typeof window === 'undefined' ? undefined : window.localStorage
  } catch {
    return undefined
  }
}
