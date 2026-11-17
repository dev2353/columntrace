import { describe, expect, it } from 'vitest'
import sample from '../../public/sample-lineage.json'
import { colId, ColumnGraph, parseColId, type ColRef } from './colgraph'
import {
  activePatterns, addKeep, addPatterns, compile, compileSettings, defaultExclude, removeKeep, loadExclude, PRESETS, removeRule, rulesFor, saveExclude, toggleRule,
} from './exclude'
import { layered, orderRows } from './layout'
import { HOME, navKey, parseHash, toHash } from './nav'
import { fixedDepth, parseTeamDefaults, perSide, stamp, teamExclude, teamFilters } from './teamDefaults'
import { FlowGraph } from './flow'
import { buildImpact, csvEscape, toCsv, toMarkdown } from './report'
import { contract } from './tablegraph'
import type { Lineage } from './types'
import { familyOf, usedBy } from './usage'
import {
  autoColumnDepth, autoDepth, buildAnyView, buildColumnView, buildJobView, buildSchemaView, buildTableView,
  changedFilters, columnCount, defaultFilters, matcherFor, type Filters,
} from './view'

const fg = new FlowGraph(sample as unknown as Lineage)
const id = (q: string) => {
  const r = fg.resolve(q)
  if (!r) throw new Error(`not found: ${q}`)
  return r
}
const names = (rows: { name: string }[]) => new Set(rows.map((r) => r.name))
/** Filters with no name exclusions, so the fixture tables are all visible. */
const open = (patch: Partial<Filters> = {}): Filters => ({
  ...defaultFilters(), exclude: { rules: [] }, up: 2, down: 2, ...patch,
})

describe('FlowGraph', () => {
  it('resolves bare table names and prefers exact matches in search', () => {
    expect(id('bronze_shop.orders')).toBe('table:bronze_shop.orders')
    expect(fg.search('silver_shop.orders')[0].name).toBe('silver_shop.orders')
    expect(fg.search('')).toEqual([])
  })

  it('suggests busy data tables as hubs, never config tables', () => {
    const hubs = fg.hubs(20).map((n) => n.name)
    expect(hubs.length).toBeGreaterThan(3)
    expect(hubs.some((n) => n.startsWith('cfg.'))).toBe(false)
    expect(hubs[0]).toBe('silver_shop.orders')
  })

  it('leaves excluded tables out of the hub suggestions', () => {
    const hubs = fg.hubs(20, compile(['silver_shop.'])).map((n) => n.name)
    expect(hubs.some((n) => n.startsWith('silver_shop.'))).toBe(false)
  })

  it('walks downstream through jobs, tables, semantic tables and calling pipelines', () => {
    const down = names(fg.walk(id('bronze_shop.orders'), 'down'))
    for (const n of ['silver_shop.orders', 'gold_shop.revenue_daily', 'Shop Model.revenue_daily', 'pl_main']) {
      expect(down.has(n)).toBe(true)
    }
    expect(down.has('nb_dynamic_export')).toBe(false) // only an unresolved read: never claimed
    expect(down.has('nb_ingest_orders')).toBe(false)  // a sibling job, not downstream
  })

  it('walks upstream from a semantic table', () => {
    const up = names(fg.walk(id('Shop Model.revenue_daily'), 'up'))
    for (const n of ['gold_shop.revenue_daily', 'nb_gold_revenue', 'silver_shop.orders', 'bronze_shop.orders']) {
      expect(up.has(n)).toBe(true)
    }
  })

  it('strict (parsed-only) results are a subset of the loose ones', () => {
    const start = id('bronze_shop.customers')
    const strict = fg.walk(start, 'down', { allowConf: (c) => c === 'parsed' })
    const loose = fg.walk(start, 'down', { allowConf: (c) => c !== 'unresolved' })
    expect(strict.length).toBeLessThanOrEqual(loose.length)
    expect(strict.every((r) => r.confidence === 'parsed')).toBe(true)
  })
})

describe('name exclusions', () => {
  it('matches substrings and wildcards case-insensitively', () => {
    const m = compile(['vw_', '*_TMP', 'silver_ml.*'])
    expect(m('gold_x.vw_report')).toBe(true)
    expect(m('bronze_x.load_tmp')).toBe(true)
    expect(m('silver_ml.model_1')).toBe(true)
    expect(m('silver_sales.orders')).toBe(false)
    expect(compile([])('anything')).toBe(false)
  })

  it('ships presets for views, temp, backups and silver_ml, all on by default', () => {
    expect(PRESETS.map((p) => p.id)).toEqual(['views', 'tmp', 'bkp', 'ml'])
    const patterns = activePatterns(defaultExclude())
    for (const p of ['vw_', '_tmp', 'tmp_', '_bkp', '_bak', 'silver_ml.']) expect(patterns).toContain(p)
  })

  it('adds custom patterns split on commas and newlines', () => {
    const p = activePatterns({ rules: rulesFor('staging, *_old\nfoo') })
    expect(p).toEqual(['staging', '*_old', 'foo'])
  })

  it('keeps an editable list: added patterns become rules that can be switched off and removed', () => {
    let s = addPatterns(defaultExclude(), 'Staging, *_old')
    expect(s.rules.map((r) => r.label).slice(-2)).toEqual(['staging', '*_old'])
    expect(activePatterns(s)).toContain('staging')
    s = toggleRule(s, 'custom:staging')
    expect(activePatterns(s)).not.toContain('staging')
    expect(s.rules.some((r) => r.id === 'custom:staging')).toBe(true)      // still listed, just off
    s = addPatterns(s, 'staging')                                         // adding it again switches it back on
    expect(s.rules.filter((r) => r.id === 'custom:staging')).toHaveLength(1)
    expect(activePatterns(s)).toContain('staging')
    s = removeRule(removeRule(s, 'views'), 'custom:*_old')
    expect(activePatterns(s)).not.toContain('vw_')
    expect(activePatterns(s)).not.toContain('*_old')
  })

  it('shows tables on the always-show list even when a rule hides them, and remembers the list', () => {
    let s = defaultExclude()
    expect(compileSettings(s)('silver_x.tmp_orders')).toBe(true)                // the temp-tables rule hides it
    s = addKeep(s, 'silver_x.tmp_orders')
    expect(compileSettings(s)('silver_x.tmp_orders')).toBe(false)               // ...unless it is kept
    expect(compileSettings(s)('silver_x.tmp_other')).toBe(true)
    const mem = new Map<string, string>()
    const store = { getItem: (k: string) => mem.get(k) ?? null, setItem: (k: string, v: string) => void mem.set(k, v) }
    saveExclude(s, store)
    expect(loadExclude(store).keep).toEqual(['silver_x.tmp_orders'])
    expect(removeKeep(s, 'silver_x.tmp_orders').keep).toEqual([])
    // the graph follows it: a hidden staging table appears once kept
    const f = open({ up: 4, down: 4, exclude: { rules: rulesFor('staging.') } })
    expect(buildTableView(fg, id('bronze_shop.orders'), f).nodes.some((n) => n.label === 'staging.orders_raw')).toBe(false)
    const kept = { ...f, exclude: { rules: rulesFor('staging.'), keep: ['staging.orders_raw'] } }
    expect(buildTableView(fg, id('bronze_shop.orders'), kept).nodes.some((n) => n.label === 'staging.orders_raw')).toBe(true)
  })

  it('remembers the list in storage and falls back to the defaults on bad data', () => {
    const mem = new Map<string, string>()
    const store = { getItem: (k: string) => mem.get(k) ?? null, setItem: (k: string, v: string) => void mem.set(k, v) }
    saveExclude(addPatterns(defaultExclude(), 'staging'), store)
    expect(activePatterns(loadExclude(store))).toContain('staging')
    mem.set([...mem.keys()][0], '{"rules":[{"id":1}]}')
    expect(loadExclude(store)).toEqual(defaultExclude())
    mem.set([...mem.keys()][0], 'not json')
    expect(loadExclude(store)).toEqual(defaultExclude())
    const broken = { getItem: () => { throw new Error('blocked') }, setItem: () => { throw new Error('blocked') } }
    expect(loadExclude(broken)).toEqual(defaultExclude())
    expect(() => saveExclude(defaultExclude(), broken)).not.toThrow()
  })

  it('hides matching tables from the table view and counts them, keeping the focus', () => {
    const wide = buildTableView(fg, id('silver_shop.orders'), open({ up: 4, down: 4 }))
    expect(wide.nodes.some((n) => n.label === 'gold_shop.orders_mlv')).toBe(true)
    const f = open({ up: 4, down: 4, exclude: { rules: rulesFor('_mlv, gold_shop.stale') } })
    const cut = buildTableView(fg, id('silver_shop.orders'), f)
    expect(cut.nodes.some((n) => n.label === 'gold_shop.orders_mlv')).toBe(false)
    expect(cut.hiddenExcluded).toBeGreaterThanOrEqual(2)                      // counted, never dropped silently
    const self = buildTableView(fg, id('silver_shop.orders'), { ...f, exclude: { rules: rulesFor('silver_shop.orders') } })
    expect(self.nodes.some((n) => n.focus)).toBe(true)                        // the focus always stays
  })

  it('does not route lineage through an excluded table', () => {
    // bronze_shop.orders -> silver_shop.orders -> gold_shop.revenue_daily: exclude the middle
    const f = open({ up: 0, down: 3, exclude: { rules: rulesFor('silver_shop.orders') } })
    const v = buildTableView(fg, id('bronze_shop.orders'), f)
    expect(v.nodes.some((n) => n.label === 'gold_shop.revenue_daily')).toBe(false)
  })

  it('removes excluded schemas from the overview', () => {
    const f = open({ exclude: { rules: rulesFor('silver_shop.') } })
    const v = buildSchemaView(fg, f)
    expect(v.nodes.some((n) => n.id === 'schema:silver_shop')).toBe(false)
    expect(v.nodes.some((n) => n.id === 'schema:gold_shop')).toBe(true)
    expect(v.hiddenExcluded).toBeGreaterThan(0)
  })

  it('caches matchers by pattern set', () => {
    expect(matcherFor(open())).toBe(matcherFor(open()))
  })
})

describe('tables view (jobs folded into edges)', () => {
  const edge = (v: ReturnType<typeof buildTableView>, a: string, b: string) =>
    v.edges.find((e) => e.source === `table:${a}` && e.target === `table:${b}`)

  it('shows only data nodes and attributes each flow to the jobs that move it', () => {
    const v = buildTableView(fg, id('silver_shop.orders'), open())
    expect(v.nodes.every((n) => ['table', 'semantic_table', 'unknown'].includes(n.type))).toBe(true)
    const e = edge(v, 'bronze_shop.orders', 'silver_shop.orders')!
    expect(e.via?.map((j) => j.name).sort()).toEqual(['nb_silver_orders', 'nb_silver_orders_python'])
    expect(e.label).toBe('2 jobs')
    expect(edge(v, 'silver_shop.orders', 'gold_shop.revenue_daily')?.label).toBe('nb_gold_revenue')
  })

  it('counts depth in table hops: one hop is one job', () => {
    const one = buildTableView(fg, id('bronze_shop.orders'), open({ up: 0, down: 1 }))
    expect(one.nodes.some((n) => n.label === 'silver_shop.orders')).toBe(true)
    expect(one.nodes.some((n) => n.label === 'gold_shop.revenue_daily')).toBe(false)   // two jobs away
    expect(buildTableView(fg, id('bronze_shop.orders'), open({ up: 0, down: 2 })).nodes.some((n) => n.label === 'gold_shop.revenue_daily')).toBe(true)
  })

  it('includes semantic tables via binds edges', () => {
    const v = buildTableView(fg, id('gold_shop.revenue_daily'), open({ up: 0, down: 1 }))
    expect(v.edges.find((e) => e.kind === 'binds')?.target).toBe('semantic_table:Shop Model.revenue_daily')
  })

  it('applies liveness filters to the jobs, removing the edges they carry', () => {
    const f = open()
    expect(edge(buildTableView(fg, id('silver_shop.orders'), f), 'silver_shop.orders', 'gold_shop.orders_mlv')).toBeDefined()
    const noDead = open({ liveness: new Set(f.liveness) })
    noDead.liveness.delete('dead-end')   // nb_mlv_orders is a dead-end notebook
    expect(edge(buildTableView(fg, id('silver_shop.orders'), noDead), 'silver_shop.orders', 'gold_shop.orders_mlv')).toBeUndefined()
    const noOrch = open({ liveness: new Set(f.liveness) })
    noOrch.liveness.delete('orchestrated')   // nb_gold_revenue
    expect(edge(buildTableView(fg, id('silver_shop.orders'), noOrch), 'silver_shop.orders', 'gold_shop.revenue_daily')).toBeUndefined()
  })

  it('keeps jobs that write few tables, even over the pair limit', () => {
    const c = contract(fg, { allowItem: () => true, allowConf: () => true, includeConfig: false, maxPairs: 1 })
    // nb_silver_orders reads two tables and writes one: not a generic loader, so all its pairs stay
    expect(c.skipped.map((s) => s.name)).not.toContain('nb_silver_orders')
    expect(c.edges.find((x) => x.from === 'table:bronze_shop.orders' && x.to === 'table:silver_shop.orders')).toBeDefined()
    expect(contract(fg, { allowItem: () => true, allowConf: () => true, includeConfig: false }).skipped).toHaveLength(0)
    expect(buildTableView(fg, id('silver_shop.orders'), open({ maxPairs: 1 })).skippedJobs).toBe(c.skipped.length)   // the view reports what the folding skipped
    expect(buildTableView(fg, id('silver_shop.orders'), open({ maxPairs: 500 })).skippedJobs).toBe(0)
  })

  it('hides unresolved nodes by default but counts them, and shows them when switched on', () => {
    const f = open({ up: 3, down: 3 })
    expect(f.types.has('unknown')).toBe(false)
    const hidden = buildTableView(fg, id('silver_shop.orders'), f)
    expect(hidden.nodes.some((n) => n.type === 'unknown')).toBe(false)
    expect(hidden.hiddenUnresolved).toBeGreaterThan(0)
    const shown = buildTableView(fg, id('silver_shop.orders'), { ...f, types: new Set(f.types).add('unknown') })
    expect(shown.nodes.some((n) => n.type === 'unknown')).toBe(true)
    expect(shown.hiddenUnresolved).toBe(0)
  })

  it('hides config-resolved edges unless asked', () => {
    const f = open({ up: 2, down: 2 })
    const hidden = buildTableView(fg, id('bronze_shop.orders'), f)
    expect(hidden.edges.some((e) => e.config)).toBe(false)
    expect(buildTableView(fg, id('bronze_shop.orders'), { ...f, showConfig: true }).edges.some((e) => e.config)).toBe(true)
  })

  it('caps nodes by distance and edges by strength, and says so', () => {
    const nodes = buildTableView(fg, id('bronze_shop.orders'), open({ maxNodes: 3, up: 5, down: 5 }))
    expect(nodes.nodes.length).toBeLessThanOrEqual(3)
    expect(nodes.truncated && nodes.hidden > 0).toBe(true)
    expect(nodes.nodes.some((n) => n.focus)).toBe(true)
    const f = open({ showConfig: true, down: 4, up: 4, maxEdges: 3 })
    const v = buildTableView(fg, id('bronze_shop.orders'), f)
    expect(v.edges).toHaveLength(3)
    expect(v.hiddenEdges).toBeGreaterThan(0)
    expect(v.edges.every((e) => !e.config)).toBe(true)   // config-resolved edges are dropped first
  })

  it('returns nothing for an unknown or non-table focus', () => {
    expect(buildTableView(fg, 'table:nope', open()).nodes).toEqual([])
    expect(buildTableView(fg, 'notebook:nb_silver_orders', open()).nodes).toEqual([])
  })

  it('picks a starting depth per side: at least one hop each, deeper while within budget', () => {
    const focus = id('silver_shop.orders')
    const side = (up: number, down: number) => buildTableView(fg, focus, { ...open(), up, down }).nodes.length - 1
    const roomy = autoDepth(fg, focus, open(), 1000)
    expect(side(roomy.up, 0)).toBe(side(roomy.up + 1, 0))          // deep enough: one more hop adds nothing
    expect(side(roomy.up - 1, 0)).toBeLessThan(side(roomy.up, 0))  // ...and not deeper than needed
    const tight = autoDepth(fg, focus, open(), 1)
    expect(tight).toEqual({ up: 1, down: 1 })                       // the first hop always shows
  })
})

describe('lakehouse comparison', () => {
  it('marks extracted tables the schema export does not list, and only those', () => {
    const v = buildTableView(fg, id('silver_shop.orders'), open({ up: 3, down: 3 }))
    const missing = v.nodes.filter((n) => n.missing).map((n) => n.label)
    const present = v.nodes.filter((n) => !n.missing).map((n) => n.label)
    expect(missing.length).toBeGreaterThan(0)
    expect(present).toContain('silver_shop.orders')
    for (const name of missing) expect(fg.nodes.get(`table:${name}`)?.meta.inLakehouse).toBe(false)
  })
})

describe('job lens', () => {
  it('shows the tables one job reads and writes, with its inputs flowing to its outputs', () => {
    const v = buildJobView(fg, id('nb_silver_orders'), open())
    expect(v.job?.name).toBe('nb_silver_orders')
    expect(new Set(v.nodes.map((n) => n.label))).toEqual(new Set(['bronze_shop.orders', 'bronze_shop.customers', 'silver_shop.orders']))
    expect(v.edges.map((e) => `${e.source}>${e.target}`).sort()).toEqual([
      'table:bronze_shop.customers>table:silver_shop.orders', 'table:bronze_shop.orders>table:silver_shop.orders',
    ])
  })

  it('works for pipelines (Copy activities) and applies the exclusions', () => {
    const v = buildJobView(fg, id('pl_copy'), open())
    expect(v.nodes.map((n) => n.label)).toContain('dwh_stage.revenue_daily')
    const f = open({ exclude: { rules: rulesFor('dwh_stage') } })
    const cut = buildJobView(fg, id('pl_copy'), f)
    expect(cut.nodes.some((n) => n.label.startsWith('dwh_stage'))).toBe(false)
    expect(cut.hiddenExcluded).toBeGreaterThan(0)
  })

  it('is what buildAnyView returns when the focus is a job', () => {
    expect(buildAnyView(fg, id('nb_silver_orders'), open()).job?.name).toBe('nb_silver_orders')
    expect(buildAnyView(fg, id('silver_shop.orders'), open()).job).toBeUndefined()
  })
})

describe('schemas view (warehouse overview)', () => {
  it('has one node per schema with table counts and aggregated flows between schemas', () => {
    const v = buildSchemaView(fg, open())
    const labels = v.nodes.map((n) => n.label)
    for (const s of ['bronze_shop', 'silver_shop', 'gold_shop']) expect(labels.some((l) => l.startsWith(`${s} (`))).toBe(true)
    expect(v.nodes.every((n) => n.type === 'schema')).toBe(true)
    const e = v.edges.find((x) => x.source === 'schema:bronze_shop' && x.target === 'schema:silver_shop')!
    expect(Number(e.label)).toBeGreaterThanOrEqual(2)
    expect(e.pairs!.length).toBe(Number(e.label))
    expect(v.edges.every((x) => x.source !== x.target)).toBe(true)
  })

  it('lists the tables inside a schema, busiest first, and reacts to job filters', () => {
    const v = buildSchemaView(fg, open())
    expect(v.nodes.find((n) => n.id === 'schema:silver_shop')!.tableIds![0]).toBe('table:silver_shop.orders')
    const none = buildSchemaView(fg, open({ liveness: new Set<never>() })).edges.length
    expect(none).toBeLessThan(v.edges.length)
  })

  it('needs no focus', () => {
    expect(buildAnyView(fg, null, open({ mode: 'tables' })).nodes).toEqual([])
    expect(buildAnyView(fg, null, open({ mode: 'schemas' })).nodes.length).toBeGreaterThan(3)
  })
})

describe('used by', () => {
  it('lists the notebooks and pipelines that write and read a table, best-evidenced first', () => {
    const u = usedBy(fg, id('silver_shop.orders'))
    expect(u.writers.map((r) => r.job.name).sort()).toEqual(['nb_delta_api', 'nb_silver_orders', 'nb_silver_orders_python'])
    const readers = u.readers.map((r) => r.job.name)
    for (const n of ['nb_gold_revenue', 'nb_helper_loader', 'nb_mlv_orders', 'nb_four_part']) expect(readers).toContain(n)
    // jobs seen in code come first; the config loader is attached through config and sorts last
    const firstConfig = u.readers.findIndex((r) => r.config)
    expect(firstConfig).toBeGreaterThan(3)
    expect(u.readers.slice(firstConfig).every((r) => r.config)).toBe(true)
    expect(u.readers[firstConfig].job.name).toBe('nb_silver_loader')
    expect(u.writers.find((r) => r.job.name === 'nb_silver_orders')?.evidence?.file).toContain('nb_silver_orders')
    expect(u.binds.map((n) => n.name)).toContain('Shop Model.orders_import')
  })

  it('marks string-literal mentions and config-attached jobs', () => {
    const lit = usedBy(fg, id('silver_shop.customers')).readers.find((r) => r.job.name === 'nb_literal_sql')
    expect(lit?.how).toBe('references')
    const cfg = usedBy(fg, id('bronze_shop.orders')).writers.find((r) => r.job.name === 'nb_generic_load')
    expect(cfg?.config).toBe(true)
    // config-attached rows sort after the ones seen in code
    const rows = usedBy(fg, id('bronze_shop.orders')).writers
    expect(rows[rows.length - 1].config).toBe(true)
  })

  it('lists the table a semantic table is built on', () => {
    expect(usedBy(fg, 'semantic_table:Shop Model.revenue_daily').boundTo.map((n) => n.name)).toEqual(['gold_shop.revenue_daily'])
  })

  it('finds variants of a job, newest first', () => {
    const fam = familyOf(fg, fg.nodes.get(id('nb_silver_orders'))!)
    expect(fam.map((n) => n.name)).toEqual(['nb_silver_orders_python', 'nb_silver_orders'])
    expect(familyOf(fg, fg.nodes.get(id('nb_gold_revenue'))!)).toEqual([])
  })
})

describe('impact report', () => {
  it('lists downstream and upstream with liveness and strictness counts', () => {
    const r = buildImpact(fg, id('bronze_shop.orders'))
    expect(r.looseDownstream).toBeGreaterThanOrEqual(r.strictDownstream)
    const md = toMarkdown(r)
    expect(md.startsWith('# Impact report: bronze_shop.orders')).toBe(true)
    expect(md).toContain('Downstream: breaks if this changes')
    expect(md).toContain('gold_shop.revenue_daily')
  })

  it('exports CSV with a header and escaped fields', () => {
    expect(csvEscape('a,b')).toBe('"a,b"')
    expect(csvEscape('say "hi"')).toBe('"say ""hi"""')
    expect(csvEscape('plain')).toBe('plain')
    const [header, ...rows] = toCsv(buildImpact(fg, id('bronze_shop.orders'))).split('\n')
    expect(header.startsWith('direction,depth,type,name')).toBe(true)
    expect(rows.length).toBeGreaterThan(3)
    expect(rows.some((r) => r.startsWith('downstream,1,'))).toBe(true)
  })
})

describe('job pair limit and unresolved names', () => {
  // job j reads 5 tables and writes `outs` tables plus 1 unresolved name
  const tiny = (outs = 4): FlowGraph => {
    const node = (id: string, type: string, name: string) => ({ id, type, name, path: null, itemId: null, meta: {} })
    const ins = [1, 2, 3, 4, 5].map((i) => node(`table:s.in${i}`, 'table', `s.in${i}`))
    const outputs = Array.from({ length: outs }, (_, i) => node(`table:s.out${i}`, 'table', `s.out${i}`))
    const nodes = [node('notebook:j', 'notebook', 'j'), node('unresolved:{x}', 'unknown', '{x}'), ...ins, ...outputs]
    const edges = [
      ...ins.map((t) => ({ from: 'notebook:j', to: t.id, kind: 'reads', confidence: 'parsed', evidence: [] })),
      ...outputs.map((t) => ({ from: 'notebook:j', to: t.id, kind: 'writes', confidence: 'parsed', evidence: [] })),
      { from: 'notebook:j', to: 'unresolved:{x}', kind: 'writes', confidence: 'unresolved', evidence: [] },
    ]
    return new FlowGraph({ meta: { schemaVersion: 1, generatedAt: '' }, nodes, edges } as unknown as Lineage)
  }
  const run = (maxPairs: number, g = tiny()) => contract(g, { allowItem: () => true, allowConf: () => true, includeConfig: false, maxPairs })

  it('counts only resolved pairs against the limit, so a hidden unresolved write does not skip the job', () => {
    const c = run(20)                      // 5 x 4 resolved pairs fit; 25 with the unresolved name would not
    expect(c.skipped).toHaveLength(0)
    expect(c.edges).toHaveLength(20)
    expect(c.edges.some((e) => e.to === 'unresolved:{x}')).toBe(false)
  })

  it('adds the unresolved pairs too when everything fits, and skips a loader when even resolved pairs do not', () => {
    expect(run(25).edges.some((e) => e.to === 'unresolved:{x}')).toBe(true)
    expect(run(19).skipped.map((s) => s.name)).toEqual(['j'])
  })

  it('never skips a job that writes 3 tables or fewer, however many it reads', () => {
    const c = run(1, tiny(2))
    expect(c.skipped).toHaveLength(0)
    expect(c.edges.filter((e) => e.to === 'table:s.out0')).toHaveLength(5)
  })

  it('takes exact pairs from column lineage when a loader is too broad', () => {
    const g = tiny()
    g.data.columns = {
      schemaVersion: 1, tables: {}, stats: {},
      edges: [{ from: 'table:s.in2', fromColumn: 'a', to: 'table:s.out0', toColumn: 'a', transform: 'direct', confidence: 'parsed', job: 'notebook:j' }],
    }
    const c = run(2, g)
    const e = c.edges.filter((x) => x.to === 'table:s.out0')
    expect(e.map((x) => x.from)).toEqual(['table:s.in2'])
    expect(e[0].via[0].basis).toBe('columns')
    expect(c.skipped.map((x) => x.name)).toEqual(['j'])             // its other outputs have no column lineage
  })
})

describe('row-level flows from config tables', () => {
  const view = (patch: Partial<Filters> = {}) => buildTableView(fg, id('silver_shop.orders_cfg'), open({ up: 1, down: 1, ...patch }))

  it('draws config-row flows between tables by default, attributed to the loader', () => {
    const v = view()
    const e = v.edges.find((x) => x.source === 'table:bronze_shop.orders' && x.target === 'table:silver_shop.orders_cfg')!
    expect(e.label).toBe('nb_silver_loader')
    expect(e.conf).toBe('parsed')
    expect(e.config).toBe(false)                             // precise, so not hidden like config-resolved edges
    expect(v.nodes.map((n) => n.label).sort()).toEqual(['bronze_shop.customers', 'bronze_shop.orders', 'silver_shop.orders_cfg'])
  })

  it('walks through flows in impact analysis and counts them in the schema overview', () => {
    const down = new Set(fg.walk(id('bronze_shop.customers'), 'down').map((r) => r.name))
    expect(down.has('silver_shop.orders_cfg')).toBe(true)
    const s = buildSchemaView(fg, open())
    expect(s.edges.some((x) => x.source === 'schema:bronze_shop' && x.target === 'schema:silver_shop')).toBe(true)
  })

  it('applies the liveness filter to the loader that runs the flow', () => {
    const f = open({ up: 1, down: 1 })
    const status = fg.nodes.get(id('nb_silver_loader'))!.meta.liveness!     // never called, but it calls nb_custom_silver
    expect(status).toBe('entry-point')
    const without = { ...f, liveness: new Set(f.liveness) }
    without.liveness.delete(status)
    expect(buildTableView(fg, id('silver_shop.orders_cfg'), without).edges).toEqual([])
  })

  it('lets a notebook row link the notebook to its target table', () => {
    const v = buildJobView(fg, id('nb_custom_silver'), open())
    expect(v.nodes.map((n) => n.label)).toContain('silver_shop.custom_out')
  })
})

describe('column lineage', () => {
  const lineage = sample as unknown as Lineage
  const cg = new ColumnGraph(lineage.columns!)
  const ref = (t: string, c: string): ColRef => ({ table: `table:${t}`, column: c })
  const names = (v: ReturnType<typeof buildColumnView>) => new Set(v.nodes.flatMap((n) => (n.columns ?? []).map((c) => `${n.label}.${c.name}`)))

  it('is present in the sample and lists the columns of a table, with how many sources and consumers each has', () => {
    expect(lineage.columns!.stats.edges).toBeGreaterThan(20)
    expect(cg.columnsOf('table:silver_shop.orders')).toEqual(['order_id', 'customer_id', 'order_date', 'amount', 'region'])
    expect(cg.counts('table:gold_shop.revenue_daily', 'amount')).toEqual({ sources: 1, consumers: 1 })  // feeds the view
    expect(cg.hasLineage('table:gold_shop.revenue_daily')).toBe(true)
    expect(cg.hasLineage('table:cfg.load_cfg')).toBe(false)
  })

  it('traces a column upstream through two jobs', () => {
    const v = buildColumnView(fg, cg, ref('gold_shop.order_flags', 'label'), open({ up: 3, down: 0 }))
    expect(names(v)).toEqual(new Set(['gold_shop.order_flags.label', 'silver_shop.customers.region', 'bronze_shop.customers.region']))
    // one card per table, listing the traced columns; the focus table and column are marked
    expect(v.nodes.map((n) => n.label).sort()).toEqual(['bronze_shop.customers', 'gold_shop.order_flags', 'silver_shop.customers'])
    const card = v.nodes.find((n) => n.focus)!
    expect(card.label).toBe('gold_shop.order_flags')
    expect(card.columns!.find((c) => c.focus)?.name).toBe('label')
    expect(card.otherColumns).toBeGreaterThan(0)
    expect(v.anchor).toBe('table:gold_shop.order_flags')
    // links join column rows and run between table cards
    const e = v.edges.find((x) => x.targetCol === 'table:gold_shop.order_flags#label')!
    expect([e.source, e.target, e.sourceCol]).toEqual(['table:silver_shop.customers', 'table:gold_shop.order_flags', 'table:silver_shop.customers#region'])
  })

  it('traces downstream and labels only computed edges', () => {
    const v = buildColumnView(fg, cg, ref('silver_shop.orders', 'amount'), open({ up: 0, down: 2 }))
    expect(names(v).has('gold_shop.revenue_daily.amount')).toBe(true)
    expect(names(v).has('gold_shop.v_revenue.amount')).toBe(true)          // two hops, through the view
    const agg = v.edges.find((e) => e.targetCol!.endsWith('revenue_daily#amount'))!
    expect(agg.label).toBe('aggregate')
    expect(agg.columnEdges![0].expr).toMatch(/sum/i)
    const copy = v.edges.find((e) => e.targetCol!.endsWith('v_revenue#amount'))!
    expect(copy.label).toBe('')                                            // copied as is: no label
    expect(agg.via!.map((j) => j.name)).toEqual(['nb_gold_revenue'])
  })

  it('respects name exclusions, edge confidence and job liveness', () => {
    const f = open({ up: 3, down: 0 })
    const ex = buildColumnView(fg, cg, ref('gold_shop.order_flags', 'label'), { ...f, exclude: { rules: rulesFor('bronze_shop') } })
    expect(names(ex).has('bronze_shop.customers.region')).toBe(false)
    expect(ex.hiddenExcluded).toBe(1)
    const strict = buildColumnView(fg, cg, ref('silver_shop.customers', 'region'), { ...f, conf: new Set(['parsed']) })
    expect(names(strict).has('bronze_shop.customers.region')).toBe(false)  // that link is inferred (MERGE ... *)
    const noLive = { ...f, liveness: new Set(f.liveness) }
    noLive.liveness.delete('feeds-others')                                // nb_silver_customers
    expect(names(buildColumnView(fg, cg, ref('silver_shop.customers', 'region'), noLive)).has('bronze_shop.customers.region')).toBe(false)
  })

  it('keeps unresolved stars visible as a * column, and picks a readable start depth', () => {
    const v = buildColumnView(fg, cg, ref('gold_shop.copy_unknown', '*'), open({ up: 1, down: 0 }))
    expect(names(v)).toEqual(new Set(['gold_shop.copy_unknown.*', 'unknown_schema.t.*']))
    expect(v.edges[0].label).toBe('star')
    const d = autoColumnDepth(fg, cg, ref('silver_shop.orders', 'amount'), open(), 1000)
    expect(d).toEqual({ up: 4, down: 4 })
  })

  it('dispatches columns mode through buildAnyView and needs a focus column', () => {
    const f = open({ mode: 'columns', up: 2, down: 0 })
    expect(buildAnyView(fg, null, f, { graph: cg, focus: null }).nodes).toEqual([])
    expect(buildAnyView(fg, null, f, { graph: cg, focus: ref('gold_shop.order_flags', 'label') }).nodes.length).toBeGreaterThan(1)
    expect(buildAnyView(fg, null, f).nodes).toEqual([])                       // no column data at all
  })

  it('counts drawn columns for the starting depth', () => {
    const v = buildColumnView(fg, cg, ref('silver_shop.orders', 'amount'), open({ up: 0, down: 2 }))
    expect(columnCount(v)).toBe(names(v).size)
  })

  it('suggests the most connected traced columns and respects exclusions and a table filter', () => {
    const top = cg.topColumns(5)
    expect(top.length).toBe(5)
    const deg = (r: ColRef) => cg.counts(r.table, r.column).sources + cg.counts(r.table, r.column).consumers
    expect(deg(top[0])).toBeGreaterThanOrEqual(deg(top[4]))
    expect(top.some((r) => r.column === '*')).toBe(false)                    // unresolved stars are never suggestions
    expect(cg.topColumns(50, (t) => t === 'table:silver_shop.orders').some((r) => r.table === 'table:silver_shop.orders')).toBe(false)
    const only = cg.topColumns(10, () => false, 'table:gold_shop.order_flags')
    expect(only.length).toBeGreaterThan(0)
    expect(only.every((r) => r.table === 'table:gold_shop.order_flags')).toBe(true)
  })

  it('finds columns by "table.column" text, ignoring very short queries', () => {
    const nameOf = (t: string) => fg.nodes.get(t)?.name ?? t
    expect(cg.search('ab', nameOf)).toEqual([])
    const hits = cg.search('order_flags.is_big', nameOf)
    expect(hits).toEqual([{ table: 'table:gold_shop.order_flags', column: 'is_big' }])
    expect(cg.search('revenue_daily', nameOf).every((r) => r.table.includes('revenue_daily'))).toBe(true)
    expect(cg.search('no_such_column_anywhere', nameOf)).toEqual([])
  })

  it('finds columns without lineage too, after the traced ones', () => {
    const nameOf = (t: string) => fg.nodes.get(t)?.name ?? t
    const untraced = Object.entries(cg.tables).flatMap(([t, cols]) => cols.map((c) => ({ table: t, column: c })))
      .find((r) => { const n = cg.counts(r.table, r.column); return n.sources + n.consumers === 0 })!
    expect(untraced).toBeDefined()
    const hits = cg.search(`${nameOf(untraced.table)}.${untraced.column}`, nameOf, 50)
    expect(hits).toContainEqual(untraced)
    const deg = (r: ColRef) => cg.counts(r.table, r.column).sources + cg.counts(r.table, r.column).consumers
    const mixed = cg.search(nameOf(untraced.table), nameOf, 50).map(deg)
    expect(mixed).toEqual([...mixed].sort((x, y) => y - x))                  // traced first
  })

  it('round-trips column ids', () => {
    const r = ref('a.b', 'c')
    expect(parseColId(colId(r))).toEqual(r)
  })
})


describe('focus-path drawing and per-table expansion', () => {
  // a -> f -> d, plus a -> d (bypasses the focus) and d -> f (loops back into it); and a chain
  // t0 -> t1 -> t2 -> t3. Each link is one notebook.
  const graph = (): FlowGraph => {
    const node = (id: string, type: string, name: string) => ({ id, type, name, path: null, itemId: null, meta: {} })
    const tables = ['a', 'f', 'd', 't0', 't1', 't2', 't3'].map((t) => node(`table:s.${t}`, 'table', `s.${t}`))
    const links: [string, string][] = [['a', 'f'], ['f', 'd'], ['a', 'd'], ['d', 'f'], ['t0', 't1'], ['t1', 't2'], ['t2', 't3']]
    const jobs = links.map(([x, y]) => node(`notebook:${x}_${y}`, 'notebook', `${x}_${y}`))
    const edges = links.flatMap(([x, y]) => [
      { from: `notebook:${x}_${y}`, to: `table:s.${x}`, kind: 'reads', confidence: 'parsed' },
      { from: `notebook:${x}_${y}`, to: `table:s.${y}`, kind: 'writes', confidence: 'parsed' },
    ])
    return new FlowGraph({ meta: { schemaVersion: 1, generatedAt: '' }, nodes: [...tables, ...jobs], edges } as unknown as Lineage)
  }
  const g = graph()
  const f = open({ up: 2, down: 2 })

  it('draws only links on a path through the focus and counts the others', () => {
    const v = buildTableView(g, 'table:s.f', f)
    expect(v.edges.map((e) => `${e.source}>${e.target}`).sort()).toEqual(['table:s.a>table:s.f', 'table:s.f>table:s.d'])
    expect(v.sideLinks).toBe(2)
    expect(Object.fromEntries(v.nodes.map((n) => [n.label, n.side]))).toEqual({ 's.a': 'up', 's.f': 'focus', 's.d': 'down' })
  })

  it('shows how many tables wait beyond the depth, and opens them one table at a time', () => {
    const v = buildTableView(g, 'table:s.t1', open({ up: 0, down: 1 }))
    expect(v.nodes.map((n) => n.label).sort()).toEqual(['s.t1', 's.t2'])
    const t1 = v.nodes.find((n) => n.label === 's.t1')!
    const t2 = v.nodes.find((n) => n.label === 's.t2')!
    expect(t1.more).toEqual({ up: 1, down: 0 })
    expect(t2.more).toEqual({ up: 0, down: 1 })
    const opened = buildTableView(g, 'table:s.t1', open({ up: 0, down: 1 }), { up: new Set(['table:s.t1']), down: new Set(['table:s.t2']) })
    expect(opened.nodes.map((n) => n.label).sort()).toEqual(['s.t0', 's.t1', 's.t2', 's.t3'])
    expect(opened.nodes.find((n) => n.label === 's.t3')!.more).toEqual({ up: 0, down: 0 })
  })

  it('reports how many settings differ from the defaults', () => {
    expect(changedFilters(defaultFilters())).toBe(0)
    expect(changedFilters({ ...defaultFilters(), showConfig: true, maxPairs: 6 })).toBe(2)
  })
})

describe('layered layout', () => {
  const box = (id: string) => ({ id, w: 100, h: 40 })

  it('puts upstream tables left of the anchor and downstream ones right, with every link pointing right', () => {
    const nodes = ['a', 'b', 'f', 'c', 'd'].map(box)
    const edges = [{ s: 'a', t: 'b' }, { s: 'b', t: 'f' }, { s: 'a', t: 'f' }, { s: 'f', t: 'c' }, { s: 'c', t: 'd' }]
    const lay = layered(nodes, edges, { anchor: 'f' })
    const x = (id: string) => lay.boxes.get(id)!.x
    expect(x('a')).toBeLessThan(x('b'))
    expect(x('b')).toBeLessThan(x('f'))
    expect(x('f')).toBeLessThan(x('c'))
    expect(x('c')).toBeLessThan(x('d'))
    for (const e of edges) expect(x(e.s)).toBeLessThan(x(e.t))
    expect(lay.backEdges).toBe(0)
  })

  it('never overlaps boxes in a rank and survives cycles', () => {
    const nodes = ['a', 'b', 'c', 'x', 'y', 'z'].map(box)
    const edges = [{ s: 'a', t: 'x' }, { s: 'b', t: 'x' }, { s: 'c', t: 'x' }, { s: 'x', t: 'y' }, { s: 'y', t: 'x' }, { s: 'x', t: 'z' }]
    const lay = layered(nodes, edges, { gapY: 10 })
    expect(lay.backEdges).toBe(1)
    const byRank = new Map<number, { y: number; h: number }[]>()
    for (const b of lay.boxes.values()) byRank.set(b.rank, [...(byRank.get(b.rank) ?? []), b])
    for (const list of byRank.values()) {
      list.sort((p, q) => p.y - q.y)
      for (let i = 1; i < list.length; i++) expect(list[i].y).toBeGreaterThanOrEqual(list[i - 1].y + list[i - 1].h + 10 - 1e-6)
    }
    expect(lay.width).toBeGreaterThan(0)
    expect(layered([], []).boxes.size).toBe(0)
  })

  it('reorders rows inside cards so linked rows line up', () => {
    const rows = new Map([['L', ['l1', 'l2']], ['R', ['r1', 'r2']]])
    const rowCard = new Map([['l1', 'L'], ['l2', 'L'], ['r1', 'R'], ['r2', 'R']])
    // l1 -> r2 and l2 -> r1 cross; after ordering they should not
    const out = orderRows(rows, rowCard, [['l1', 'r2'], ['l2', 'r1']], (_card, i) => i * 10)
    const posL = (r: string) => out.get('L')!.indexOf(r)
    const posR = (r: string) => out.get('R')!.indexOf(r)
    expect(Math.sign(posL('l1') - posL('l2'))).toBe(Math.sign(posR('r2') - posR('r1')))
  })
})

describe('navigation in the URL', () => {
  it('round-trips a table focus, a traced column, the schemas view and the All tables page', () => {
    const table = { mode: 'tables' as const, focus: 'table:silver_shop.orders', col: null }
    const column = { mode: 'columns' as const, focus: 'table:silver_shop.orders', col: { table: 'table:silver_shop.orders', column: 'amount' } }
    for (const n of [table, column, { mode: 'schemas' as const, focus: null, col: null }, HOME]) {
      expect(parseHash(toHash(n))).toEqual(n)
    }
    expect(toHash(column)).not.toMatch(/#.*#/)                            // the column id's '#' is encoded
  })

  it('ignores unknown or broken hashes', () => {
    expect(parseHash('')).toBeNull()
    expect(parseHash('#view=nope')).toBeNull()
    expect(parseHash('#view=columns&col=no-hash')).toBeNull()
  })

  it('keys a place by what is on screen only', () => {
    expect(navKey({ mode: 'schemas', focus: 'table:a.b', col: null })).toBe(navKey({ mode: 'schemas', focus: null, col: null }))
    expect(navKey({ mode: 'tables', focus: 'table:a.b', col: null })).not.toBe(navKey(HOME))
  })
})

describe('team defaults', () => {
  const doc = {
    start: 'Silver_Shop.Orders', depth: { up: 2, down: 1 }, tablesPerSide: 30,
    hide: { views: false, patterns: ['staging.'] }, alwaysShow: ['silver_shop.tmp_keep'],
    jobs: 'active', evidence: ['parsed'], configResolved: true, show: { unresolvedNames: true }, broadJobPairs: 60,
    unknownKey: 1,
  }

  it('reads every setting it knows and ignores the rest', () => {
    const { defaults, problems } = parseTeamDefaults(doc)
    expect(problems).toEqual([])
    expect(defaults.start).toBe('silver_shop.orders')
    expect(fixedDepth(defaults)).toEqual({ up: 2, down: 1 })
    expect(perSide(defaults)).toBe(30)
    const f = teamFilters(defaults)
    expect([...f.liveness].sort()).toEqual(['called', 'orchestrated', 'scheduled'])
    expect([...f.conf]).toEqual(['parsed'])
    expect(f.showConfig).toBe(true)
    expect(f.types.has('unknown')).toBe(true)
    expect(f.maxPairs).toBe(60)
  })

  it('builds the name rules: presets switched, extra patterns, always-show list', () => {
    const ex = teamExclude(parseTeamDefaults(doc).defaults)
    expect(activePatterns(ex)).not.toContain('vw_')
    expect(activePatterns(ex)).toContain('staging.')
    expect(compileSettings(ex)('silver_shop.tmp_keep')).toBe(false)
    expect(compileSettings(ex)('silver_shop.tmp_other')).toBe(true)
  })

  it('reports bad values instead of failing, and falls back to the built-in defaults', () => {
    const { defaults, problems } = parseTeamDefaults({ depth: { up: 'x' }, tablesPerSide: 2 })
    expect(problems.length).toBe(2)
    expect(fixedDepth(defaults)).toBeNull()
    expect(parseTeamDefaults('nope').problems).toEqual(['not a JSON object'])
    expect(changedFilters(teamFilters({}))).toBe(0)
  })

  it("keeps a viewer's own name rules only until the team file changes", () => {
    const mem = new Map<string, string>()
    const store = { getItem: (k: string) => mem.get(k) ?? null, setItem: (k: string, v: string) => void mem.set(k, v) }
    const v1 = parseTeamDefaults({ alwaysShow: ['a.b'] }).defaults
    const v2 = parseTeamDefaults({ alwaysShow: ['a.b', 'c.d'] }).defaults
    const mine = addPatterns(teamExclude(v1), 'my_own')
    saveExclude(mine, store, stamp(v1))
    expect(activePatterns(loadExclude(store, teamExclude(v1), stamp(v1)))).toContain('my_own')     // same team file
    expect(activePatterns(loadExclude(store, teamExclude(v2), stamp(v2)))).not.toContain('my_own') // team file changed
    expect(loadExclude(store, teamExclude(v2), stamp(v2)).keep).toEqual(['a.b', 'c.d'])
  })
})
