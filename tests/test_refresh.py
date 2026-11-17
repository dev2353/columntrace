"""The refresh command: settings, which steps run, the offline re-extract and the change report."""

import json
from pathlib import Path

import pytest

from columntrace import refresh as rf

FIX = Path(__file__).resolve().parents[1] / "fixtures"


def settings_file(tmp_path, body: str) -> Path:
    f = tmp_path / "refresh.toml"
    f.write_text(body, encoding="utf-8")
    return f


FULL = f"""
[repo]
path = "{(FIX / 'synthetic_workspace').as_posix()}"
pull = false

[dates]
org = "o"
project = "p"
repo = "r"
out = "{(FIX / 'synthetic_dates.json').as_posix()}"

[schema]
server = "x"
database = "lh"
out = "{(FIX / 'synthetic_schema.json').as_posix()}"
ignore = ["sandbox"]

[[config_tables]]
table = "cfg.silver_load"
workspace = "w"
lakehouse = "l"
out = "{(FIX / 'synthetic_config_table.json').as_posix()}"

[extract]
out = "out/lineage.private.json"
"""


def test_settings_resolve_paths_next_to_the_file_and_check_required_keys(tmp_path):
    s = rf.load_settings(settings_file(tmp_path, FULL))
    assert s.out == (tmp_path / "out" / "lineage.private.json").resolve()
    assert s.schema["ignore"] == ["sandbox"] and s.config_tables[0]["table"] == "cfg.silver_load"
    with pytest.raises(rf.RefreshError, match="repo"):
        rf.load_settings(settings_file(tmp_path, "[extract]\nout = 'x.json'\n"))
    with pytest.raises(rf.RefreshError, match="server"):
        rf.load_settings(settings_file(tmp_path, "[repo]\npath = '.'\n[schema]\nout = 's.json'\n"))
    with pytest.raises(rf.RefreshError, match="workspace"):
        rf.load_settings(settings_file(tmp_path, "[repo]\npath = '.'\n[[config_tables]]\ntable = 'cfg.x'\nout = 'c.json'\n"))


def test_plan_follows_the_settings_the_skip_list_and_offline(tmp_path):
    s = rf.load_settings(settings_file(tmp_path, FULL))
    assert rf.plan(s, set(), False) == ["dates", "schema", "config", "extract"]       # pull = false
    assert rf.plan(s, {"dates", "schema"}, False) == ["config", "extract"]
    assert rf.plan(s, set(), True) == ["extract"]
    bare = rf.load_settings(settings_file(tmp_path, "[repo]\npath = '.'\n"))
    assert rf.plan(bare, set(), False) == ["pull", "extract"]


def test_offline_refresh_extracts_keeps_the_previous_file_and_reports_changes(tmp_path):
    s = rf.load_settings(settings_file(tmp_path, FULL))
    lines: list[str] = []
    assert rf.run(s, ["extract"], log=lines.append) is None                         # first run: nothing to compare
    first = json.loads(s.out.read_text(encoding="utf-8"))
    assert "columns" in first and first["flows"]

    # pretend the last run knew a table that is gone now and lacked one that is new
    first["nodes"].append({"id": "table:old_schema.dropped", "type": "table", "name": "old_schema.dropped", "meta": {}})
    first["nodes"] = [n for n in first["nodes"] if n["name"] != "gold_shop.order_flags"]
    s.out.write_text(json.dumps(first), encoding="utf-8")
    c = rf.run(s, ["extract"], log=lines.append)
    assert rf.previous_path(s.out).exists() and rf.previous_path(s.out).name == "lineage.previous.private.json"
    assert c["tables"]["added"] == ["gold_shop.order_flags"] and c["tables"]["removed"] == ["old_schema.dropped"]
    report = (tmp_path / "out" / "lineage.changes.private.md").read_text(encoding="utf-8")
    assert "gold_shop.order_flags" in report and "old_schema.dropped" in report
    assert any(line.startswith("[extract]") for line in lines)


def test_a_failing_step_stops_the_run_and_names_the_step(tmp_path):
    s = rf.load_settings(settings_file(tmp_path, FULL))
    ran = []

    def boom(_s):
        raise RuntimeError("401 unauthorized")

    with pytest.raises(rf.RefreshError, match=r"\[schema\] failed: RuntimeError: 401"):
        rf.run(s, ["schema", "extract"], log=ran.append, runners={**rf.RUNNERS, "schema": boom})
    assert not s.out.exists()                                                     # extract never ran


def test_extract_needs_the_files_of_skipped_steps(tmp_path):
    body = FULL.replace((FIX / "synthetic_schema.json").as_posix(), (tmp_path / "missing.json").as_posix())
    s = rf.load_settings(settings_file(tmp_path, body))
    with pytest.raises(rf.RefreshError, match="does not exist yet"):
        rf.run(s, ["extract"], log=lambda _x: None)


def test_compare_counts_links_columns_and_only_new_warnings():
    old = {"nodes": [{"name": "a.t", "type": "table"}, {"name": "nb", "type": "notebook"}],
           "edges": [{"from": "notebook:nb", "to": "table:a.t", "kind": "writes", "confidence": "parsed"}],
           "warnings": [{"item": "nb", "message": "old"}], "columns": {"stats": {"edges": 5}}}
    new = {"nodes": old["nodes"] + [{"name": "nb2", "type": "notebook"}],
           "edges": old["edges"] + [{"from": "notebook:nb2", "to": "table:a.t", "kind": "reads", "confidence": "parsed"},
                                    {"from": "notebook:nb2", "to": "unresolved:x", "kind": "writes", "confidence": "unresolved"}],
           "warnings": old["warnings"] + [{"item": "nb2", "message": "new"}, {"item": "config table cfg.x", "message": "added 3"}],
           "columns": {"stats": {"edges": 9}}}
    c = rf.compare(old, new)
    assert c["notebooks"]["added"] == ["nb2"] and c["links"] == {"before": 1, "after": 2, "added": 1, "removed": 0}
    assert c["columnEdges"] == {"before": 5, "after": 9} and c["newWarnings"] == ["nb2: new"]
    assert "(+4)" in rf.report_markdown(c)


def test_cli_rejects_unknown_skip_steps(tmp_path, capsys):
    assert rf.main(str(settings_file(tmp_path, FULL)), skip="schmea") == 2
    assert "unknown step" in capsys.readouterr().err


def test_inputs_record_the_repo_commit_and_when_each_file_was_made(tmp_path):
    import subprocess

    from columntrace.extract import describe_inputs
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.txt").write_text("x")
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com"]
    subprocess.run([*git, "init", "-q", "-b", "main"], check=True)
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "c"], check=True)
    inp = describe_inputs(repo, str(FIX / "synthetic_dates.json"), str(FIX / "synthetic_schema.json"),
                          [str(FIX / "synthetic_config_table.json")])
    assert inp["repo"]["branch"] == "main" and len(inp["repo"]["commit"]) == 12 and inp["repo"]["date"]
    assert inp["dates"]["fetchedAt"] and inp["schema"]["exportedAt"]
    assert inp["configTables"][0]["table"] == "cfg.silver_load"
    assert "repo" not in describe_inputs(tmp_path)                        # not a clone: no repo entry
