"""Phase 1 acceptance on the synthetic workspace (no real data).

Acceptance: >=90% precision and >=90% recall of table-level edges against the
hand-labelled ground truth; every unresolved reference is shown, none dropped.
"""

import json
from pathlib import Path

import pytest

from columntrace.extract import extract
from columntrace.impact import FlowGraph

ROOT = Path(__file__).resolve().parents[1] / "fixtures"
WORKSPACE = ROOT / "synthetic_workspace"


@pytest.fixture(scope="module")
def graph():
    return extract(WORKSPACE)


@pytest.fixture(scope="module")
def data(graph):
    return graph.to_dict()


@pytest.fixture(scope="module")
def truth():
    return json.loads((ROOT / "ground_truth.json").read_text(encoding="utf-8"))


def _found_edges(data):
    return {(e["from"], e["to"], e["kind"]) for e in data["edges"] if e["confidence"] != "unresolved"}


def _unresolved(data):
    names = {n["id"]: n["name"] for n in data["nodes"]}
    return {(e["from"], e["kind"], names[e["to"]]) for e in data["edges"] if e["confidence"] == "unresolved"}


def test_recall_and_precision_of_resolved_edges(data, truth):
    expected = {tuple(e) for e in truth["edges"]}
    found = _found_edges(data)
    missing, extra = expected - found, found - expected
    recall = 1 - len(missing) / len(expected)
    precision = 1 - len(extra) / len(found)
    assert not missing, f"missed edges: {sorted(missing)}"
    assert not extra, f"unexpected edges: {sorted(extra)}"
    assert recall >= 0.9 and precision >= 0.9


def test_every_unresolved_reference_is_shown(data, truth):
    expected = {tuple(u) for u in truth["unresolved"]}
    found = _unresolved(data)
    assert expected <= found, f"dropped unresolved: {sorted(expected - found)}"
    assert found <= expected, f"unexpected unresolved: {sorted(found - expected)}"


def test_no_false_positives_from_comments_strings_ctes_or_temp_views(data, truth):
    table_names = {n["name"] for n in data["nodes"] if n["type"] == "table"}
    for bad in truth["forbidden_tables"]:
        assert bad not in table_names


def test_config_resolver_edges_are_marked_inferred(data):
    inferred = {(e["from"], e["to"], e["kind"]) for e in data["edges"] if e["confidence"] == "inferred"}
    assert ("notebook:nb_generic_load", "table:bronze_shop.orders", "writes") in inferred
    assert ("notebook:nb_generic_load", "source:shop.customers", "reads") in inferred
    # the disabled config row (id 3) must not appear
    assert not any("returns" in e["to"] for e in data["edges"])


def test_broken_notebook_uses_regex_fallback_and_warns(data):
    nb = next(n for n in data["nodes"] if n["id"] == "notebook:nb_broken_syntax")
    assert nb["meta"]["parseErrors"] == 1
    edge = next(e for e in data["edges"]
                if e["from"] == "notebook:nb_broken_syntax" and e["to"] == "table:silver_shop.broken_out")
    assert edge["confidence"] == "inferred"
    assert any(w["item"] == "nb_broken_syntax" for w in data["warnings"])


def test_pipeline_notebook_ids_resolve_by_logical_id(data):
    calls = {(e["from"], e["to"]) for e in data["edges"] if e["kind"] == "calls" and e["confidence"] == "parsed"}
    assert ("pipeline:pl_main", "notebook:nb_ingest_orders") in calls
    assert ("pipeline:pl_main", "pipeline:pl_copy") in calls


def test_impact_downstream_of_bronze_orders(data):
    fg = FlowGraph(data)
    start = fg.resolve("bronze_shop.orders")
    names = {r["name"] for r in fg.walk(start, "down")}
    assert {"silver_shop.orders", "gold_shop.revenue_daily", "dwh_stage.revenue_daily",
            "Shop Model.revenue_daily"} <= names
    # orchestration: a pipeline that calls an affected notebook is reported, but it
    # does not fan out to every other notebook it calls (nb_report_view is only
    # downstream via the table it reads, not via pl_main)
    assert "pl_main" in names
    assert "nb_helper_loader" in names          # reads silver_shop.orders (downstream)
    assert "nb_dynamic_export" not in names     # only an unresolved read: never claimed


def test_impact_upstream_of_semantic_table(data):
    fg = FlowGraph(data)
    start = fg.resolve("Shop Model.revenue_daily")
    names = {r["name"] for r in fg.walk(start, "up")}
    assert {"gold_shop.revenue_daily", "nb_gold_revenue", "silver_shop.orders", "bronze_shop.orders"} <= names


def test_impact_report_markdown(data):
    from columntrace.impact import impact_report_markdown
    fg = FlowGraph(data)
    md = impact_report_markdown(fg, fg.resolve("bronze_shop.orders"))
    assert md.startswith("# Impact report: bronze_shop.orders")
    assert "Downstream: breaks if this changes" in md and "gold_shop.revenue_daily" in md


def test_impact_confidence_filter_shrinks_result(data):
    fg = FlowGraph(data)
    start = fg.resolve("bronze_shop.customers")
    strict = {r["id"] for r in fg.walk(start, "down", min_confidence="parsed")}
    loose = {r["id"] for r in fg.walk(start, "down", min_confidence="inferred")}
    assert strict <= loose
    assert all(r["confidence"] == "parsed" for r in fg.walk(start, "down", min_confidence="parsed"))


def test_four_part_names_and_sql_literals(data):
    edges = {(e["from"], e["to"], e["kind"]) for e in data["edges"]}
    assert ("notebook:nb_four_part", "table:silver_shop.orders", "reads") in edges
    assert ("notebook:nb_four_part", "table:gold_shop.four_part_out", "writes") in edges
    assert ("notebook:nb_literal_sql", "table:gold_shop.stale_rows", "writes") in edges


def test_best_confidence_wins_when_edge_seen_twice(data):
    # nb_gold_revenue assigns the SQL to a variable (literal scan, inferred) and then runs
    # it via spark.sql (parsed): exactly one edge, with the better confidence.
    hits = [e for e in data["edges"] if e["from"] == "notebook:nb_gold_revenue"
            and e["to"] == "table:silver_shop.orders" and e["kind"] == "reads"]
    assert len(hits) == 1 and hits[0]["confidence"] == "parsed"


def test_evidence_present_and_removable(graph):
    with_ev = graph.to_dict(include_evidence=True)
    without = graph.to_dict(include_evidence=False)
    assert any(e.get("evidence") for e in with_ev["edges"])
    assert all("evidence" not in e for e in without["edges"])


def test_sql_helpers_and_literal_loops_need_no_warning_and_others_name_the_line(data):
    items = {w["item"]: w["message"] for w in data["warnings"]}
    assert "nb_sql_helpers" not in items                    # run_sql(...) and `for label, sql in STEPS` resolve
    assert "built at run time (line 15: row['query'])" in items["nb_silver_loader"]   # truly dynamic: says where


def test_cells_in_other_languages_are_scanned_but_never_parsed_as_python():
    from columntrace.notebooks import parse_notebook
    text = (
        "# Fabric notebook source\n\n# CELL ********************\n\n"
        "# MAGIC %%spark\n# MAGIC val df = spark.sql(\"SELECT * FROM silver_x.src\")\n"
        "# MAGIC df.write.saveAsTable(\"gold_x.out\")\n\n"
        "# CELL ********************\n\n# MAGIC %%configure\n# MAGIC { \"conf\": { \"spark.x\": \"true\" } }\n\n"
        "# CELL ********************\n\nspark.sql(\"SELECT 1 FROM silver_x.other\")\n"
    )
    info = parse_notebook("nb.py", "nb.Notebook", text)
    assert info.parse_errors == 0 and info.other_cells == 2
    found = {(h.name, h.mode, h.confidence) for h in info.hits}
    assert ("gold_x.out", "write", "inferred") in found and ("silver_x.src", "read", "inferred") in found


def test_a_collection_that_refers_to_itself_does_not_crash_the_parser():
    from columntrace.notebooks import parse_notebook
    text = ("# Fabric notebook source\n\n# CELL ********************\n\n"
            "params = {'a': 'x'}\nparams = {'next': params, 'b': params['a']}\n"
            "steps = [steps, 'y']\nspark.sql(params['b'])\nspark.sql(steps[0])\n")
    info = parse_notebook("nb.py", "nb.Notebook", text)
    assert info.parse_errors == 0


def test_bare_names_are_tables_only_when_written_or_known_in_dbo(tmp_path):
    nb = tmp_path / "nb_bare.Notebook"
    nb.mkdir()
    (nb / ".platform").write_text('{"metadata":{"displayName":"nb_bare"},"config":{"logicalId":"b1"}}')
    (nb / "notebook-content.py").write_text(
        "# Fabric notebook source\n\n# CELL ********************\n\n"
        'spark.sql("CREATE SCHEMA IF NOT EXISTS cfg")\n'
        'spark.sql("CREATE TABLE plain_tbl AS SELECT x FROM silver_x.src")\n'
        'spark.sql("SELECT * FROM step_result")\n'
        'spark.sql("SELECT * FROM known_dbo")\n')
    d = extract(tmp_path, schema={"dbo.known_dbo": ["c"], "silver_x.src": ["x"]}).to_dict()
    tables = {n["name"] for n in d["nodes"] if n["type"] == "table"}
    assert {"plain_tbl", "known_dbo", "silver_x.src"} <= tables          # written, or in the default schema
    assert "step_result" not in tables and "cfg" not in tables          # a relation name, and a schema
    assert d["meta"]["annotations"]["unqualifiedSetAside"] == {"names": 1, "byItem": {"notebook:nb_bare": ["step_result"]}}


def test_keywords_unset_parameters_and_duckdb_tables_are_not_lakehouse_tables(tmp_path):
    from columntrace.sql_refs import extract_refs
    refs, fallback = extract_refs("MERGE INTO a.b T USING a.c S ON (( WHEN MATCHED THEN UPDATE SET x = 1", set())
    assert fallback and "set" not in {r.name for r in refs} and "a.b" in {r.name for r in refs}

    nb = tmp_path / "nb_odd.Notebook"
    nb.mkdir()
    (nb / ".platform").write_text('{"metadata":{"displayName":"nb_odd"},"config":{"logicalId":"o1"}}')
    (nb / "notebook-content.py").write_text(
        "# Fabric notebook source\n\n# CELL ********************\n\n"
        'db = ""\n'
        'spark.sql(f"CREATE TABLE {db}.tmp_x AS SELECT x FROM silver_x.src")\n'
        'con.execute("CREATE OR REPLACE TABLE _mat AS SELECT 1 AS x")\n'
        'con.execute("CREATE OR REPLACE TABLE gold_x.out AS SELECT * FROM _mat")\n')
    d = extract(tmp_path).to_dict()
    tables = {n["name"] for n in d["nodes"] if n["type"] == "table"}
    assert ".tmp_x" not in tables and "_mat" not in tables
    assert "gold_x.out" in tables                                    # a schema-qualified DuckDB write is real
    assert any(n["type"] == "unknown" and ".tmp_x" in n["name"] for n in d["nodes"])   # shown as unresolved


def test_tables_are_compared_with_the_lakehouse_when_a_schema_export_is_given(tmp_path):
    nb = tmp_path / "nb_cmp.Notebook"
    nb.mkdir()
    (nb / ".platform").write_text('{"metadata":{"displayName":"nb_cmp"},"config":{"logicalId":"c2"}}')
    (nb / "notebook-content.py").write_text(
        "# Fabric notebook source\n\n# CELL ********************\n\n"
        'spark.sql("INSERT INTO gold_x.out SELECT x FROM silver_x.src JOIN silver_x.gone ON 1 = 1")\n'
        'spark.sql("CREATE TABLE plain_tbl AS SELECT x FROM known_dbo")\n')
    d = extract(tmp_path, schema={"silver_x.src": ["x"], "gold_x.out": ["x"], "dbo.known_dbo": ["x"]}).to_dict()
    flag = {n["name"]: n["meta"].get("inLakehouse") for n in d["nodes"] if n["type"] == "table"}
    assert flag == {"silver_x.src": True, "gold_x.out": True, "known_dbo": True,
                    "silver_x.gone": False,                 # read by the code, not in the lakehouse
                    "plain_tbl": False}                     # written by the code, not in the export (yet)
    assert d["meta"]["annotations"]["lakehouse"] == {"checked": 5, "missing": 2, "defaultSchema": "dbo"}
    plain = extract(tmp_path).to_dict()                     # no schema export: nothing is marked
    assert all("inLakehouse" not in n["meta"] for n in plain["nodes"]) and "lakehouse" not in plain["meta"]["annotations"]


def test_helpers_returning_names_loops_into_fstrings_and_relative_delta_paths():
    from columntrace.notebooks import parse_notebook
    text = (
        "# Fabric notebook source\n\n# CELL ********************\n\n"
        "LH = f\"`{param_ws}`.`{param_lh}`\"\n"
        "def obj(schema, name):\n    \"\"\"qualified name\"\"\"\n    return f\"{LH}.`{schema}`.`{name}`\"\n"
        "TARGET = obj('gold_x', 'report_mlv')\nSRC = obj('silver_x', 'facts')\n"
        "spark.sql(f\"CREATE MATERIALIZED LAKE VIEW IF NOT EXISTS {TARGET} AS SELECT * FROM {SRC}\")\n"
        "tables = ['silver_x.snap_a', 'silver_x.snap_b']\n"
        "for tbl in tables:\n    spark.sql(f\"MERGE INTO {tbl} AS t USING tmp_v AS b ON t.id = b.id WHEN MATCHED THEN UPDATE SET t.x = b.x\")\n"
        "dt = DeltaTable.forPath(spark, \"Tables/silver_x/discount_wip\")\n"
        "dt.alias('T').merge(df.alias('S'), 'T.id = S.id').whenMatchedUpdateAll().execute()\n"
    )
    info = parse_notebook("nb.py", "nb.Notebook", text)
    found = {(h.name, h.mode) for h in info.hits if h.confidence != "unresolved"}
    for want in [("gold_x.report_mlv", "write"), ("silver_x.facts", "read"), ("silver_x.snap_a", "write"),
                 ("silver_x.snap_b", "write"), ("silver_x.discount_wip", "write")]:
        assert want in found, (want, sorted(found))
    assert info.unresolved_sql == 0
