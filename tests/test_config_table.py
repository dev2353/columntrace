"""Config-table resolver: rows that map a target table to a SELECT or to a notebook."""

from pathlib import Path

import pytest

from columntrace.columns import ColumnSchema
from columntrace.extract import extract
from columntrace.schema_export import load_schema

ROOT = Path(__file__).resolve().parents[1] / "fixtures"
CFG = str(ROOT / "synthetic_config_table.json")
SCHEMA = load_schema(str(ROOT / "synthetic_schema.json"))


@pytest.fixture(scope="module")
def doc():
    g = extract(ROOT / "synthetic_workspace", schema=SCHEMA, config_tables=[CFG])
    return g.to_dict()


def flows(doc):
    return {(f["from"].removeprefix("table:"), f["to"].removeprefix("table:"), f["job"]) for f in doc["flows"]}


def edges(doc, kind, src=None):
    return [e for e in doc["edges"] if e["kind"] == kind and (src is None or e["from"] == src)]


def test_sql_rows_become_row_level_flows_through_the_loader(doc):
    loader = "notebook:nb_silver_loader"
    assert flows(doc) == {
        ("bronze_shop.orders", "silver_shop.orders_cfg", loader),
        ("bronze_shop.customers", "silver_shop.orders_cfg", loader),
        ("silver_shop.orders", "silver_shop.totals_cfg", loader),     # the CTE "t" is not a table
    }
    assert all(f["confidence"] == "parsed" and f["evidence"]["config"] == "cfg.silver_load" for f in doc["flows"])


def test_disabled_rows_and_cte_names_are_ignored(doc):
    names = {n["name"] for n in doc["nodes"]}
    assert "silver_shop.disabled_out" not in names
    assert "t" not in names


def test_notebook_rows_link_the_named_notebook_to_the_table_it_fills(doc):
    w = [e for e in edges(doc, "writes", "notebook:nb_custom_silver") if e["to"] == "table:silver_shop.custom_out"]
    assert len(w) == 1 and w[0]["confidence"] == "inferred"
    assert w[0]["evidence"][0]["via"] == "config table"            # precise: shown by default, not a config-resolver edge
    calls = [e for e in edges(doc, "calls", "notebook:nb_silver_loader") if e["to"] == "notebook:nb_custom_silver"]
    assert len(calls) == 1


def test_loader_edges_for_used_by_are_tagged_as_config_resolver_edges(doc):
    loader = "notebook:nb_silver_loader"
    w = [e for e in edges(doc, "writes", loader) if e["to"] == "table:silver_shop.orders_cfg"]
    r = [e for e in edges(doc, "reads", loader) if e["to"] == "table:bronze_shop.customers"]
    assert w and r
    assert w[0]["evidence"][0]["via"] == "config resolver" and r[0]["evidence"][0]["via"] == "config resolver"


def test_column_lineage_follows_the_query_and_honors_except_columns(doc):
    cols = doc["columns"]["edges"]
    got = {(e["to"].removeprefix("table:"), e["toColumn"], e["from"].removeprefix("table:"), e["fromColumn"], e["transform"])
           for e in cols if e["job"] == "notebook:nb_silver_loader"}
    assert got == {
        ("silver_shop.orders_cfg", "order_id", "bronze_shop.orders", "order_id", "direct"),
        ("silver_shop.orders_cfg", "region", "bronze_shop.customers", "region", "direct"),
        ("silver_shop.totals_cfg", "customer_id", "silver_shop.orders", "customer_id", "direct"),
        ("silver_shop.totals_cfg", "total", "silver_shop.orders", "amount", "aggregate"),   # SUM inside the CTE
    }
    assert not [e for e in cols if e["toColumn"] == "name" and e["to"] == "table:silver_shop.orders_cfg"]  # except_columns


def test_a_notebook_that_only_reads_the_config_is_not_taken_for_the_loader(tmp_path):
    import shutil
    ws = tmp_path / "ws"
    shutil.copytree(ROOT / "synthetic_workspace", ws)
    # a second notebook reads the config table and writes some dynamic table, but never uses its
    # table_name / query columns
    nb = ws / "notebooks" / "nb_config_checker.Notebook"
    nb.mkdir()
    (nb / ".platform").write_text('{"metadata":{"displayName":"nb_config_checker"},"config":{"logicalId":"c1"}}')
    (nb / "notebook-content.py").write_text(
        "# Fabric notebook source\n\n# CELL ********************\n\n"
        'cfg = lakehouse_table_path("cfg", "silver_load")\nfor row in rows:\n    out = row["name"]\n'
        '    spark.sql("SELECT 1").write.saveAsTable(out)\n')
    d = extract(ws, schema=SCHEMA, config_tables=[CFG]).to_dict()
    assert {f["job"] for f in d["flows"]} == {"notebook:nb_silver_loader"}


def test_without_a_config_table_nothing_changes():
    d = extract(ROOT / "synthetic_workspace", schema=SCHEMA).to_dict()
    assert "flows" not in d
    assert not [n for n in d["nodes"] if n["name"] == "silver_shop.orders_cfg"]


def test_without_a_loader_notebook_flows_are_attributed_to_the_config_table(tmp_path):
    import json
    nb = tmp_path / "nb_other.Notebook"
    nb.mkdir()
    (nb / ".platform").write_text('{"metadata":{"displayName":"nb_other"},"config":{"logicalId":"1"}}')
    (nb / "notebook-content.py").write_text("# Fabric notebook source\n")
    cfg = tmp_path / "t.json"
    cfg.write_text(json.dumps({"schemaVersion": 1, "table": "cfg.x", "rows": [
        {"id": 1, "table_name": "s.out", "query": "SELECT a FROM s.src", "enabled": True, "is_notebook": False}]}))
    d = extract(tmp_path, config_tables=[str(cfg)], column_lineage=True, schema={"s.src": ["a"]}).to_dict()
    assert [(f["from"], f["to"], f["job"]) for f in d["flows"]] == [("table:s.src", "table:s.out", "config:cfg.x")]
    assert [e["job"] for e in d["columns"]["edges"]] == ["config:cfg.x"]


def test_query_columns_resolve_against_the_schema_when_given_a_star():
    g = extract(ROOT / "synthetic_workspace", schema={**SCHEMA}, config_tables=[CFG]).to_dict()
    # row 3 (SELECT *) is disabled, so no star edges appear for it
    assert not [e for e in g["columns"]["edges"] if e["to"] == "table:silver_shop.disabled_out"]
    assert isinstance(ColumnSchema(SCHEMA).tables, dict)
