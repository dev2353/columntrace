# How it works

ColumnTrace builds one graph from a Fabric git-integration folder and writes it to `lineage.json`.
The UI reads that file; the extractor never writes to Fabric.

## 1. Items

Every `*.Notebook` and `*.DataPipeline` folder becomes a node, named from its `.platform` file and keyed
by its `logicalId` so pipeline activities can find the notebooks they run. Semantic models contribute
one `semantic_table` node per TMDL table, bound to the lakehouse table it reads.

## 2. Table references

| Where | How |
| --- | --- |
| `spark.sql(...)`, `%%sql`, SQL notebooks | parsed with sqlglot (Spark dialect): sources, targets, CTEs and temp views told apart |
| Python calls | `saveAsTable`, `insertInto`, `spark.table`, `DeltaTable.forName/forPath`, `write_deltalake`, shared path helpers |
| Strings | f-strings, `.format`, `%`, constants, parameter-cell defaults and `schema, table = name.split(".", 1)` are evaluated; whatever stays dynamic becomes an `unresolved` node with its template, e.g. `silver.{table}` |
| Pipelines | TridentNotebook and InvokePipeline activities (`calls`), Copy source and sink datasets, Lookup and Script SQL |
| Literals | a quoted `schema.table` string that matches a known table becomes a `references` edge |

A cell that does not parse falls back to a regex scan, and the notebook gets a warning; its edges are
`inferred`.

## 3. Confidence

Every edge carries one level, and the UI draws each differently:

- `parsed` (solid): read directly from SQL, an API call or pipeline JSON.
- `inferred` (dashed): a step was assumed: an unqualified name, a regex fallback, SQL held in a string, a
  literal mention, or a link added by a config resolver.
- `unresolved` (dotted): the table name is built at run time. The node keeps the template.

When the same edge is seen twice, the best evidence wins.

## 4. From jobs to table flows

Stored edges read like sentences (`notebook reads table`, `notebook writes table`). The Tables view folds
each job into the links between tables: a notebook that reads `a` and writes `b` becomes `a → b`, labelled
with the notebook. A job that reads N tables and writes M cannot say which input feeds which output, so
all N × M pairs are drawn. A job that writes more than 3 tables and has more pairs than the limit (24 by
default) is a generic loader that would connect everything to everything: its outputs with column lineage
get their exact source tables from it, and the rest are skipped and counted.

Around a focus table only links on a path through the focus are drawn. Upstream tables feed each other
or the focus, and the focus feeds downstream tables. A link that bypasses the focus (an upstream table
that also feeds a downstream one) or loops back is counted, not drawn. That keeps every picture a
left-to-right flow. The layout is a small layered (Sugiyama-style) layout: cycles are broken, ancestors
are ranked as close to the focus as their longest path allows, rows are ordered by the barycenter of
their neighbors, and links are drawn as curves.

## 5. Liveness, staleness and variants

Large repos collect experiments, copies and PySpark/Python twins. Every notebook and pipeline gets a
repo-only status, a hint rather than proof that something runs:

| Status | Meaning |
| --- | --- |
| `scheduled` | has an enabled schedule in its `.schedules` file (disabled or expired ones are ignored) |
| `orchestrated` | reachable through `calls` edges from a scheduled item |
| `called` | called by something, but not reachable from any schedule |
| `entry-point` | never called but calls others (run by hand or externally) |
| `feeds-others` | never called, but another item reads what it writes |
| `dead-end` | never called and nothing reads its output |
| `uncalled` | (pipelines) never called and not scheduled |

Variant families group names such as `x`, `x_python`, `x_v2` and `x_copy`; each member records whether it
is the newest. With last-change dates (`fetch-dates`), items older than `--stale-days` (365 by default)
are flagged `stale`, and items that share one commit with 50 or more others are marked `bulkCommit`
(renames, repo-wide syncs), so their date is not mistaken for a real edit.

## 6. Column lineage

With a schema export (`--schema`) or `--columns`, every statement that writes a table (INSERT, CTAS,
`CREATE VIEW`, MERGE, materialized lake views, DataFrames saved from `spark.sql`) is traced. Each output
column is followed through CTEs, subqueries, unions and temp views to the physical columns it is computed
from. Each column edge records:

- how it is computed: `direct` (copied), `expression`, `aggregate`, or `star` (a `SELECT *` over a table
  whose columns are unknown). The strongest transform on the path wins, so a copy of an aggregate is an
  `aggregate`, with the expression where it happens.
- `parsed`, or `inferred` when a step was assumed: a `*` or `UPDATE SET *` matched by name, a column
  without a table prefix given to the only source that can have it, an INSERT without a column list
  mapped by position.
- the SQL expression, the job and the line.

The code is followed as far as it stays SQL: `spark.sql(q)` with `q` a variable or f-string,
`spark.table("v")`, `createOrReplaceTempView`, DataFrames passed through column-preserving methods
(`filter`, `distinct`, `limit`, `write`, ...) to `saveAsTable` or `insertInto`, and chains such as
`spark.sql(q).write.saveAsTable(t)`.

Join keys and filters decide which rows flow, not which columns, so they are not column edges. Temp
views built from DataFrame code, and bare names that are not tables, are counted in the stats
(`tempViewSources`, `unqualifiedSources`) instead of being drawn.

## 7. Config tables

Generic loader notebooks often run `spark.sql(row["query"])` for each row of a config table, so the code
alone never shows which tables feed which. Export the table (`export-table`) and pass it to `extract`
with `--config-table`. Rows are read as `table_name` (target), `query`, `enabled`, `is_notebook` and
`except_columns`:

- A **SQL row** gives a row-level *flow* from each source table of the query into `table_name`. These
  are exact pairs, drawn by default and attributed to the loader notebook. The query also feeds column
  lineage, minus `except_columns`. The loader is the notebook that reads the config table, writes a table
  it cannot name, and uses the `table_name` and `query` columns in its code.
- A **notebook row** (`is_notebook`) names the notebook that fills `table_name`. That notebook gets a
  write edge to the table and the loader gets a call edge to it, both `inferred`.
- Disabled rows are ignored.
- A row whose query names no table, or whose query is exactly 8,000 characters long, is reported. The
  SQL endpoint cuts longer text at that length, and a cut query usually loses its `FROM` clause. The
  warning shows on the target table's card and in its side panel. Exporting the table with
  `export-delta` (OneLake) gives the whole query. That reader also handles Delta column mapping, where
  columns added later are stored under internal names.

`load_cfg`-style JSON configs in the repo (source table, destination, columns) use a separate built-in
resolver. Its edges are tagged *config-resolved* and hidden in the UI by default, because they
over-approximate.

## 8. Limits

- PySpark DataFrame transformations are not traced at column level, and non-SQL writers (pandas,
  delta-rs) have no column lineage.
- Parameter defaults are taken at face value. A run that overrides one writes elsewhere.
- Lakehouse and warehouse tables with the same `schema.table` share one node.
- `.ipynb` notebooks and report (PBIR) definitions are not parsed yet.
- The UI caps a view at 300 tables and 700 links. It keeps the closest tables and the strongest links,
  and says when it does.
