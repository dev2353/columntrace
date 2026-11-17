"""Config resolvers expand metadata-driven notebooks whose table names live in config.

A generic loader notebook reads ``cfg.load_cfg`` and writes to a table built at
run time, so the notebook alone shows only an *unresolved* write. This resolver
reads the config export and adds one *inferred* edge per enabled config row.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Protocol

from .model import Graph, Node, table_id


class ConfigResolver(Protocol):
    name: str

    def apply(self, graph: Graph, repo_root: Path) -> int: ...


class LoadCfgResolver:
    """Expands notebooks that read ``cfg.load_cfg`` and have an unresolved write."""

    name = "load_cfg"
    CFG_TABLE = table_id("cfg.load_cfg")
    CONFIG_COLUMNS = ("dest_table", "dest_schema", "dest_table_schema", "dest_table_full_name")

    def __init__(self, cfg_path: Path | None = None) -> None:
        self.cfg_path = cfg_path

    def _find_cfg(self, repo_root: Path) -> Path | None:
        if self.cfg_path:
            return self.cfg_path
        return next(iter(sorted(repo_root.rglob("load_cfg.json"))), None)

    def apply(self, graph: Graph, repo_root: Path) -> int:
        path = self._find_cfg(repo_root)
        if not path or self.CFG_TABLE not in graph.nodes:
            return 0
        rows = json.loads(path.read_text(encoding="utf-8"))

        readers = {src for (src, dst, kind, _c) in graph.edges
                   if kind == "reads" and dst == self.CFG_TABLE and src.startswith("notebook:")}

        # nb -> unresolved write templates that mention a config column; those are
        # the notebooks whose target table comes from the config row.
        templates: dict[str, list[str]] = {}
        for (src, dst, kind, conf) in graph.edges:
            if src in readers and kind == "writes" and conf == "unresolved":
                name = graph.nodes[dst].name
                if any(col in name for col in self.CONFIG_COLUMNS):
                    templates.setdefault(src, []).append(name)

        added = 0
        for nb in sorted(templates):
            # a literal schema prefix (e.g. "bronze_jira.{dest_table}") narrows the rows
            literal_schemas = {t.split(".", 1)[0] for t in templates[nb]
                               if "." in t and "{" not in t.split(".", 1)[0]}
            for row in rows:
                enabled = row.get("enabled")
                if enabled in (False, 0, "0", "false", "False"):
                    continue
                schema, table = row.get("dest_table_schema"), row.get("dest_table")
                if not schema or not table:
                    continue
                if literal_schemas and schema.lower() not in {s.lower() for s in literal_schemas}:
                    continue
                dest = table_id(f"{schema}.{table}")
                graph.add_node(Node(dest, "table", f"{schema}.{table}".lower()))
                ev = {"config": "cfg.load_cfg", "id": row.get("id"), "via": "config resolver"}
                graph.add_edge(nb, dest, "writes", "inferred", ev)
                added += 1
                if row.get("src_name") and row.get("src_table"):
                    src_id = f"source:{row['src_name']}.{row['src_table']}".lower()
                    graph.add_node(Node(src_id, "table", f"{row['src_name']}:{row['src_table']}",
                                        meta={"external": True, "system": row["src_name"]}))
                    graph.add_edge(nb, src_id, "reads", "inferred", ev)
                    added += 1
        return added


RESOLVERS: dict[str, type] = {"load_cfg": LoadCfgResolver}


class QueryConfigResolver:
    """Config rows that map a target table to a SELECT query, or to a notebook that fills it.

    The loader notebook only shows ``spark.sql(row['query'])``; the table holds the real
    mapping. For each enabled row (columns: table_name, query, enabled, is_notebook,
    except_columns):

    * SQL row: the query's source tables flow into ``table_name``. Each pair is a row-level
      *flow* (precise, drawn by default). The loader notebooks also get config-tagged
      reads/writes edges so "Used by" lists them (hidden by default like other config edges).
    * Notebook row (``is_notebook``): ``query`` names the notebook that fills ``table_name``;
      it gets a write edge to it and the loaders get a call edge to it (both inferred).

    ``columns()`` adds column lineage for the SQL rows (names matched by the loader's MERGE),
    minus ``except_columns``.
    """

    name = "query_config"

    def __init__(self, rows: list[dict], table: str, text_limit: int | None = None) -> None:
        self.rows, self.table = rows, table.lower()
        self.loaders: list[str] = []
        # a query exactly this long was cut by the SQL endpoint (None: the export has whole values)
        self.text_limit = text_limit
        self.problems = 0

    @staticmethod
    def _on(v) -> bool:
        return v not in (None, False, 0, "0", "false", "False", "")

    def _dest(self, row: dict) -> str | None:
        t = str(row.get("table_name") or "").strip().strip("`[]").lower()
        return t or None

    def apply(self, graph: Graph, repo_root: Path | None = None) -> int:
        cfg = table_id(self.table)
        # a loader reads the config table AND writes a table it cannot name: a notebook that merely
        # reads the config (checks, watermark updates, dashboards) is not what ran the rows
        readers = {src for (src, dst, kind, _c) in graph.edges
                   if kind == "reads" and dst == cfg and src.startswith("notebook:")}
        dynamic_writers = {src for (src, _dst, kind, conf) in graph.edges if kind == "writes" and conf == "unresolved"}
        candidates = sorted(readers & dynamic_writers)
        # ... and its code must use the config's own columns (row['table_name'], row['query']): a
        # notebook that reads the table for something else and writes some dynamic table is not it
        self.loaders = [c for c in candidates if self._uses_columns(graph, c, repo_root)]
        return self._apply_rows(graph)

    TABLE_COL, QUERY_COL = "table_name", "query"

    def _uses_columns(self, graph: Graph, notebook_id: str, repo_root: Path | None) -> bool:
        node = graph.nodes.get(notebook_id)
        if repo_root is None or node is None or not node.path:
            return True                                   # cannot read the code: keep the structural match
        folder = repo_root / node.path
        files = [p for p in (folder / "notebook-content.py", folder / "notebook-content.sql") if p.exists()]
        if not files:
            return True
        text = files[0].read_text(encoding="utf-8", errors="replace")
        quoted = lambda col: re.search(rf"""['"]{col}['"]""", text) is not None   # noqa: E731
        return quoted(self.TABLE_COL) and quoted(self.QUERY_COL)

    def _apply_rows(self, graph: Graph) -> int:
        from .sql_refs import extract_refs

        jobs = self.loaders or [f"config:{self.table}"]
        by_lower = {n.name.lower(): n.id for n in graph.nodes.values() if n.type == "notebook"}
        added = 0
        for row in self.rows:
            dest = self._dest(row)
            query = str(row.get("query") or "").strip()
            if not self._on(row.get("enabled")) or not dest or not query:
                continue
            dest_id = table_id(dest)
            graph.add_node(Node(dest_id, "table", dest, meta={} if "." in dest else {"unqualified": True}))
            ev = {"config": self.table, "id": row.get("id"), "via": "config table"}
            ev_cfg = {**ev, "via": "config resolver"}      # hidden by default: loader x all rows
            if self._on(row.get("is_notebook")):
                nb = by_lower.get(query.lower())
                if nb is None:
                    continue
                graph.add_edge(nb, dest_id, "writes", "inferred", ev)          # precise: one notebook, one table
                for loader in self.loaders:
                    graph.add_edge(loader, nb, "calls", "inferred", ev)
                added += 1 + len(self.loaders)
                continue
            refs, _ = extract_refs(query, set(), fallback=False)
            sources = sorted({r.name for r in refs if r.mode == "read" and r.name != dest})
            cut = bool(self.text_limit) and len(str(row.get("query") or "")) == self.text_limit
            if cut or not any("." in s for s in sources):
                # never silent: the row would otherwise just leave its table without sources
                msg = (f"query is exactly {self.text_limit} characters: the SQL endpoint cut it, so sources may be "
                       "missing. Re-export the config table with export-delta." if cut else "query names no table")
                graph.warn(f"{self.table} row {row.get('id')} ({dest})", msg)
                graph.nodes[dest_id].meta["configWarning"] = msg
                self.problems += 1
            for src in sources:
                if "." not in src:
                    continue                                                   # CTE or temp view, not a table
                src_id = table_id(src)
                graph.add_node(Node(src_id, "table", src))
                for job in jobs:
                    graph.add_flow(src_id, dest_id, job, "parsed", ev)
                    added += 1
                for loader in self.loaders:
                    graph.add_edge(loader, src_id, "reads", "inferred", ev_cfg)
            for loader in self.loaders:
                graph.add_edge(loader, dest_id, "writes", "inferred", ev_cfg)
        return added

    def columns(self, graph: Graph, cs, ensure_table) -> int:
        """Column lineage for the SQL rows, as if each query were `CREATE TABLE target AS query`."""
        from .columns import analyze_notebook
        from .notebooks import Stmt

        jobs = self.loaders or [f"config:{self.table}"]
        added = 0
        for row in self.rows:
            dest = self._dest(row)
            query = str(row.get("query") or "").strip()
            if not self._on(row.get("enabled")) or self._on(row.get("is_notebook")) or not dest or not query:
                continue
            skip = {c.strip().lower() for c in str(row.get("except_columns") or "").split(",") if c.strip()}
            edges, _ = analyze_notebook([Stmt(f"CREATE TABLE {dest} AS {query}", 0, f"config row {row.get('id')}")], cs)
            for e in edges:
                if e.target_col in skip:
                    continue
                src, dst = ensure_table(graph, e.source), ensure_table(graph, e.target)
                for job in jobs:
                    graph.add_column_edge(job, src, e.source_col, dst, e.target_col, e.transform, e.expr, e.conf, 0)
                    added += 1
        return added
