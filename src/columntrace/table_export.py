"""Export the rows of one (small) config table from a lakehouse SQL analytics endpoint.

Config tables drive generic loaders: the notebook shows only ``spark.sql(row['query'])`` while the
table holds the real source-to-target mapping. Exporting it to a file lets the extractor read that
mapping without running anything in Fabric.

Read-only by construction: the only statement ever sent is ``SELECT TOP (n) * FROM [schema].[table]``
for a validated ``schema.table`` name. Auth and connection are shared with ``export-schema``
(``SQL_TOKEN`` or ``az account get-access-token``; pyodbc and ODBC Driver 18).

The SQL endpoint shows lakehouse text columns as `varchar(8000)`: a longer value (a long query, say)
arrives cut at 8000 characters. Such cells are listed in `cutCells`; `export-delta` reads the
same table from OneLake with every value whole.

The exported file contains the table's rows (which may include SQL text): keep it private. It does
not record the server or database.
"""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import Any

from .schema_export import SchemaExportError

SCHEMA_VERSION = 1
DEFAULT_MAX_ROWS = 50_000
TEXT_LIMIT = 8000          # lakehouse strings through the SQL endpoint: varchar(8000)
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def parse_table_name(name: str) -> tuple[str, str]:
    """``schema.table`` -> (schema, table); anything else is refused (no injection surface)."""
    parts = name.split(".")
    if len(parts) != 2 or not all(_IDENT.match(p) for p in parts):
        raise SchemaExportError(f"table must be schema.table with plain identifiers, got {name!r}")
    return parts[0], parts[1]


def build_query(table: str, max_rows: int = DEFAULT_MAX_ROWS) -> str:
    schema, name = parse_table_name(table)
    if not isinstance(max_rows, int) or max_rows < 1:
        raise SchemaExportError("max_rows must be a positive integer")
    return f"SELECT TOP ({max_rows + 1}) * FROM [{schema}].[{name}]"     # +1 to detect truncation


def jsonable(v: Any) -> Any:
    if isinstance(v, (datetime, date, time)):
        return v.isoformat()
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, (bytes, bytearray)):
        return v.hex()
    return v


def fetch_table(conn: Any, table: str, max_rows: int = DEFAULT_MAX_ROWS) -> tuple[list[str], list[tuple], bool]:
    """Return (column names, rows, truncated)."""
    cur = conn.cursor()
    try:
        cur.execute(build_query(table, max_rows))
        cols = [d[0] for d in cur.description]
        rows = [tuple(r) for r in cur.fetchall()]
    finally:
        cur.close()
    truncated = len(rows) > max_rows
    return cols, rows[:max_rows], truncated


def cut_cells(cols: list[str], rows: list[tuple], limit: int = TEXT_LIMIT) -> list[dict]:
    """Cells exactly `limit` characters long: almost surely cut by the endpoint, not that long."""
    return [{"row": i, "column": c} for i, r in enumerate(rows) for c, v in zip(cols, r, strict=True)
            if isinstance(v, str) and len(v) == limit]


def to_document(table: str, cols: list[str], rows: list[tuple], truncated: bool = False,
                source: str = "sql-endpoint-table", text_limit: int | None = TEXT_LIMIT) -> dict:
    doc = {
        "schemaVersion": SCHEMA_VERSION, "source": source, "table": table.lower(),
        "exportedAt": datetime.now(UTC).isoformat(timespec="seconds"),
        "truncated": truncated, "columns": cols,
        "rows": [{c: jsonable(v) for c, v in zip(cols, r, strict=True)} for r in rows],
    }
    if text_limit:
        doc["cutCells"] = cut_cells(cols, rows, text_limit)
    return doc


def save_json(doc: dict, path: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=1)


def load_document(path: str) -> dict:
    """An exported table document (from `export-table` or `export-delta`)."""
    with open(path, encoding="utf-8") as f:
        doc = json.load(f)
    if doc.get("schemaVersion") != SCHEMA_VERSION or "rows" not in doc:
        raise SchemaExportError(f"not a table export (schemaVersion {SCHEMA_VERSION} expected): {path}")
    return doc


def load_rows(path: str) -> list[dict]:
    """The rows of an exported table document."""
    return load_document(path)["rows"]
