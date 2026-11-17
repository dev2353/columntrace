"""Command line: extract, refresh, the read-only exports, impact and stats."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from .extract import extract
from .gitdates import AdoClient, DateFetchError, fetch_dates, load_dates, save_dates
from .impact import FlowGraph, impact_report_markdown


def _cmd_extract(a: argparse.Namespace) -> int:
    repo = Path(a.repo)
    if not repo.is_dir():
        print(f"repo folder not found: {repo}", file=sys.stderr)
        return 2
    resolvers = tuple(x for x in a.resolvers.split(",") if x)
    dates = load_dates(a.dates) if a.dates else None
    schema = None
    if a.schema:
        from .schema_export import load_schema
        schema = load_schema(a.schema)
    ignore = [x.strip() for x in a.schema_ignore.split(",") if x.strip()]
    now = None
    if a.now:
        try:
            now = datetime.fromisoformat(a.now.replace("Z", "+00:00"))
        except ValueError:
            print(f"--now is not an ISO date: {a.now}", file=sys.stderr)
            return 2
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
    from .extract import describe_inputs
    inputs = describe_inputs(repo, a.dates, a.schema, a.config_table)
    g = extract(repo, resolvers, dates=dates, now=now, stale_days=a.stale_days, schema=schema,
                schema_ignore=ignore, column_lineage=a.columns, config_tables=a.config_table, inputs=inputs)
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    g.save(str(out), include_evidence=not a.no_evidence)
    print(json.dumps(g.stats(), indent=1))
    print(f"written: {out}")
    return 0


def _cmd_fetch_dates(a: argparse.Namespace) -> int:
    client = AdoClient(a.org, a.project, a.repo, a.branch)
    folders = [x.strip("/ ") for x in a.folders.split(",") if x.strip("/ ")] or None
    try:
        doc = fetch_dates(client, folders)
    except DateFetchError as exc:
        print(f"fetch-dates failed: {exc}", file=sys.stderr)
        return 2
    save_dates(doc, a.out)
    print(f"{len(doc['items'])} item folders, {doc['apiCalls']} API calls -> {a.out}")
    return 0


def _cmd_export_schema(a: argparse.Namespace) -> int:
    from . import schema_export as se
    try:
        conn = se.connect(a.server, a.database)
        try:
            rows = se.fetch_rows(conn)
        finally:
            conn.close()
    except se.SchemaExportError as exc:
        print(f"export-schema failed: {exc}", file=sys.stderr)
        return 2
    doc = se.to_document(rows)
    se.save_json(doc, a.out)
    if a.csv:
        se.save_csv(rows, a.csv)
    c = doc["counts"]
    print(f"{c['tables']} tables ({c['userTables']} outside system schemas), {c['columns']} columns -> {a.out}")
    return 0


def _cmd_export_table(a: argparse.Namespace) -> int:
    from . import schema_export as se
    from . import table_export as te
    try:
        te.build_query(a.table, a.max_rows)                  # validate before connecting
        conn = se.connect(a.server, a.database)
        try:
            cols, rows, truncated = te.fetch_table(conn, a.table, a.max_rows)
        finally:
            conn.close()
    except se.SchemaExportError as exc:
        print(f"export-table failed: {exc}", file=sys.stderr)
        return 2
    doc = te.to_document(a.table, cols, rows, truncated)
    te.save_json(doc, a.out)
    note = f" (truncated at {a.max_rows}; raise --max-rows)" if truncated else ""
    print(f"{len(rows)} rows, {len(cols)} columns{note} -> {a.out}")
    if doc["cutCells"]:
        cut = sorted({c["column"] for c in doc["cutCells"]})
        print(f"warning: {len(doc['cutCells'])} value(s) in {', '.join(cut)} are exactly {te.TEXT_LIMIT} characters: "
              "the SQL endpoint cuts longer text. Use export-delta to read them whole.", file=sys.stderr)
    return 0


def _cmd_export_delta(a: argparse.Namespace) -> int:
    from . import delta_export as de
    from . import table_export as te
    from .schema_export import SchemaExportError
    try:
        cols, rows, truncated = de.fetch_table(a.workspace, a.lakehouse, a.table, a.max_rows)
    except SchemaExportError as exc:
        print(f"export-delta failed: {exc}", file=sys.stderr)
        return 2
    te.save_json(de.to_delta_document(a.table, cols, rows, truncated), a.out)
    note = f" (truncated at {a.max_rows}; raise --max-rows)" if truncated else ""
    longest = max((len(v) for r in rows for v in r if isinstance(v, str)), default=0)
    print(f"{len(rows)} rows, {len(cols)} columns{note}, longest text {longest} characters -> {a.out}")
    return 0


def _cmd_refresh(a: argparse.Namespace) -> int:
    from .refresh import main as refresh
    return refresh(a.config, a.skip, a.offline)


def _load(path: str) -> FlowGraph:
    return FlowGraph(json.loads(Path(path).read_text(encoding="utf-8")))


def _cmd_impact(a: argparse.Namespace) -> int:
    fg = _load(a.lineage)
    node = fg.resolve(a.node)
    if node is None:
        print(f"no unique node matches '{a.node}'", file=sys.stderr)
        return 2
    print(impact_report_markdown(fg, node, a.confidence))
    return 0


def _cmd_stats(a: argparse.Namespace) -> int:
    data = json.loads(Path(a.lineage).read_text(encoding="utf-8"))
    print(json.dumps(data["meta"]["stats"], indent=1))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="columntrace")
    sub = p.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("extract", help="build lineage.json from a Fabric git folder")
    e.add_argument("--repo", required=True)
    e.add_argument("--out", required=True)
    e.add_argument("--resolvers", default="load_cfg", help="comma list; '' to disable")
    e.add_argument("--no-evidence", action="store_true", help="omit code snippets (safer to share)")
    e.add_argument("--dates", help="dates JSON from `fetch-dates` (adds lastChange and stale flags)")
    e.add_argument("--stale-days", type=int, default=365, help="age after which an item is flagged stale")
    e.add_argument("--schema", help="schema JSON from `export-schema`: expands SELECT * and turns on column lineage")
    e.add_argument("--schema-ignore", default="",
                   help="comma list of table-name patterns to leave out of the schema (substring, or * wildcard), "
                        "e.g. '_uat,staging.*'")
    e.add_argument("--now", help="ISO date to use as 'today' (ages, stale flags, generatedAt): reproducible output")
    e.add_argument("--columns", action="store_true", help="column lineage even without a schema")
    e.add_argument("--config-table", action="append", default=[], metavar="FILE",
                   help="file from `export-table` (repeatable): rows mapping a target table to a SELECT or a notebook")
    e.set_defaults(fn=_cmd_extract)
    f = sub.add_parser("fetch-dates", help="read last-change dates per item folder from Azure DevOps")
    f.add_argument("--org", required=True, help="Azure DevOps organization name")
    f.add_argument("--project", required=True)
    f.add_argument("--repo", required=True)
    f.add_argument("--branch", default="main")
    f.add_argument("--folders", default="", help="comma list of top-level folders (default: all)")
    f.add_argument("--out", required=True)
    f.set_defaults(fn=_cmd_fetch_dates)
    x = sub.add_parser("export-schema", help="export every table and column from a lakehouse SQL endpoint (read-only)")
    x.add_argument("--server", required=True, help="SQL endpoint host, e.g. <id>.datawarehouse.fabric.microsoft.com")
    x.add_argument("--database", required=True, help="lakehouse name")
    x.add_argument("--out", required=True, help="JSON output")
    x.add_argument("--csv", help="optional flat CSV of the raw rows")
    x.set_defaults(fn=_cmd_export_schema)
    t = sub.add_parser("export-table", help="export the rows of one small config table (read-only SELECT)")
    t.add_argument("--server", required=True)
    t.add_argument("--database", required=True)
    t.add_argument("--table", required=True, help="schema.table, e.g. cfg.my_config")
    t.add_argument("--out", required=True)
    t.add_argument("--max-rows", type=int, default=50_000)
    t.set_defaults(fn=_cmd_export_table)
    d = sub.add_parser("export-delta",
                       help="export one config table from its Delta files in OneLake (read-only; long text whole)")
    d.add_argument("--workspace", required=True, help="workspace id or name")
    d.add_argument("--lakehouse", required=True, help="lakehouse id or name")
    d.add_argument("--table", required=True, help="schema.table, e.g. cfg.my_config")
    d.add_argument("--out", required=True)
    d.add_argument("--max-rows", type=int, default=50_000)
    d.set_defaults(fn=_cmd_export_delta)
    r = sub.add_parser("refresh", help="update every input and lineage.json from one settings file, then report changes")
    r.add_argument("--config", required=True, help="TOML settings (see refresh.example.toml); keep it private")
    r.add_argument("--skip", default="", help="comma list of steps to skip: pull, dates, schema, config, extract")
    r.add_argument("--offline", action="store_true", help="no network: re-extract from the files of the last run")
    r.set_defaults(fn=_cmd_refresh)
    i = sub.add_parser("impact", help="what is upstream/downstream of a node")
    i.add_argument("lineage")
    i.add_argument("node", help="e.g. silver_shop.orders or notebook:nb_x")
    i.add_argument("--confidence", choices=["parsed", "inferred"], default="inferred",
                   help="weakest edge allowed; 'parsed' is the strict view")
    i.set_defaults(fn=_cmd_impact)
    s = sub.add_parser("stats")
    s.add_argument("lineage")
    s.set_defaults(fn=_cmd_stats)
    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
