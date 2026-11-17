"""Liveness, staleness and variant families on the synthetic workspace."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from columntrace.extract import extract
from columntrace.gitdates import load_dates
from columntrace.liveness import annotate, family_key

ROOT = Path(__file__).resolve().parents[1] / "fixtures"
NOW = datetime(2026, 10, 24, tzinfo=UTC)


@pytest.fixture(scope="module")
def graph():
    return extract(ROOT / "synthetic_workspace", dates=load_dates(str(ROOT / "synthetic_dates.json")), now=NOW)


def meta(graph, node_id):
    return graph.nodes[node_id].meta


@pytest.mark.parametrize("node_id,expected", [
    ("pipeline:pl_main", "scheduled"),             # one enabled schedule (a disabled one is ignored)
    ("pipeline:pl_copy", "orchestrated"),          # called by the scheduled pipeline
    ("pipeline:pl_dynamic", "uncalled"),
    ("notebook:nb_ingest_orders", "orchestrated"),
    ("notebook:nb_gold_revenue", "orchestrated"),  # called inside a ForEach
    ("notebook:nb_generic_load", "orchestrated"),  # called inside an IfCondition
    ("notebook:nb_orchestrator", "entry-point"),   # never called, but calls others
    ("notebook:nb_helper_loader", "feeds-others"), # its output is read by a pipeline
    ("notebook:nb_silver_customers", "feeds-others"),
    ("notebook:nb_report_view", "dead-end"),
    ("notebook:nb_no_tables", "dead-end"),
    ("notebook:nb_dynamic_export", "dead-end"),    # only unresolved writes: never claimed as output
])
def test_liveness_status(graph, node_id, expected):
    assert meta(graph, node_id)["liveness"] == expected


def test_disabled_schedule_does_not_count(graph):
    assert meta(graph, "pipeline:pl_main")["schedules"] == {"enabled": 1, "total": 2}


def test_schedule_with_past_end_date_is_not_enabled(tmp_path):
    nb = tmp_path / "nb_x.Notebook"
    nb.mkdir()
    (nb / ".platform").write_text('{"metadata":{"displayName":"nb_x"},"config":{"logicalId":"1"}}')
    (nb / "notebook-content.py").write_text("# Fabric notebook source\n")
    (nb / ".schedules").write_text('{"schedules":[{"enabled":true,"configuration":'
                                   '{"endDateTime":"2020-01-01T00:00:00"}}]}')
    g = extract(tmp_path, now=NOW)
    assert g.nodes["notebook:nb_x"].meta["schedules"] == {"enabled": 0, "total": 1}


def test_variant_family_and_newest_member(graph):
    a, b = meta(graph, "notebook:nb_silver_orders"), meta(graph, "notebook:nb_silver_orders_python")
    assert a["family"] == b["family"] == "nb_silver_orders" and a["familyMembers"] == 2
    assert b["newestInFamily"] is True and a["newestInFamily"] is False
    assert "family" not in meta(graph, "notebook:nb_gold_revenue")


def test_variant_python_copy_that_nothing_calls_still_feeds_others(graph):
    assert meta(graph, "notebook:nb_silver_orders_python")["liveness"] == "feeds-others"


def test_stale_flags_use_last_change_and_threshold(graph):
    assert meta(graph, "notebook:nb_no_tables")["stale"] is True
    assert meta(graph, "notebook:nb_report_view")["stale"] is True
    assert meta(graph, "notebook:nb_gold_revenue")["stale"] is False
    assert meta(graph, "notebook:nb_no_tables")["ageDays"] > 365


def test_bulk_commit_spread_is_flagged_when_many_items_share_a_commit():
    g = extract(ROOT / "synthetic_workspace", dates=load_dates(str(ROOT / "synthetic_dates.json")), now=NOW)
    annotate(g, load_dates(str(ROOT / "synthetic_dates.json")), NOW, bulk_threshold=10)
    assert g.nodes["notebook:nb_gold_revenue"].meta.get("bulkCommit") is True
    assert not g.nodes["notebook:nb_no_tables"].meta.get("bulkCommit")   # unique commit


def test_summary_counts_unmatched_items(graph):
    s = graph.annotations["liveness"]
    assert s["datesMatched"] == s["datesAvailable"] and s["itemsWithoutDate"] == 0
    assert s["families"] >= 1 and s["stale"] == 2


def test_runs_without_dates():
    g = extract(ROOT / "synthetic_workspace", now=NOW)
    assert g.nodes["notebook:nb_gold_revenue"].meta["liveness"] == "orchestrated"
    assert "lastChange" not in g.nodes["notebook:nb_gold_revenue"].meta
    assert g.annotations["liveness"]["itemsWithoutDate"] is None


@pytest.mark.parametrize("name,key", [
    ("nb_generic_bronze_load_python", "nb_generic_bronze_load"),
    ("nb_generic_bronze_load_v3_tbd", "nb_generic_bronze_load"),
    ("SHOP_X_create_tables_V2", "shop_x_create_tables"),
    ("nb_load_data", "nb_load_data"),
])
def test_family_key(name, key):
    assert family_key(name) == key
