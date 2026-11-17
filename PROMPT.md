# Prompts for building ColumnTrace with an AI coding agent

Two prompts, both meant for an agent that can read files and run commands (Claude Code or similar):

- **Prompt A** sets up this repository against *your* Fabric workspace and gives you a working lineage
  page in an afternoon.
- **Prompt B** rebuilds the whole tool from scratch, step by step, with the same design and tests.
  Use it to learn how it works, or to rebuild it in another stack.

Fill in the **Customize** block first. Every value is optional except the repo; leave out a line
and the agent skips that part. Keep the filled-in prompt private: it names your systems.

## Customize

```text
FABRIC_GIT_REPO      = <clone URL of the git repo your Fabric workspace syncs to>
BRANCH               = main
ADO_ORG / PROJECT    = <Azure DevOps organization> / <project>        (for last-change dates; GitHub: leave out)
SQL_ENDPOINT         = <id>.datawarehouse.fabric.microsoft.com         (lakehouse SQL analytics endpoint)
LAKEHOUSE            = <lakehouse name>                                 (for the schema export)
WORKSPACE_ID         = <workspace id>                                   (for config tables read from OneLake)
LAKEHOUSE_ID         = <lakehouse id>
CONFIG_TABLES        = cfg.<table that maps target tables to SQL>, ...  (tables that drive generic loaders)
CONFIG_COLUMNS       = table_name, query, enabled, is_notebook, except_columns   (rename to match yours)
SCHEMA_IGNORE        = _uat, staging.*                                  (tables to leave out of the schema)
EXCLUDE_IN_UI        = vw_, _tmp, _bkp, <schema>.                       (hidden by default, editable in the UI)
WORK_FOLDER          = <local folder; real data goes in <WORK_FOLDER>/data, never in the repo>
SHARE_WITH           = nobody | my team (private page) | public demo (synthetic data only)
```

---

## Prompt A: set it up for my workspace

```text
You are setting up ColumnTrace, a read-only table and column lineage tool for Microsoft Fabric, for my
workspace. Use the values in the Customize block below. Work in WORK_FOLDER.

Rules
- Read-only everywhere: never write to Fabric, the lakehouse, the SQL endpoint or the git remote.
  Before the first call to each external system (git clone, Azure DevOps, SQL endpoint, OneLake), tell me
  the exact command and wait for my OK.
- Real data stays in WORK_FOLDER/data (git-ignored). Nothing from it goes into the columntrace repo.
- Auth: use my own `az login`; never ask me for passwords or tokens.

Steps
1. Clone the ColumnTrace repo into WORK_FOLDER/columntrace. Run `pip install -e ".[dev,sql,onelake]"`,
   `python -m pytest`, and `cd ui && npm ci && npm test`. Stop and report if anything fails.
2. Shallow-clone FABRIC_GIT_REPO (BRANCH) into WORK_FOLDER/data/<repo name>.
3. Copy columntrace/refresh.example.toml to WORK_FOLDER/data/refresh.toml and fill it in from the
   Customize block. Leave out sections whose values I did not give.
4. Run `columntrace refresh --config ../data/refresh.toml`. Explain each step's output in one line.
5. Check the result:
   - If config tables are listed, compare the exported column names with CONFIG_COLUMNS. If mine
     differ, adjust QueryConfigResolver (src/columntrace/config_resolver.py) through a small, tested
     setting instead of renaming my columns.
   - List every config-row warning (cut queries, rows that name no table) and fix the cause: a query
     cut at 8000 characters means the export went through the SQL endpoint; use the OneLake export.
   - Pick the three busiest tables and confirm their upstream sources against the notebook code.
     Report anything that looks wrong, with file and line.
6. Start the UI (`cd ui && npm run dev`) with ui/.env.local pointing at WORK_FOLDER/data, and give
   me the URL that opens my lineage file. Set the default exclusions in ui/src/lib/exclude.ts
   to EXCLUDE_IN_UI if I gave any.
7. Sharing, per SHARE_WITH:
   - nobody: stop here.
   - my team: build one page with `ui/scripts/single-page.mjs` (VITE_DATA_FILE=lineage.json, or
     --embed for a page that opens from disk) and tell me where to host it privately. Never put it
     anywhere public.
   - public demo: use only the synthetic sample (`npm run build:demo`), never my data.
8. Write WORK_FOLDER/REFRESH.txt: the commands to refresh next month and where the page lives.

Finish with: what works, what was not checked, and the numbers (tables, notebooks, pipelines,
column links, config flows, warnings).
```

---

## Prompt B: build it from scratch

```text
Build "ColumnTrace": a read-only lineage and impact tool for Microsoft Fabric. It reads the item
definitions Fabric commits to git, writes one lineage.json, and shows it in a browser. Use the
Customize block for names and defaults. Python 3.11+ with sqlglot for the extractor; React +
TypeScript + Vite for the UI, with no graph library. Work in small steps; after each step, run the
tests and show me the result before going on.

Ground rules
- Never write to Fabric or any remote. Network exports are separate, opt-in commands.
- Never guess silently: every edge carries a confidence (parsed, inferred, unresolved), and anything
  the extractor cannot resolve is kept as an "unresolved" node with its template, or counted.
- Real data never enters the repo. Build a synthetic workspace generator first and test against it.

Step 1. Synthetic fixtures and ground truth
  tools/make_fixtures.py writes fixtures/synthetic_workspace in Fabric git format
  (<name>.Notebook/.platform + notebook-content.py with "# CELL" and "# MAGIC" markers,
  <name>.DataPipeline/pipeline-content.json, a semantic model in TMDL, .schedules files), plus
  ground_truth.json (table edges) and ground_truth_columns.json (column edges). Cover: %%sql cells,
  spark.sql with f-strings and variables, parameter-cell defaults, schema, table = name.split(".", 1),
  saveAsTable/insertInto, temp views, CTEs, MERGE, UNION, materialized lake views, SQL in strings,
  a broken cell, dynamic table names, %run and notebookutils.notebook.run, pipeline notebook and Copy
  activities, a generic config-driven loader, and a PySpark/Python twin of one notebook.

Step 2. Table lineage (src/columntrace/)
  model.py (Graph, Node, Edge; to_dict with sorted output), notebooks.py (cells to statements and
  calls, an AST visitor for Python, regex fallback), strings.py (evaluate constants, f-strings,
  .format, %, split unpacking), sql_refs.py (sqlglot: sources vs targets, skip CTEs and temp views),
  pipelines.py, semantic.py, extract.py (walk the repo in sorted order). Edge kinds: reads, writes,
  calls, binds, references. Test: exact match with ground_truth.json (no misses, no extras).

Step 3. Liveness and dates
  liveness.py: statuses scheduled, orchestrated, called, entry-point, feeds-others, dead-end,
  uncalled; variant families (_python, _v2, _copy ...); stale flag from dates. gitdates.py: one
  Azure DevOps Items call per folder with latestProcessedChange (no history walk), retries, a fake
  transport in tests, only a date and a short commit id stored.

Step 4. Exports (read-only, separate commands)
  export-schema: one INFORMATION_SCHEMA.COLUMNS query through pyodbc + ODBC Driver 18 with an Entra
  token. export-table: SELECT TOP (n) * for a validated schema.table; flag values exactly 8000
  characters long (the SQL endpoint shows lakehouse text as varchar(8000)). export-delta: the same
  table read from OneLake Delta files with deltalake, through its SQL engine (QueryBuilder) so
  column-mapped columns are not returned empty.

Step 5. Config tables
  QueryConfigResolver reads exported rows (CONFIG_COLUMNS). SQL rows become exact table-to-table
  "flows" attributed to the loader notebook (the notebook that reads the config table, writes a
  dynamic target, and uses the config's column names in its code). Notebook rows link the named
  notebook to its target. Warn (in lineage.json and on the table) when a query names no table or is
  exactly 8000 characters long.

Step 6. Column lineage (columns.py)
  For every statement that writes a table, trace each output column through CTEs, subqueries,
  unions and earlier temp views (inlined as CTEs in dependency order) to physical columns, with
  transform direct/expression/aggregate/star (the strongest on the path wins), the expression, and
  parsed/inferred. Expand SELECT * from the schema export. Count, never guess: ambiguous columns,
  unresolved stars, sources that are temp views built from DataFrame code. Never cache by id() of
  objects that can be freed. Test: exact match with ground_truth_columns.json, and the same output
  on repeated runs.

Step 7. CLI and refresh
  columntrace extract | impact | stats | fetch-dates | export-schema | export-table | export-delta |
  refresh. refresh reads one TOML settings file (repo, dates, schema, config tables, output), runs
  the steps in order (git fetch --depth 1 + reset --keep for pull), keeps the previous lineage file
  and prints a change report (tables and jobs added or removed, link counts, new warnings).

Step 8. UI (ui/)
  - Tables view: a focus table; ancestors left, descendants right; notebooks and pipelines folded
    into the links (a job reading N and writing M tables gives N x M pairs; skip jobs over a limit and
    count them). Draw only links on a path through the focus; count the rest.
  - Layout: your own layered layout (break cycles, rank around the focus by longest path, barycenter
    ordering, overlap-free y), table cards in HTML, smooth SVG curves, pan and zoom, +N buttons that
    open one table's neighbors, hover to highlight a whole path.
  - Columns view: table cards listing the traced columns; links join column rows; f / Σ marks with
    the SQL on hover.
  - Schemas view, an All tables page (filter, sort, sources, not read, with column lineage),
    Back/Forward kept in the URL hash, a side panel (used by, columns, impact report with copy as
    Markdown/CSV), a Filters drawer (editable exclusion rules with match counts, kept in localStorage;
    job statuses; evidence levels).
  - Light and dark themes from CSS variables; keyboard access; works at phone width.
  - Unit tests for every lib module (view building, layout, exclusions, navigation, reports).

Step 9. Repo hygiene
  README with the repo tree and every command; docs/ (how it works, CLI, lineage.json format);
  CONTRIBUTING; MIT license; CI running ruff, pytest, the fixture regeneration diff, the UI tests and
  build; tools/check_public.py, a leak check with private words in a git-ignored file; a demo page
  built from the synthetic sample.

Done means: every test passes, the synthetic sample opens in the browser, and running extract on my
FABRIC_GIT_REPO gives a lineage file I can open in the UI.
```

---

## Tips

- Run Prompt A once per workspace. For a second workspace, keep a second settings file and data folder.
- To adapt to another config-table layout, give the agent one sample row (with fake values) and the
  name of the loader notebook.
- After a month, `columntrace refresh` is all you need. Ask the agent to explain the change report if
  something unexpected was added or removed.
