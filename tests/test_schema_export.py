"""Schema export: grouping, files and the query, with a fake connection (no network)."""

import csv
import json

import pytest

from columntrace import schema_export as se

ROWS = [
    ("silver_shop", "orders", "order_id", 1, "bigint", "NO"),
    ("silver_shop", "orders", "Amount", 2, "decimal", "YES"),
    ("gold_shop", "Revenue_Daily", "order_date", 1, "date", "YES"),
    ("sys", "objects", "name", 1, "varchar", "NO"),
]


class FakeCursor:
    def __init__(self, rows):
        self.rows, self.sql, self.closed = rows, None, False

    def execute(self, sql):
        self.sql = sql

    def fetchall(self):
        return self.rows

    def close(self):
        self.closed = True


class FakeConn:
    def __init__(self, rows):
        self.cur = FakeCursor(rows)

    def cursor(self):
        return self.cur


def test_query_is_a_read_only_metadata_select():
    q = se.QUERY.upper()
    assert q.startswith("SELECT") and "INFORMATION_SCHEMA.COLUMNS" in q
    for word in ("INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE", "EXEC", "MERGE", "TRUNCATE"):
        assert word not in q


def test_fetch_rows_runs_the_query_and_closes_the_cursor():
    conn = FakeConn(ROWS)
    assert se.fetch_rows(conn) == ROWS
    assert conn.cur.sql == se.QUERY and conn.cur.closed


def test_document_groups_columns_by_lowercase_table_in_order():
    doc = se.to_document(ROWS)
    assert doc["counts"] == {"tables": 3, "userTables": 2, "columns": 4}
    cols = doc["tables"]["silver_shop.orders"]["columns"]
    assert [c["name"] for c in cols] == ["order_id", "Amount"]            # order and original case kept
    assert cols[0] == {"name": "order_id", "type": "bigint", "nullable": False}
    assert "gold_shop.revenue_daily" in doc["tables"]


def test_files_round_trip_and_hold_no_connection_details(tmp_path):
    doc = se.to_document(ROWS)
    j, c = tmp_path / "s.json", tmp_path / "s.csv"
    se.save_json(doc, str(j))
    se.save_csv(ROWS, str(c))
    assert se.load_schema(str(j))["silver_shop.orders"] == ["order_id", "Amount"]
    text = j.read_text()
    assert "database.windows" not in text and "fabric.microsoft" not in text
    rows = list(csv.reader(c.open(encoding="utf-8")))
    assert rows[0] == se.CSV_HEADER and len(rows) == 1 + len(ROWS)


def test_load_schema_rejects_an_unknown_version(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text(json.dumps({"schemaVersion": 99, "tables": {}}))
    with pytest.raises(se.SchemaExportError, match="unsupported"):
        se.load_schema(str(p))
