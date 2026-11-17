"""Export column metadata from a Fabric lakehouse SQL analytics endpoint.

One read-only query against ``INFORMATION_SCHEMA.COLUMNS`` returns every table and column
name with its type: metadata only, no row data. The result resolves ``SELECT *`` and
checks schemas derived from code.

Auth: a bearer token from ``SQL_TOKEN``, else ``az account get-access-token`` for
``https://database.windows.net/``. Needs ``pyodbc`` (``pip install columntrace[sql]``) and
the Microsoft ODBC Driver 18 for SQL Server.

The exported file holds names and types only; it does not record the server or database.
"""

from __future__ import annotations

import csv
import json
import os
import struct
import subprocess
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

SQL_RESOURCE = "https://database.windows.net/"
SCHEMA_VERSION = 1
SQL_COPT_SS_ACCESS_TOKEN = 1256          # pyodbc pre-connect attribute for an Entra token

QUERY = (
    "SELECT TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME, ORDINAL_POSITION, DATA_TYPE, IS_NULLABLE "
    "FROM INFORMATION_SCHEMA.COLUMNS "
    "ORDER BY TABLE_SCHEMA, TABLE_NAME, ORDINAL_POSITION"
)
CSV_HEADER = ["table_schema", "table_name", "column_name", "ordinal_position", "data_type", "is_nullable"]
SYSTEM_SCHEMAS = {"sys", "information_schema", "queryinsights"}


class SchemaExportError(RuntimeError):
    pass


def get_sql_token() -> str:
    token = os.environ.get("SQL_TOKEN")
    if token:
        return token.strip()
    try:
        out = subprocess.run(
            ["az", "account", "get-access-token", "--resource", SQL_RESOURCE, "--query", "accessToken", "-o", "tsv"],
            capture_output=True, text=True, timeout=60, shell=(os.name == "nt"))
    except (OSError, subprocess.SubprocessError) as exc:
        raise SchemaExportError(f"no SQL_TOKEN and Azure CLI unavailable: {exc}") from exc
    if out.returncode != 0 or not out.stdout.strip():
        raise SchemaExportError("no SQL_TOKEN and `az account get-access-token` failed; run `az login`")
    return out.stdout.strip()


def connect(server: str, database: str, token: str | None = None) -> Any:
    try:
        import pyodbc
    except ImportError as exc:                       # pragma: no cover - environment dependent
        raise SchemaExportError("pyodbc is required: pip install columntrace[sql]") from exc
    raw = (token or get_sql_token()).encode("utf-16-le")
    attrs = {SQL_COPT_SS_ACCESS_TOKEN: struct.pack(f"<I{len(raw)}s", len(raw), raw)}
    conn_str = (
        f"Driver={{ODBC Driver 18 for SQL Server}};Server=tcp:{server},1433;Database={database};"
        "Encrypt=yes;TrustServerCertificate=no;Connection Timeout=30"
    )
    return pyodbc.connect(conn_str, attrs_before=attrs, autocommit=True)


def fetch_rows(conn: Any) -> list[tuple]:
    cur = conn.cursor()
    try:
        cur.execute(QUERY)
        return [tuple(r) for r in cur.fetchall()]
    finally:
        cur.close()


def to_document(rows: Iterable[tuple]) -> dict:
    """Group rows into {"schema.table": {"columns": [{name, type, nullable}]}} (keys lowercase)."""
    tables: dict[str, dict] = {}
    ncols = 0
    for schema, table, column, _pos, dtype, nullable in rows:
        key = f"{schema}.{table}".lower()
        tables.setdefault(key, {"columns": []})["columns"].append(
            {"name": column, "type": dtype, "nullable": str(nullable).upper() == "YES"})
        ncols += 1
    user_tables = sum(1 for k in tables if k.split(".", 1)[0] not in SYSTEM_SCHEMAS)
    return {
        "schemaVersion": SCHEMA_VERSION, "source": "information_schema",
        "exportedAt": datetime.now(UTC).isoformat(timespec="seconds"),
        "counts": {"tables": len(tables), "userTables": user_tables, "columns": ncols},
        "tables": tables,
    }


def save_json(doc: dict, path: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=1)


def save_csv(rows: Iterable[tuple], path: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(CSV_HEADER)
        w.writerows(rows)


def load_schema(path: str) -> dict[str, list[str]]:
    """Return {schema.table: [column names in order]} from an exported document."""
    with open(path, encoding="utf-8") as f:
        doc = json.load(f)
    if doc.get("schemaVersion") != SCHEMA_VERSION:
        raise SchemaExportError(f"unsupported schema export version: {doc.get('schemaVersion')}")
    return {t: [c["name"] for c in v["columns"]] for t, v in doc["tables"].items()}
