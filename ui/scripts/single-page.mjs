// Inline the built CSS and JS into one HTML page, for hosts that take a single page plus a data
// file (a static share, an artifact). Run after `vite build`:
//
//   node scripts/single-page.mjs --out page.html [--title "Team lineage"] [--fragment]
//                                [--embed lineage.json --label "Shop sample" [--sample]] [--defaults team-defaults.json]
//
// --fragment leaves out <!doctype>, <html>, <head> and <body> for hosts that add their own.
// Data: --embed puts a lineage file inside the page, so it opens straight from disk (file://);
// --sample marks it as synthetic in the UI. Without --embed, build with VITE_DATA_FILE=<name> and
// publish that file next to the page. Either way the page holds your lineage: share it only where
// your data may go.

import { readFileSync, writeFileSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const args = process.argv.slice(2)
const opt = (name, dflt) => {
  const i = args.indexOf(`--${name}`)
  return i >= 0 ? args[i + 1] : dflt
}
const fragment = args.includes('--fragment')
const out = opt('out')
if (!out) {
  console.error('usage: node scripts/single-page.mjs --out page.html [--title name] [--fragment] [--embed file --label text --sample]')
  process.exit(2)
}

const dist = join(dirname(fileURLToPath(import.meta.url)), '..', 'dist')
const html = readFileSync(join(dist, 'index.html'), 'utf8')
const asset = (href) => readFileSync(join(dist, href.replace(/^\.?\//, '')), 'utf8')

const css = [...html.matchAll(/<link rel="stylesheet"[^>]*href="([^"]+)"[^>]*>/g)].map((m) => asset(m[1])).join('\n')
const js = [...html.matchAll(/<script type="module"[^>]*src="([^"]+)"[^>]*><\/script>/g)].map((m) => asset(m[1])).join('\n')
if (!css || !js) {
  console.error('dist/index.html has no stylesheet or module script: run `npm run build` first')
  process.exit(2)
}
const icon = html.match(/<link rel="icon"[^>]*>/)?.[0] ?? ''
const desc = html.match(/<meta name="description"[^>]*>/)?.[0] ?? ''
const title = opt('title', html.match(/<title>([^<]*)<\/title>/)?.[1] ?? 'ColumnTrace')
const esc = (s) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;')

const head = `<title>${esc(title)}</title>\n${desc}\n${icon}\n<style>\n${css}\n</style>`
const embed = opt('embed')
let data = ''
if (embed) {
  const json = JSON.stringify(JSON.parse(readFileSync(resolve(embed), 'utf8')))   // validated and compacted
  const attrs = `id="columntrace-data" type="application/json" data-label="${esc(opt('label', 'embedded lineage')).replace(/"/g, '&quot;')}"${args.includes('--sample') ? ' data-sample="true"' : ''}`
  // '<' as \u003c keeps "</script>" or "<!--" in the data from ending the block early; it is still valid JSON
  data = `<script ${attrs}>${json.replace(/</g, '\\u003c')}</script>\n`
}
// --defaults team-defaults.json travels inside the page too (a page opened from disk cannot fetch it)
const defaultsFile = opt('defaults')
if (defaultsFile) {
  const json = JSON.stringify(JSON.parse(readFileSync(resolve(defaultsFile), 'utf8')))
  data += `<script id="columntrace-defaults" type="application/json">${json.replace(/</g, '\\u003c')}</script>\n`
}
// a "</script" inside the bundle would end the inline script early
const body = `<div id="root"></div>\n${data}<script type="module">\n${js.replace(/<\/script/gi, '<\\/script')}\n</script>`
const page = fragment
  ? `${head}\n${body}\n`
  : `<!doctype html>\n<html lang="en">\n<head>\n<meta charset="UTF-8" />\n<meta name="viewport" content="width=device-width, initial-scale=1.0" />\n${head}\n</head>\n<body>\n${body}\n</body>\n</html>\n`
writeFileSync(resolve(out), page)
console.log(`${out}: ${(page.length / 1024).toFixed(0)} kB (${fragment ? 'fragment' : 'full page'})`)
