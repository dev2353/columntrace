# Command reference

Install with `pip install -e .` (add `[sql]` for the two export commands, which need `pyodbc` and the
Microsoft ODBC Driver 18 for SQL Server). Every command is read-only.

## extract

```bash
columntrace extract --repo <fabric-git-folder> --out lineage.json \
    [--dates dates.json] [--stale-days 365] \
    [--schema schema.json] [--schema-ignore "_uat,staging.*"] [--columns] \
    [--config-table my_config.json ...] \
    [--resolvers load_cfg] [--no-evidence] [--now 2026-10-24]
```

| Option | Effect |
| --- | --- |
| `--dates` | file from `fetch-dates`: adds `lastChange`, `ageDays`, `stale` and `bulkCommit` to notebooks and pipelines |
| `--stale-days` | age after which an item is flagged stale (default 365) |
| `--schema` | file from `export-schema`: expands `SELECT *` and turns on column lineage |
| `--schema-ignore` | comma list of table-name patterns left out of the schema (substring, or `*` wildcard) |
| `--columns` | column lineage without a schema (explicit column lists only) |
| `--config-table` | file from `export-table` (repeatable option): config rows that map a target table to a SELECT or a notebook |
| `--resolvers` | config resolvers for JSON configs in the repo (default `load_cfg`; `''` turns them off) |
| `--no-evidence` | leaves out code snippets, SQL expressions and line numbers (safer to share) |
| `--now` | the date used as "today" for ages and `generatedAt`, so the same input gives the same file |

The command prints the graph's stats and writes `lineage.json` ([format](lineage-format.md)).

## refresh

```bash
columntrace refresh --config refresh.toml [--skip pull,dates,schema,config,extract] [--offline]
```

Runs every step listed in a settings file ([example](../refresh.example.toml)), in order, and stops at
the first one that fails:

| Step | Does | Settings section |
| --- | --- | --- |
| `pull` | `git fetch --depth 1` of the branch, then `git reset --keep` (local edits are kept, never overwritten) | `[repo] pull = true` |
| `dates` | `fetch-dates` | `[dates]` |
| `schema` | `export-schema` | `[schema]` |
| `config` | `export-delta` for each `[[config_tables]]` entry (`via = "sql"` uses `export-table`) | `[[config_tables]]` |
| `extract` | `extract` with every file above; the previous output is kept as `<name>.previous.json` | `[extract]` |

A skipped step reuses the file the last run wrote. `--offline` skips everything that uses the network.
After `extract`, the command compares the new file with the previous one and prints a short Markdown
report, also saved as `<name>.changes.md`: tables, notebooks and pipelines added or removed, link and
column-link counts, and warnings that are new. Paths in the settings file are relative to that file.
The file names your repository, endpoint and workspace, so keep it outside the repository
(`refresh.toml` is git-ignored).

## impact

```bash
columntrace impact lineage.json silver_sales.orders [--confidence parsed|inferred]
```

Prints a Markdown report of everything downstream (breaks if the node changes) and upstream (feeds it).
`--confidence parsed` gives the strict view. The node can be a table name, `notebook:<name>` or
`pipeline:<name>`.

## stats

```bash
columntrace stats lineage.json
```

## fetch-dates (Azure DevOps)

```bash
columntrace fetch-dates --org <org> --project <project> --repo <repo> --branch main --out dates.json
```

Calls `Items - List` with `latestProcessedChange=true`. Azure DevOps computes the latest commit per
folder, so a repository of several hundred items takes about one call per top-level folder (no
commit-history walk). Auth is a bearer token from `AZURE_DEVOPS_TOKEN` or `az account get-access-token`.
Only a date and a short commit id are stored per item: no author names, no commit messages. The date is
when a change last reached the branch. In repos fed by a promotion pipeline, that is the last deployed
change, not the last run.

## export-schema (lakehouse SQL endpoint)

```bash
columntrace export-schema --server <id>.datawarehouse.fabric.microsoft.com --database <lakehouse> \
    --out schema.json [--csv columns.csv]
```

Sends one query, `SELECT ... FROM INFORMATION_SCHEMA.COLUMNS`. It returns metadata only: table names,
column names and types, no row data. Auth is a bearer token from `SQL_TOKEN` or
`az account get-access-token`. The file does not record the server or database. Find the endpoint
address with `GET /v1/workspaces/{ws}/lakehouses/{id}`
(`properties.sqlEndpointProperties.connectionString`).

## export-table (one config table)

```bash
columntrace export-table --server <endpoint> --database <lakehouse> --table cfg.my_config \
    --out my_config.json [--max-rows 50000]
```

Sends one statement, `SELECT TOP (n) * FROM [schema].[table]`, for a validated `schema.table` name (no
other characters are accepted), and reports truncation. The file holds the table's rows, which can
include SQL text, so keep it private.

The SQL endpoint shows lakehouse text columns as `varchar(8000)`, so a longer value arrives cut at
8,000 characters. A cut query usually loses its `FROM` clause, and with it the row's source tables.
`export-table` lists such cells (`cutCells`) and warns. `extract` marks the affected table with a
warning, shown on its card and in its side panel. Use `export-delta` for those tables.

## export-delta (one config table, from OneLake)

```bash
pip install -e ".[onelake]"          # deltalake
columntrace export-delta --workspace <id or name> --lakehouse <id or name> --table cfg.my_config \
    --out my_config.json [--max-rows 50000]
```

Reads the table's Delta files under `Tables/<schema>/<table>` (read-only), so every value is whole.
Auth is a bearer token for `https://storage.azure.com/` from `ONELAKE_TOKEN` or
`az account get-access-token`. The output has the same format as `export-table`, so pass it to
`extract --config-table` in its place. It does not record the workspace or lakehouse.

## Opening a private file in the dev UI

The UI opens any file with **Open…** or drag and drop. To open a file by URL without copying it into the
project, put its folder in the git-ignored `ui/.env.local`:

```bash
COLUMNTRACE_DATA_DIR=/absolute/path/to/private/output
```

Then restart `npm run dev` and visit `http://localhost:5173/?data=/@fs/<absolute path to lineage.json>`.
