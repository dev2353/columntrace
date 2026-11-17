"""OneLake (Delta) export of a config table, and how cut queries from the SQL endpoint are reported."""

import json

import pytest

from columntrace import delta_export as de
from columntrace import table_export as te
from columntrace.extract import extract
from columntrace.schema_export import SchemaExportError

WS = "00000000-0000-0000-0000-00000000000a"
LH = "00000000-0000-0000-0000-00000000000b"


def test_table_uri_takes_ids_or_names_and_refuses_anything_else():
    assert de.table_uri(WS, LH, "cfg.load") == f"abfss://{WS}@onelake.dfs.fabric.microsoft.com/{LH}/Tables/cfg/load"
    assert de.table_uri("My WS", "Shop_LH", "cfg.load").endswith("@onelake.dfs.fabric.microsoft.com/Shop_LH.Lakehouse/Tables/cfg/load")
    for bad in [(WS, LH, "cfg.load; drop"), ("a/b", LH, "cfg.x"), (WS, "x@y", "cfg.x"), (WS, LH, "x")]:
        with pytest.raises(SchemaExportError):
            de.table_uri(*bad)


def test_fetch_reads_once_with_a_bearer_token_and_reports_truncation():
    calls = []
    long_query = "SELECT a FROM s.t WHERE " + "x = 1 AND " * 1000          # longer than the endpoint's 8000

    def reader(uri, options):
        calls.append((uri, options))
        return ["id", "query"], [(1, long_query), (2, "SELECT 1"), (3, "SELECT 2")]

    cols, rows, truncated = de.fetch_table(WS, LH, "cfg.load", max_rows=2, token="tok", reader=reader)
    assert len(calls) == 1 and calls[0][1] == {"bearer_token": "tok", "use_fabric_endpoint": "true"}
    assert cols == ["id", "query"] and len(rows) == 2 and truncated
    doc = de.to_delta_document("cfg.load", cols, rows, truncated)
    assert doc["source"] == "onelake-delta" and "cutCells" not in doc
    assert doc["rows"][0]["query"] == long_query                           # whole, not cut at 8000


def test_sql_endpoint_export_lists_cells_cut_at_the_limit():
    cut = "S" * te.TEXT_LIMIT
    doc = te.to_document("cfg.load", ["id", "query"], [(1, cut), (2, "short")])
    assert doc["cutCells"] == [{"row": 0, "column": "query"}]


def _workspace(tmp_path):
    nb = tmp_path / "ws" / "nb_other.Notebook"
    nb.mkdir(parents=True)
    (nb / ".platform").write_text('{"metadata":{"displayName":"nb_other"},"config":{"logicalId":"1"}}')
    (nb / "notebook-content.py").write_text("# Fabric notebook source\n")
    return tmp_path / "ws"


def _config(tmp_path, source, query):
    f = tmp_path / f"{source}.json"
    f.write_text(json.dumps({"schemaVersion": 1, "source": source, "table": "cfg.x", "rows": [
        {"id": 7, "table_name": "s.out", "query": query, "enabled": True, "is_notebook": False}]}))
    return str(f)


def test_a_query_cut_by_the_endpoint_is_reported_on_the_table_and_in_the_warnings(tmp_path):
    ws = _workspace(tmp_path)
    cut = ("SELECT a AS col_a, " + "b AS b, " * 2000)[:te.TEXT_LIMIT]           # no FROM: it was cut off
    d = extract(ws, config_tables=[_config(tmp_path, "sql-endpoint-table", cut)]).to_dict()
    node = next(n for n in d["nodes"] if n["id"] == "table:s.out")
    assert "export-delta" in node["meta"]["configWarning"]
    assert any(w["item"] == "cfg.x row 7 (s.out)" and "cut" in w["message"] for w in d["warnings"])


def test_the_same_length_from_a_delta_export_is_trusted(tmp_path):
    ws = _workspace(tmp_path)
    query = "SELECT a FROM s.src WHERE 1 = 1".ljust(te.TEXT_LIMIT)        # exactly the limit, but whole
    d = extract(ws, config_tables=[_config(tmp_path, "onelake-delta", query)]).to_dict()
    assert [(f["from"], f["to"]) for f in d["flows"]] == [("table:s.src", "table:s.out")]
    assert "configWarning" not in next(n for n in d["nodes"] if n["id"] == "table:s.out")["meta"]


def test_a_query_that_names_no_table_is_reported(tmp_path):
    ws = _workspace(tmp_path)
    d = extract(ws, config_tables=[_config(tmp_path, "sql-endpoint-table", "SELECT 1 AS one")]).to_dict()
    assert next(n for n in d["nodes"] if n["id"] == "table:s.out")["meta"]["configWarning"] == "query names no table"
