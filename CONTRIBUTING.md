# Contributing

## Layout

```text
src/columntrace/        the extractor (Python, one dependency: sqlglot)
  extract.py            orchestration: items -> graph -> lineage.json
  notebooks.py          notebook cells: SQL, Python calls, temp views, DataFrame chains
  strings.py            evaluates f-strings, .format, constants, parameter defaults, split()
  sql_refs.py           table references in SQL (sqlglot, regex fallback)
  columns.py            column lineage (sqlglot scopes, CTEs, unions, MERGE, temp views)
  pipelines.py          pipeline JSON: calls, Copy, Lookup, Script
  semantic.py           semantic model TMDL
  config_resolver.py    load_cfg JSON and exported config tables
  liveness.py           status, variants, stale flags
  gitdates.py           last-change dates from Azure DevOps (read-only)
  schema_export.py      INFORMATION_SCHEMA.COLUMNS export (read-only)
  table_export.py       one config table export through the SQL endpoint (read-only)
  delta_export.py       one config table export from OneLake Delta files (read-only, whole text)
  refresh.py            every step from one settings file, plus the change report
  impact.py, cli.py     impact report, command line
ui/                     React + TypeScript + Vite, no graph library
  src/lib/              pure logic, unit tested: flow, tablegraph, view, layout, colgraph, exclude, usage, report
  src/lib/nav.ts        where the user is, kept in the URL hash (Back / Forward)
  src/lib/teamDefaults.ts   team-defaults.json: everyone's starting settings (example: team-defaults.example.json)
  src/components/       LineageCanvas (SVG + HTML cards), TableCatalog, SidePanel, FilterPanel, ImpactModal
  public/sample-lineage.json   generated from the fixtures
  scripts/single-page.mjs      one self-contained page (demo, or a page to share)
demo/index.html         the demo page: `cd ui && npm run build:demo` after the sample or the UI changes
PROMPT.md               agent prompts to set it up for a workspace, or to rebuild it
fixtures/               a synthetic "Contoso Shop" workspace and hand-labelled ground truth
tools/make_fixtures.py  regenerates fixtures/ and the UI sample
tools/check_public.py   pre-publish leak check
docs/                   how it works, command reference, lineage.json format
```

## Setup and checks

```bash
pip install -e ".[dev]"
python -m pytest
python -m ruff check .

cd ui
npm ci
npm test
npm run build        # also type-checks
```

## Fixtures

Fixtures are generated, never hand-edited. To cover a new pattern, add a notebook to
`tools/make_fixtures.py`, add the edges you expect from it to the ground truth in the same file, and run:

```bash
python tools/make_fixtures.py
```

This rewrites `fixtures/` and `ui/public/sample-lineage.json` on a fixed clock, so the output only changes
when the extractor or the fixtures do. CI regenerates both and fails if they differ from what is committed.
`tests/test_synthetic.py` and `tests/test_columns.py` then require an exact match with the ground truth:
no missed edges and no extra ones.

## Keeping real data out

Never commit a real workspace, an export or a `lineage.json` built from one. Put them under `data/` or
name them `*.private.json`; both are ignored. Before pushing, run:

```bash
python tools/check_public.py
```

It flags workspace and item ids, SQL endpoint hosts, e-mail addresses, home folders, tokens and private
files. Add words specific to your organization (company, workspace or server names) to `.publiccheck.local`,
one regular expression per line. That file is git-ignored, so the check finds those words without the
repository naming them.
