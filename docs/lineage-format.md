# lineage.json (schemaVersion 1)

The contract between the extractor and the UI. All ids are strings. Nodes, edges, flows and column
edges are sorted, so two extracts of the same input diff cleanly.

```jsonc
{
  "meta": {
    "schemaVersion": 1,
    "generatedAt": "2026-10-24T00:00:00+00:00",
    "stats": { "nodes": 71, "edges": 120, "nodesByType": {}, "edgesByKind": {}, "edgesByConfidence": {}, "warnings": 1 },
    "annotations": { "liveness": { "...": "..." } },    // summaries from later passes
    "inputs": {                                          // how current each input was
      "repo": { "branch": "main", "commit": "f147cf2a1b2c", "date": "2026-10-23T14:49:49+00:00" },
      "dates": { "source": "azure-devops", "fetchedAt": "2026-10-24T09:44:33+00:00" },
      "schema": { "source": "information_schema", "exportedAt": "2026-10-24T10:43:01+00:00" },
      "configTables": [{ "source": "onelake-delta", "table": "cfg.my_config", "exportedAt": "2026-10-24T14:16:17+00:00" }]
    }
  },
  "nodes": [ /* Node */ ],
  "edges": [ /* Edge */ ],
  "flows": [ /* Flow, only with --config-table */ ],
  "columns": { /* Columns, only with --schema or --columns */ },
  "warnings": [ { "item": "nb_x", "message": "1 cell(s) failed Python parsing; regex fallback used" } ]
}
```

## Node

| Field | Meaning |
| --- | --- |
| `id` | `table:<schema.table>` (lower case), `notebook:<name>`, `pipeline:<name>`, `semantic_table:<model>.<table>`, `unresolved:<template>` |
| `type` | `table`, `notebook`, `pipeline`, `semantic_table`, `unknown` |
| `name` | display name |
| `path`, `itemId` | item folder in the repo and its `logicalId` (jobs only) |
| `meta` | see below |

Job `meta`: `liveness` (see [How it works](how-it-works.md#5-liveness-staleness-and-variants)),
`schedules` (`{enabled, total}`), `family`, `familyMembers`, `newestInFamily`, and with dates `lastChange`,
`ageDays`, `stale`, `commit`, `commitSpread`, `bulkCommit`. Table `meta`: `unqualified` (no schema in the
code), `external` (a source-system table seen in a Copy activity), `inLakehouse` (with a schema export:
whether the export lists the table; a name without a schema is looked up in `dbo`), `configWarning`.

With a schema export, `meta.annotations.lakehouse` counts the comparison (`checked`, `missing`). A name
without a schema that nothing writes and the lakehouse does not have (a CTE, temp view or DuckDB
relation) is not a node; `meta.annotations.unqualifiedSetAside` lists those per notebook.

## Edge

```json
{ "from": "notebook:nb_silver_orders", "to": "table:silver_shop.orders", "kind": "writes",
  "confidence": "parsed",
  "evidence": [{ "file": "notebooks/nb_silver_orders.Notebook/notebook-content.py", "line": 32,
                 "via": "saveAsTable", "snippet": "df.write.mode(\"overwrite\").saveAsTable(...)" }] }
```

| `kind` | from → to |
| --- | --- |
| `reads`, `writes` | job → table |
| `references` | job → table named in a string literal (read or write unknown) |
| `calls` | job → job (pipeline activity, `%run`, `notebook.run`) |
| `binds` | semantic table → the table it is built on |

`confidence` is `parsed`, `inferred` or `unresolved`. `evidence[].via` is `"config resolver"` for links
added by a config resolver (the UI hides those by default).

## Flow

A table-to-table pair known exactly from one config-table row:

```json
{ "from": "table:bronze_shop.orders", "to": "table:silver_shop.orders_cfg", "job": "notebook:nb_silver_loader",
  "confidence": "parsed", "evidence": { "config": "cfg.silver_load", "id": 1, "via": "config table" } }
```

`job` is the loader notebook, or `config:<table>` when no loader was found.

## Columns

```jsonc
{
  "schemaVersion": 1,
  "tables": { "table:silver_shop.orders": ["order_id", "customer_id", "order_date", "amount", "region"] },
  "edges": [{
    "from": "table:silver_shop.orders", "fromColumn": "amount",
    "to": "table:gold_shop.revenue_daily", "toColumn": "amount",
    "transform": "aggregate",          // direct | expression | aggregate | star
    "confidence": "parsed",            // parsed | inferred
    "job": "notebook:nb_gold_revenue",
    "expr": "SUM(o.amount)", "line": 21 // left out with --no-evidence
  }],
  // counts per pass: statements, edges, edgesParsed, failedParse, dynamicTarget, ambiguousColumns,
  // unresolvedStars, tempViewSources, unqualifiedSources, ...
  "stats": { "statements": 30, "edges": 40, "tempViewSources": 0 }
}
```

`fromColumn` is `*` when a `SELECT *` could not be expanded because the source's columns are unknown.
