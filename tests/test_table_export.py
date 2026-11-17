"""Config table export: query safety, serialization, truncation (fake connection, no network)."""

import json
from datetime import date, datetime
from decimal import Decimal

import pytest

from columntrace import table_export as te
from columntrace.schema_export import SchemaExportError


class FakeCursor:
    def __init__(self, cols, rows):
        self.description = [(c,) for c in cols]
        self.rows, self.sql, self.closed = rows, None, False

    def execute(self, sql):
        self.sql = sql

    def fetchall(self):
        return self.rows

    def close(self):
        self.closed = True


class FakeConn:
    def __init__(self, cols, rows):
        self.cur = FakeCursor(cols, rows)

    def cursor(self):
        return self.cur


def test_query_is_a_single_bounded_select_on_a_bracket_quoted_table():
    q = te.build_query("cfg.my_config", 100)
    assert q == "SELECT TOP (101) * FROM [cfg].[my_config]"


@pytest.mark.parametrize("bad", [
    "cfg.t; DROP TABLE x", "cfg", "a.b.c", "cfg.t--", "cfg.[t]", "cfg.t WHERE 1=1", "", "cfg. t", "1cfg.t",
])
def test_anything_but_plain_schema_dot_table_is_refused(bad):
    with pytest.raises(SchemaExportError):
        te.build_query(bad)


def test_max_rows_must_be_a_positive_integer():
    for bad in (0, -1, "5; DROP"):
        with pytest.raises(SchemaExportError):
            te.build_query("cfg.t", bad)  # type: ignore[arg-type]


def test_fetch_runs_only_the_select_and_closes_the_cursor():
    conn = FakeConn(["id", "name"], [(1, "a"), (2, "b")])
    cols, rows, truncated = te.fetch_table(conn, "cfg.t", 10)
    assert conn.cur.sql == "SELECT TOP (11) * FROM [cfg].[t]" and conn.cur.closed
    assert cols == ["id", "name"] and rows == [(1, "a"), (2, "b")] and truncated is False


def test_truncation_is_detected_and_reported():
    conn = FakeConn(["id"], [(i,) for i in range(6)])      # the extra row proves there is more
    cols, rows, truncated = te.fetch_table(conn, "cfg.t", 5)
    assert len(rows) == 5 and truncated is True


def test_document_serializes_dates_decimals_and_bytes_and_records_no_connection(tmp_path):
    cols = ["id", "wm", "d", "amount", "blob", "flag"]
    row = (1, datetime(2026, 1, 2, 3, 4, 5), date(2026, 1, 2), Decimal("1.50"), b"\x01\xff", True)
    doc = te.to_document("CFG.T", cols, [row])
    assert doc["table"] == "cfg.t" and doc["rows"][0] == {
        "id": 1, "wm": "2026-01-02T03:04:05", "d": "2026-01-02", "amount": "1.50", "blob": "01ff", "flag": True}
    p = tmp_path / "t.json"
    te.save_json(doc, str(p))
    assert te.load_rows(str(p)) == doc["rows"]
    assert "fabric.microsoft" not in p.read_text()


def test_load_rows_rejects_other_documents(tmp_path):
    p = tmp_path / "x.json"
    p.write_text(json.dumps({"schemaVersion": 99, "rows": []}))
    with pytest.raises(SchemaExportError):
        te.load_rows(str(p))
