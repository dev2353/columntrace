"""Export one (small) config table straight from its Delta files in OneLake.

The lakehouse SQL endpoint shows text columns as ``varchar(8000)``, so a long value (a config row's
query, say) arrives cut. Reading the table's Delta files gives every value whole. The output is the
same document ``export-table`` writes, so ``extract --config-table`` takes either.

Read-only by construction: it opens ``Tables/<schema>/<table>`` of one lakehouse for reading and
lists no other path. Auth: a bearer token for ``https://storage.azure.com/`` from ``ONELAKE_TOKEN``,
else ``az account get-access-token``. Needs ``deltalake`` (``pip install columntrace[onelake]``).

The exported file contains the table's rows (which may include SQL text): keep it private. It does
not record the workspace or lakehouse.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Callable
from typing import Any

from .schema_export import SchemaExportError
from .table_export import DEFAULT_MAX_ROWS, parse_table_name, to_document

STORAGE_RESOURCE = "https://storage.azure.com/"
_GUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9 _.-]*$")

# (uri, storage options) -> (column names, rows); replaced in tests
Reader = Callable[[str, dict[str, str]], tuple[list[str], list[tuple]]]


def table_uri(workspace: str, lakehouse: str, table: str) -> str:
    """OneLake path of a lakehouse table. Workspace and lakehouse may be ids or display names."""
    schema, name = parse_table_name(table)
    for label, v in (("workspace", workspace), ("lakehouse", lakehouse)):
        if not (_GUID.match(v) or _NAME.match(v)) or "/" in v or "@" in v:
            raise SchemaExportError(f"{label} must be an id or a plain name, got {v!r}")
    item = lakehouse if _GUID.match(lakehouse) or lakehouse.endswith(".Lakehouse") else f"{lakehouse}.Lakehouse"
    return f"abfss://{workspace}@onelake.dfs.fabric.microsoft.com/{item}/Tables/{schema}/{name}"


def get_storage_token() -> str:
    token = os.environ.get("ONELAKE_TOKEN")
    if token:
        return token.strip()
    try:
        out = subprocess.run(
            ["az", "account", "get-access-token", "--resource", STORAGE_RESOURCE, "--query", "accessToken", "-o", "tsv"],
            capture_output=True, text=True, timeout=60, shell=(os.name == "nt"))
    except (OSError, subprocess.SubprocessError) as exc:
        raise SchemaExportError(f"no ONELAKE_TOKEN and Azure CLI unavailable: {exc}") from exc
    if out.returncode != 0 or not out.stdout.strip():
        raise SchemaExportError("no ONELAKE_TOKEN and `az account get-access-token` failed; run `az login`")
    return out.stdout.strip()


def _deltalake_reader(uri: str, options: dict[str, str]) -> tuple[list[str], list[tuple]]:   # pragma: no cover
    try:
        from deltalake import DeltaTable
    except ImportError as exc:
        raise SchemaExportError("deltalake is not installed: pip install columntrace[onelake]") from exc
    name = uri.split("/Tables/")[-1]
    try:
        dt = DeltaTable(uri, storage_options=options)
        mapped = dt.metadata().configuration.get("delta.columnMapping.mode", "none") != "none"
        try:
            # the SQL engine applies column mapping; to_pyarrow_table() returns renamed columns
            # (ALTER TABLE ... ADD COLUMNS on a mapped table) as all-null
            import pyarrow as pa
            from deltalake import QueryBuilder
            # read_all() returns an arro3 table: hand it to pyarrow through the Arrow stream interface
            data = pa.table(QueryBuilder().register("t", dt).execute("SELECT * FROM t").read_all())
        except ImportError:
            if mapped:
                raise SchemaExportError(f"{name} uses column mapping; upgrade deltalake (needs QueryBuilder)") from None
            data = dt.to_pyarrow_table()
    except SchemaExportError:
        raise
    except Exception as exc:                       # the library raises its own error types
        raise SchemaExportError(f"could not read {name}: {exc}") from exc
    cols = list(data.column_names)
    return cols, [tuple(r[c] for c in cols) for r in data.to_pylist()]


def fetch_table(workspace: str, lakehouse: str, table: str, max_rows: int = DEFAULT_MAX_ROWS,
                token: str | None = None, reader: Reader | None = None) -> tuple[list[str], list[tuple], bool]:
    """Return (column names, rows, truncated), reading the Delta table once."""
    if not isinstance(max_rows, int) or max_rows < 1:
        raise SchemaExportError("max_rows must be a positive integer")
    uri = table_uri(workspace, lakehouse, table)
    options = {"bearer_token": token or get_storage_token(), "use_fabric_endpoint": "true"}
    cols, rows = (reader or _deltalake_reader)(uri, options)
    return cols, rows[:max_rows], len(rows) > max_rows


def to_delta_document(table: str, cols: list[str], rows: list[tuple], truncated: bool = False) -> dict[str, Any]:
    # every value is whole here, so no cut cells are reported
    return to_document(table, cols, rows, truncated, source="onelake-delta", text_limit=None)
