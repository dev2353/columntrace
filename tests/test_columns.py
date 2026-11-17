"""Column-level lineage on the synthetic workspace (no real data)."""

import json
from pathlib import Path

import pytest

from columntrace.columns import ColumnSchema, analyze_notebook
from columntrace.extract import extract
from columntrace.notebooks import Stmt
from columntrace.schema_export import load_schema

ROOT = Path(__file__).resolve().parents[1] / "fixtures"


@pytest.fixture(scope="module")
def graph():
    schema = load_schema(str(ROOT / "synthetic_schema.json"))
    return extract(ROOT / "synthetic_workspace", schema=schema, schema_ignore=["sandbox"])


@pytest.fixture(scope="module")
def columns(graph):
    return graph.to_dict()["columns"]


def key(e):
    return (e["to"].removeprefix("table:"), e["toColumn"], e["from"].removeprefix("table:"), e["fromColumn"])


def find(columns, to, to_col, frm=None, job=None):
    return [e for e in columns["edges"]
            if e["to"] == f"table:{to}" and e["toColumn"] == to_col
            and (frm is None or e["from"] == f"table:{frm}") and (job is None or e["job"] == f"notebook:{job}")]


def test_recall_and_precision_against_ground_truth(columns):
    truth = json.loads((ROOT / "ground_truth_columns.json").read_text(encoding="utf-8"))["edges"]
    expected = {(t, tc, f, fc) for t, tc, f, fc, _tr, _cf in truth}
    found = {key(e) for e in columns["edges"]}
    assert not expected - found, f"missed: {sorted(expected - found)}"
    assert not found - expected, f"unexpected: {sorted(found - expected)}"


def test_transform_and_confidence_match_ground_truth(columns):
    truth = json.loads((ROOT / "ground_truth_columns.json").read_text(encoding="utf-8"))["edges"]
    got = {}
    for e in columns["edges"]:
        got.setdefault(key(e), set()).add((e["transform"], e["confidence"]))
    for t, tc, f, fc, transform, conf in truth:
        assert (transform, conf) in got[(t, tc, f, fc)], (t, tc, f, fc, got[(t, tc, f, fc)])


def test_temp_view_and_dataframe_save_are_followed(columns):
    # df = spark.sql("SELECT * FROM v_orders"); df.write.saveAsTable("silver_shop.orders"),
    # where v_orders is a temp view over a join: lineage runs through the view to the bronze tables
    for job in ("nb_silver_orders", "nb_silver_orders_python"):
        assert find(columns, "silver_shop.orders", "order_id", "bronze_shop.orders", job)
        assert find(columns, "silver_shop.orders", "region", "bronze_shop.customers", job)


def test_aggregate_expression_and_constant_handling(columns):
    amount = find(columns, "gold_shop.revenue_daily", "amount", "silver_shop.orders")[0]
    assert amount["transform"] == "aggregate" and "SUM" in amount["expr"].upper()
    is_big = find(columns, "gold_shop.order_flags", "is_big")
    assert [e["from"] for e in is_big] == ["table:silver_shop.orders"] and is_big[0]["fromColumn"] == "amount"
    # the 'x' constant in 'x' || region contributes no source column
    assert {e["fromColumn"] for e in find(columns, "gold_shop.order_flags", "label")} == {"region"}
    assert not find(columns, "gold_shop.all_events", "src")        # 'a' / 'b' constants


def test_the_strongest_transform_and_its_expression_survive_outer_select_stars(columns):
    # region is UPPER(c.region) in the query, then passed through a temp view and `SELECT *`
    e = find(columns, "silver_shop.param_out", "region", "bronze_shop.customers")[0]
    assert e["transform"] == "expression" and "UPPER" in e["expr"].upper()
    plain = find(columns, "silver_shop.param_out", "order_id", "bronze_shop.orders")[0]
    assert plain["transform"] == "direct"


def test_parameter_defaults_and_split_unpacking_resolve_the_target_table(graph):
    d = graph.to_dict()
    writes = {(e["from"], e["to"]) for e in d["edges"] if e["kind"] == "writes" and e["confidence"] != "unresolved"}
    assert ("notebook:nb_param_pipeline", "table:silver_shop.param_out") in writes
    assert not [n for n in d["nodes"] if n["name"] == "stg_param"]           # the temp view is not a table


def test_union_feeds_one_column_from_both_branches(columns):
    assert {e["from"] for e in find(columns, "gold_shop.all_events", "id")} == {"table:staging.a", "table:staging.b"}


def test_merge_assignments_skip_the_target_itself(columns):
    total = find(columns, "silver_shop.order_totals", "total_amount")
    assert [(e["from"], e["fromColumn"]) for e in total] == [("table:silver_shop.orders", "amount")]
    assert not find(columns, "silver_shop.order_totals", "bonus")      # only read from the target


def test_star_expands_with_a_schema_and_stays_unresolved_without_one(columns):
    # staging.orders_raw has a known schema: SELECT * becomes four named edges
    assert {e["toColumn"] for e in find(columns, "bronze_shop.orders", "order_id") + find(columns, "bronze_shop.orders", "amount")} == {"order_id", "amount"}
    star = find(columns, "gold_shop.copy_unknown", "*")
    assert star and star[0]["fromColumn"] == "*" and star[0]["transform"] == "star" and star[0]["confidence"] == "inferred"
    assert columns["stats"]["unresolvedStars"] >= 1


def test_dynamic_targets_are_skipped_and_counted(columns):
    assert not [e for e in columns["edges"] if e["job"] == "notebook:nb_dynamic_export"]
    assert columns["stats"]["dynamicTarget"] >= 1


def test_ignored_schemas_are_not_exported_and_known_tables_carry_columns(columns):
    assert "table:sandbox.junk_table" not in columns["tables"]
    assert columns["tables"]["table:silver_shop.orders"] == ["order_id", "customer_id", "order_date", "amount", "region"]
    assert columns["stats"]["tablesWithKnownColumns"] == len(columns["tables"])


def test_stats_add_up(columns):
    s = columns["stats"]
    assert s["edges"] == len(columns["edges"]) == s["edgesParsed"] + s["edgesInferred"]
    assert s["failedParse"] == 0 and s["notebooksWithColumnEdges"] >= 8


def test_works_without_a_schema_for_explicit_column_lists():
    g = extract(ROOT / "synthetic_workspace", column_lineage=True)
    cols = g.to_dict()["columns"]
    assert find(cols, "gold_shop.order_flags", "is_big", "silver_shop.orders")
    star = [e for e in cols["edges"] if e["to"] == "table:bronze_shop.orders"]
    assert star and star[0]["fromColumn"] == "*"             # nothing to expand the star with


def test_no_column_section_unless_requested():
    assert "columns" not in extract(ROOT / "synthetic_workspace").to_dict()


def test_evidence_can_be_dropped():
    g = extract(ROOT / "synthetic_workspace", column_lineage=True)
    slim = g.to_dict(include_evidence=False)["columns"]["edges"][0]
    assert "expr" not in slim and "line" not in slim and "confidence" in slim


def test_schema_ignore_patterns_support_substrings_and_wildcards():
    tables = {"a_test.x": ["c"], "sandbox.y": ["c"], "silver_x.z": ["c"], "silver_x.tmp_1": ["c"]}
    cs = ColumnSchema(tables, ["_test", "sandbox", "silver_x.tmp_*"])
    assert set(cs.tables) == {"silver_x.z"}


def test_a_statement_that_does_not_parse_is_counted_not_fatal():
    edges, stats = analyze_notebook([Stmt("INSERT INTO a.b SELEC FROM", 1, "spark.sql"), Stmt("SELECT 1", 2, "spark.sql")],
                                    ColumnSchema({}))
    assert edges == [] and stats.statements >= 1


def test_unprefixed_columns_in_a_join_are_attributed_when_only_one_source_can_have_them():
    sql = "INSERT INTO g.o WITH c AS (SELECT a AS y FROM s.src) SELECT x, y FROM c JOIN u.unknown ON 1 = 1"
    stmts = [Stmt(sql, 1, "spark.sql")]
    edges, stats = analyze_notebook(stmts, ColumnSchema({"s.src": ["a"], "g.o": ["x", "y"]}))
    got = {(e.target_col, e.source, e.source_col, e.conf) for e in edges}
    assert ("y", "s.src", "a", "parsed") in got            # c visibly defines y: certain
    assert ("x", "u.unknown", "x", "inferred") in got      # u.unknown is the only source with unknown columns
    assert stats.ambiguous_columns == 0


def test_a_with_clause_in_front_of_insert_is_a_cte_not_a_table():
    sql = "WITH c AS (SELECT a AS y FROM s.src) INSERT INTO g.o SELECT y FROM c"
    edges, _ = analyze_notebook([Stmt(sql, 1, "spark.sql")], ColumnSchema({"s.src": ["a"], "g.o": ["y"]}))
    assert [(e.source, e.source_col, e.target, e.target_col, e.conf) for e in edges] == [("s.src", "a", "g.o", "y", "parsed")]
    assert not any(e.source == "c" for e in edges)                      # the CTE is never reported as a table


def test_a_column_that_two_known_sources_share_stays_ambiguous():
    stmts = [Stmt("INSERT INTO g.o SELECT id FROM s.a JOIN s.b ON 1 = 1", 1, "spark.sql")]
    edges, stats = analyze_notebook(stmts, ColumnSchema({"s.a": ["id"], "s.b": ["id"], "g.o": ["id"]}))
    assert edges == [] and stats.ambiguous_columns == 1


def test_temp_view_chain_defined_in_sql_cells():
    stmts = [
        Stmt("CREATE OR REPLACE TEMP VIEW v1 AS SELECT a AS x FROM s.t", 1, "%%sql"),
        Stmt("CREATE OR REPLACE TEMP VIEW v2 AS SELECT x AS y FROM v1", 2, "%%sql"),
        Stmt("INSERT INTO g.out SELECT y FROM v2", 3, "%%sql"),
    ]
    edges, _ = analyze_notebook(stmts, ColumnSchema({"s.t": ["a"], "g.out": ["y"]}))
    assert [(e.source, e.source_col, e.target, e.target_col) for e in edges] == [("s.t", "a", "g.out", "y")]


def test_temp_views_are_inlined_after_the_views_they_read():
    # the statement reads v1 first, then v2, and v2 itself reads v1: v1 must come first as a CTE
    stmts = [
        Stmt("CREATE OR REPLACE TEMP VIEW v1 AS SELECT a, b FROM s.t", 1, "%%sql"),
        Stmt("CREATE OR REPLACE TEMP VIEW v2 AS SELECT a AS a2 FROM v1", 2, "%%sql"),
        Stmt("INSERT INTO g.o SELECT v1.b, v2.a2 FROM v1 JOIN v2 ON 1 = 1", 3, "%%sql"),
    ]
    edges, _ = analyze_notebook(stmts, ColumnSchema({"s.t": ["a", "b"], "g.o": ["b", "a2"]}))
    assert sorted((e.source, e.source_col, e.target_col) for e in edges) == [("s.t", "a", "a2"), ("s.t", "b", "b")]


def test_many_statements_in_one_notebook_give_the_same_edges_every_time():
    # scopes of earlier statements are freed while later ones are analyzed; a cache keyed by
    # id(scope) must not hand a later statement a dead scope's columns
    stmts = [Stmt(f"INSERT INTO g.o{i} SELECT x.a, y.b FROM s.x x JOIN s.y y ON x.k = y.k", i, "spark.sql") for i in range(60)]
    schema = ColumnSchema({"s.x": ["a", "k"], "s.y": ["b", "k"], **{f"g.o{i}": ["a", "b"] for i in range(60)}})
    runs = [sorted((e.target, e.target_col, e.source) for e in analyze_notebook(stmts, schema)[0]) for _ in range(3)]
    assert len(runs[0]) == 120 and runs[0] == runs[1] == runs[2]
