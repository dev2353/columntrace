"""Walk a Fabric git-integration folder and build the lineage graph."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from . import liveness
from .columns import ColumnSchema
from .config_resolver import RESOLVERS, QueryConfigResolver
from .model import Graph, Node, table_id, unresolved_id
from .notebooks import parse_notebook
from .pipelines import parse_pipeline
from .semantic import parse_tmdl_table
from .table_export import TEXT_LIMIT

SKIP_DIRS = {".git", "node_modules", "__pycache__"}


def _git_head(repo: Path) -> dict | None:
    """Branch, commit and commit date of a git clone (None when it is not one)."""
    import subprocess
    if not (repo / ".git").exists():
        return None
    try:
        log = subprocess.run(["git", "-C", str(repo), "log", "-1", "--format=%H|%cI"], capture_output=True, text=True, timeout=30)
        branch = subprocess.run(["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True,
                                timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if log.returncode != 0 or "|" not in log.stdout:
        return None
    commit, date = log.stdout.strip().split("|", 1)
    return {"branch": branch.stdout.strip() or None, "commit": commit[:12], "date": date}


def describe_inputs(repo: Path | None = None, dates: str | None = None, schema: str | None = None,
                    config_tables: list[str] | None = None) -> dict:
    """How current each input is, read from the files themselves (they record when they were made)."""
    def head(path: str) -> dict:
        try:
            doc = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return {k: doc[k] for k in ("source", "table", "branch", "exportedAt", "fetchedAt") if doc.get(k) is not None}

    out: dict = {}
    if repo is not None and (git := _git_head(repo)):
        out["repo"] = git
    if dates:
        out["dates"] = head(dates)
    if schema:
        out["schema"] = head(schema)
    if config_tables:
        out["configTables"] = [head(c) for c in config_tables]
    return out


def _item_dirs(repo: Path, suffix: str):
    # sorted, so the output does not depend on the file system's listing order
    for p in sorted(repo.rglob(f"*.{suffix}")):
        if p.is_dir() and not (SKIP_DIRS & set(p.relative_to(repo).parts)):
            yield p


def _platform(item_dir: Path) -> tuple[str, str | None]:
    """(displayName, logicalId) from the item's .platform file."""
    fallback = item_dir.name.rsplit(".", 1)[0]
    f = item_dir / ".platform"
    if not f.exists():
        return fallback, None
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        return (data.get("metadata", {}).get("displayName") or fallback,
                data.get("config", {}).get("logicalId"))
    except (json.JSONDecodeError, OSError):
        return fallback, None


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _schedules(item_dir: Path, now: datetime) -> dict:
    """Count enabled schedules in the item's `.schedules` file (Fabric git integration)."""
    f = item_dir / ".schedules"
    if not f.exists():
        return {"enabled": 0, "total": 0}
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"enabled": 0, "total": 0}
    entries = data.get("schedules", []) if isinstance(data, dict) else []
    enabled = 0
    for s in entries:
        if not s.get("enabled"):
            continue
        end = ((s.get("configuration") or {}).get("endDateTime") or "")[:19]
        try:
            if end and datetime.fromisoformat(end).replace(tzinfo=UTC) < now:
                continue          # schedule window already over
        except ValueError:
            pass
        enabled += 1
    return {"enabled": enabled, "total": len(entries)}


def _ensure_table(g: Graph, name: str) -> str:
    tid = table_id(name)
    meta = {} if "." in name else {"unqualified": True}   # may be a temp view or a dbo table
    g.add_node(Node(tid, "table", name.lower(), meta=meta))
    return tid


def _check_lakehouse(g: Graph, schema: dict[str, list[str]] | None, default_schema: str = "dbo") -> None:
    """Compare the extracted tables with the schema export, when there is one.

    Every table node gets ``meta.inLakehouse`` (true or false); a name without a schema is looked up in
    the default schema. A bare name that nothing writes and the lakehouse does not have is a CTE, a temp
    view or a DuckDB relation: it leaves the graph and is listed per notebook in annotations instead.
    Without a schema export nothing is marked, and only bare names that nothing writes leave the graph.
    """
    known = set(schema) if schema is not None else None
    written = {dst for (_src, dst, kind, conf) in g.edges if kind == "writes" and conf != "unresolved"}
    aside: dict[str, list[str]] = {}
    checked = missing = 0
    for nid, node in list(g.nodes.items()):
        if node.type != "table" or node.meta.get("external"):
            continue
        bare = "." not in node.name
        present = None if known is None else (f"{default_schema}.{node.name}" if bare else node.name) in known
        if bare and nid not in written and not present:
            for src in sorted({s for (s, dst, _k, _c) in g.edges if dst == nid}):
                aside.setdefault(src, []).append(node.name)
            del g.nodes[nid]
            continue
        if present is not None:
            node.meta["inLakehouse"] = present
            checked += 1
            missing += not present
    if known is not None:
        g.annotations["lakehouse"] = {"checked": checked, "missing": missing, "defaultSchema": default_schema}
    if not aside:
        return
    g.edges = {k: e for k, e in g.edges.items() if k[0] in g.nodes and k[1] in g.nodes}
    names = sorted({n for ns in aside.values() for n in ns})
    g.annotations["unqualifiedSetAside"] = {
        "names": len(names), "byItem": {item: sorted(set(ns)) for item, ns in sorted(aside.items())}}


def _ensure_unresolved(g: Graph, template: str) -> str:
    uid = unresolved_id(template)
    g.add_node(Node(uid, "unknown", template, meta={"reason": "dynamic or unresolvable reference"}))
    return uid


def extract(repo: Path, resolvers: tuple[str, ...] = ("load_cfg",),
            dates: dict[str, dict] | None = None, now: datetime | None = None,
            stale_days: int = 365, schema: dict[str, list[str]] | None = None,
            schema_ignore: list[str] | None = None, column_lineage: bool = False,
            config_tables: list[str] | None = None, inputs: dict | None = None) -> Graph:
    """Build the graph.

    ``dates`` is the `items` map from gitdates.load_dates (optional). ``schema`` maps
    ``schema.table`` to column names (schema_export.load_schema); passing it, or setting
    ``column_lineage``, adds column-level lineage from the SQL the notebooks execute.
    ``config_tables`` are files written by ``export-table``: rows that map a target table to a
    SELECT (or to a notebook) add row-level flows and column lineage the notebooks cannot show.
    """
    repo = repo.resolve()
    g = Graph()
    g.inputs = inputs or {}
    if now is not None:
        g.generated_at = now      # a fixed clock makes the output reproducible (fixtures, CI)
    now = now or datetime.now(UTC)
    logical: dict[str, tuple[str, str]] = {}
    notebooks: dict[str, tuple[Path, str]] = {}
    pipelines: dict[str, tuple[Path, str]] = {}

    for d in _item_dirs(repo, "Notebook"):
        name, lid = _platform(d)
        notebooks[name] = (d, lid or "")
        if lid:
            logical[lid] = ("Notebook", name)
        g.add_node(Node(f"notebook:{name}", "notebook", name,
                        path=d.relative_to(repo).as_posix(), item_id=lid,
                        meta={"schedules": _schedules(d, now)}))
    for d in _item_dirs(repo, "DataPipeline"):
        name, lid = _platform(d)
        pipelines[name] = (d, lid or "")
        if lid:
            logical[lid] = ("DataPipeline", name)
        logical.setdefault(name, ("DataPipeline", name))
        g.add_node(Node(f"pipeline:{name}", "pipeline", name,
                        path=d.relative_to(repo).as_posix(), item_id=lid,
                        meta={"schedules": _schedules(d, now)}))

    nb_by_lower = {n.lower(): n for n in notebooks}
    literals: dict[str, list[tuple[str, int, str]]] = {}   # notebook -> [(name, line, file)]
    statements: dict[str, list] = {}                       # notebook -> executed SQL, in order
    temp_views: dict[str, set[str]] = {}                   # notebook -> temp view names

    # --- notebooks -------------------------------------------------------
    for name, (d, _lid) in sorted(notebooks.items()):
        content = next((p for p in (d / "notebook-content.py", d / "notebook-content.sql") if p.exists()), None)
        node = g.nodes[f"notebook:{name}"]
        if content is None:
            node.meta["parsed"] = False
            g.warn(name, "no notebook-content.py/.sql found (ipynb not supported yet)")
            continue
        rel = content.relative_to(repo).as_posix()
        try:
            info = parse_notebook(str(content), rel, _read(content))
        except Exception as exc:  # keep going; one bad notebook must not stop extraction
            node.meta["parsed"] = False
            g.warn(name, f"parser crashed: {type(exc).__name__}: {exc}")
            continue
        node.meta.update({"parsed": not info.skipped, "cells": info.cells, "sqlCells": info.sql_cells,
                          "parseErrors": info.parse_errors, "fallbackSql": info.fallback_sql,
                          "unresolvedSql": info.unresolved_sql, "refs": len(info.hits)})
        if info.unresolved_sql:
            where = "; ".join(info.unresolved_where[:3]) + ("; ..." if len(info.unresolved_where) > 3 else "")
            g.warn(name, f"{info.unresolved_sql} spark.sql() call(s) with SQL built at run time ({where})")
        if info.parse_errors:
            g.warn(name, f"{info.parse_errors} cell(s) failed Python parsing; regex fallback used")
        nid = f"notebook:{name}"
        literals[name] = [(lit, line, rel) for lit, line in info.literals]
        statements[name] = info.statements
        temp_views[name] = info.temp_views
        for h in info.hits:
            target = _ensure_unresolved(g, h.name) if h.confidence == "unresolved" else _ensure_table(g, h.name)
            g.add_edge(nid, target, "reads" if h.mode == "read" else "writes", h.confidence, h.evidence)
        for run_name, conf, evd in info.runs:
            real = nb_by_lower.get(run_name.lower())
            if real and conf == "parsed":
                g.add_edge(nid, f"notebook:{real}", "calls", "parsed", evd)
            else:
                g.add_edge(nid, _ensure_unresolved(g, f"notebook:{run_name}"), "calls", "unresolved", evd)

    # --- table-name string literals --------------------------------------
    # A literal like "silver_x.orders" only counts if its schema is one we have
    # already seen in parsed SQL/API references (keeps "df.csv" and friends out).
    schema_counts: dict[str, int] = {}
    for (_s, dst, kind, conf) in g.edges:
        if dst.startswith("table:") and "." in dst and kind in ("reads", "writes") and conf == "parsed":
            prefix = dst[len("table:"):].split(".", 1)[0]
            schema_counts[prefix] = schema_counts.get(prefix, 0) + 1
    known_schemas = {s for s, c in schema_counts.items() if c >= 3}
    for name, lits in literals.items():
        nid = f"notebook:{name}"
        for lit, line, rel in lits:
            if lit.split(".", 1)[0] not in known_schemas:
                continue
            tid = table_id(lit)
            if any((nid, tid, k, c) in g.edges for k in ("reads", "writes") for c in ("parsed", "inferred")):
                continue
            g.add_node(Node(tid, "table", lit))
            g.add_edge(nid, tid, "references", "inferred",
                       {"file": rel, "line": line, "via": "string literal", "snippet": lit})

    # --- pipelines -------------------------------------------------------
    for name, (d, _lid) in sorted(pipelines.items()):
        f = d / "pipeline-content.json"
        node = g.nodes[f"pipeline:{name}"]
        if not f.exists():
            node.meta["parsed"] = False
            continue
        try:
            info = parse_pipeline(f.relative_to(repo).as_posix(), _read(f), logical)
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            node.meta["parsed"] = False
            g.warn(name, f"pipeline JSON not parsed: {type(exc).__name__}: {exc}")
            continue
        node.meta.update({"parsed": True, "activities": info.activities})
        for w in info.warnings:
            g.warn(name, w)
        pid = f"pipeline:{name}"
        for h in info.hits:
            if h.kind == "calls":
                if h.confidence == "unresolved":
                    tgt = _ensure_unresolved(g, h.target)
                else:
                    tgt = f"{h.target_type}:{h.target}"
                g.add_edge(pid, tgt, "calls", h.confidence, h.evidence)
            else:
                tgt = _ensure_unresolved(g, h.target) if h.confidence == "unresolved" else _ensure_table(g, h.target)
                g.add_edge(pid, tgt, h.kind, h.confidence, h.evidence)

    # --- semantic models --------------------------------------------------
    for d in _item_dirs(repo, "SemanticModel"):
        model_name, lid = _platform(d)
        for tmdl in sorted((d / "definition" / "tables").glob("*.tmdl")):
            st = parse_tmdl_table(_read(tmdl))
            if not st:
                continue
            sid = f"semantic_table:{model_name}.{st.name}"
            g.add_node(Node(sid, "semantic_table", f"{model_name}.{st.name}",
                            path=tmdl.relative_to(repo).as_posix(), item_id=lid,
                            meta={"model": model_name, "mode": st.mode, "measures": st.measures}))
            if st.source:
                g.add_edge(sid, _ensure_table(g, st.source), "binds", "parsed",
                           {"file": tmdl.relative_to(repo).as_posix(), "line": st.line, "via": "TMDL partition"})
            else:
                g.warn(model_name, f"table '{st.name}' has no detectable source")

    # --- config resolvers --------------------------------------------------
    for r in resolvers:
        cls = RESOLVERS.get(r)
        if cls is None:
            g.warn("resolvers", f"unknown resolver '{r}'")
            continue
        added = cls().apply(g, repo)
        g.warn(f"resolver:{r}", f"added {added} inferred edge(s)") if added else None

    for path in config_tables or []:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
        # exports through the SQL endpoint cut long text; a OneLake (Delta) export has whole values
        limit = None if doc.get("source") == "onelake-delta" else TEXT_LIMIT
        resolver = QueryConfigResolver(doc.get("rows", []), doc.get("table", "config"), text_limit=limit)
        added = resolver.apply(g, repo)
        g.config_tables.append(resolver)
        g.warn(f"config table {resolver.table}", f"added {added} flow/edge link(s) from {len(resolver.rows)} row(s)")

    _check_lakehouse(g, schema)
    liveness.annotate(g, dates, now, stale_days)
    if schema is not None or column_lineage:
        _column_lineage(g, statements, temp_views, ColumnSchema(schema, schema_ignore))
    return g


def _column_lineage(g: Graph, statements: dict[str, list], temp_views: dict[str, set[str]], cs: ColumnSchema) -> None:
    from .columns import ColStats, analyze_notebook  # imported late: sqlglot optimizer is heavy
    total = ColStats()
    notebooks_with_edges = 0
    for name, stmts in sorted(statements.items()):
        edges, stats = analyze_notebook(stmts, cs, temp_views.get(name, set()))
        total.add(stats)
        if edges:
            notebooks_with_edges += 1
        for e in edges:
            src, dst = _ensure_table(g, e.source), _ensure_table(g, e.target)
            g.add_column_edge(f"notebook:{name}", src, e.source_col, dst, e.target_col, e.transform, e.expr,
                              e.conf, e.line)
    for resolver in g.config_tables:
        resolver.columns(g, cs, _ensure_table)
    for node in g.nodes.values():
        if node.type == "table" and (cols := cs.cols(node.name)) is not None:
            g.column_tables[node.id] = cols
    edges = list(g.column_edges.values())
    camel = lambda k: k.split("_")[0] + "".join(w.capitalize() for w in k.split("_")[1:])   # noqa: E731
    g.column_stats = {
        **{camel(k): v for k, v in total.__dict__.items()},
        "edges": len(edges),
        "edgesParsed": sum(1 for e in edges if e["confidence"] == "parsed"),
        "edgesInferred": sum(1 for e in edges if e["confidence"] == "inferred"),
        "notebooksWithColumnEdges": notebooks_with_edges,
        "tablesWithKnownColumns": len(g.column_tables),
        "schemaTables": len(cs.tables),
    }






