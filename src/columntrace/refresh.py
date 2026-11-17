"""Refresh every input and the lineage file in one run, then report what changed.

    columntrace refresh --config refresh.toml [--skip pull,dates,schema,config] [--offline]

The settings file (TOML, kept private: it names your repo, endpoint and workspace) lists the steps.
Paths in it are relative to the file. Every step is optional, and a skipped step reuses the file the
last run wrote:

    pull      update the git clone to the branch head (git fetch --depth 1; local edits are kept)
    dates     last-change dates from Azure DevOps               (fetch-dates)
    schema    every table and column from the SQL endpoint      (export-schema)
    config    each config table, from OneLake or the endpoint   (export-delta / export-table)
    extract   lineage.json from all of the above; the previous file is kept next to it

All network steps are read-only. The change report compares the new lineage file with the previous
one: tables and jobs added or removed, link counts, column coverage and new warnings.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

STEPS = ("pull", "dates", "schema", "config", "extract")
NETWORK = {"pull", "dates", "schema", "config"}


class RefreshError(Exception):
    pass


@dataclass
class Settings:
    base: Path
    repo: Path
    branch: str = "main"
    pull: bool = True
    dates: dict | None = None
    schema: dict | None = None
    config_tables: list[dict] = field(default_factory=list)
    out: Path = Path("lineage.json")
    stale_days: int = 365
    no_evidence: bool = False

    def path(self, p: str | None) -> Path | None:
        return (self.base / p).resolve() if p else None


def load_settings(path: str | Path) -> Settings:
    f = Path(path).resolve()
    try:
        doc = tomllib.loads(f.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise RefreshError(f"cannot read settings {f}: {exc}") from exc
    base = f.parent
    repo = doc.get("repo", {})
    if not repo.get("path"):
        raise RefreshError("settings need [repo] path")
    ext = doc.get("extract", {})
    s = Settings(base=base, repo=(base / repo["path"]).resolve(), branch=repo.get("branch", "main"),
                 pull=bool(repo.get("pull", True)), dates=doc.get("dates"), schema=doc.get("schema"),
                 config_tables=list(doc.get("config_tables", [])), out=(base / ext.get("out", "lineage.json")).resolve(),
                 stale_days=int(ext.get("stale_days", 365)), no_evidence=bool(ext.get("no_evidence", False)))
    for section, keys in (("dates", ("org", "project", "repo", "out")), ("schema", ("server", "database", "out"))):
        d = getattr(s, section)
        if d is not None and (missing := [k for k in keys if not d.get(k)]):
            raise RefreshError(f"[{section}] is missing {', '.join(missing)}")
    for i, c in enumerate(s.config_tables):
        need = ["table", "out"] + (["workspace", "lakehouse"] if c.get("via", "onelake") == "onelake" else [])
        if missing := [k for k in need if not c.get(k)]:
            raise RefreshError(f"[[config_tables]] #{i + 1} is missing {', '.join(missing)}")
    return s


# ---------------------------------------------------------------------------------------
# steps (each returns one line for the log)

def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if out.returncode != 0:
        raise RefreshError(f"git {' '.join(args)} failed: {out.stderr.strip() or out.stdout.strip()}")
    return out.stdout.strip()


def step_pull(s: Settings) -> str:
    if not (s.repo / ".git").exists():
        return f"skipped: {s.repo} is not a git clone"
    before = _git(s.repo, "rev-parse", "HEAD")
    _git(s.repo, "fetch", "--depth", "1", "origin", s.branch)
    # --keep moves the branch to the fetched commit and refuses to overwrite local edits
    _git(s.repo, "reset", "--keep", "FETCH_HEAD")
    after = _git(s.repo, "rev-parse", "HEAD")
    if before == after:
        return f"already at {after[:8]}"
    try:
        changed = _git(s.repo, "diff", "--name-only", before, after).splitlines()
    except RefreshError:                          # the old commit may be gone in a shallow clone
        changed = []
    kinds = (".Notebook", ".DataPipeline", ".SemanticModel")
    items = sorted({seg for p in changed for seg in p.split("/") if seg.endswith(kinds)})
    return f"{before[:8]} -> {after[:8]}, {len(changed)} file(s) changed in {len(items)} item(s)"


def step_dates(s: Settings) -> str:
    from .gitdates import AdoClient, fetch_dates, save_dates
    d = s.dates or {}
    doc = fetch_dates(AdoClient(d["org"], d["project"], d["repo"], d.get("branch", s.branch)), d.get("folders") or None)
    save_dates(doc, str(s.path(d["out"])))
    return f"{len(doc['items'])} item folders, {doc['apiCalls']} API calls"


def step_schema(s: Settings) -> str:
    from . import schema_export as se
    d = s.schema or {}
    conn = se.connect(d["server"], d["database"])
    try:
        rows = se.fetch_rows(conn)
    finally:
        conn.close()
    doc = se.to_document(rows)
    se.save_json(doc, str(s.path(d["out"])))
    if d.get("csv"):
        se.save_csv(rows, str(s.path(d["csv"])))
    return f"{doc['counts']['tables']} tables, {doc['counts']['columns']} columns"


def step_config(s: Settings) -> str:
    from . import table_export as te
    parts = []
    for c in s.config_tables:
        out = str(s.path(c["out"]))
        max_rows = int(c.get("max_rows", te.DEFAULT_MAX_ROWS))
        if c.get("via", "onelake") == "onelake":
            from . import delta_export as de
            cols, rows, truncated = de.fetch_table(c["workspace"], c["lakehouse"], c["table"], max_rows)
            te.save_json(de.to_delta_document(c["table"], cols, rows, truncated), out)
            parts.append(f"{c['table']}: {len(rows)} rows (OneLake)")
        else:
            from . import schema_export as se
            server = c.get("server") or (s.schema or {}).get("server")
            database = c.get("database") or (s.schema or {}).get("database")
            if not server or not database:
                raise RefreshError(f"{c['table']}: via = 'sql' needs server and database (here or in [schema])")
            conn = se.connect(server, database)
            try:
                cols, rows, truncated = te.fetch_table(conn, c["table"], max_rows)
            finally:
                conn.close()
            doc = te.to_document(c["table"], cols, rows, truncated)
            te.save_json(doc, out)
            cut = f", {len(doc['cutCells'])} value(s) cut at {te.TEXT_LIMIT} characters" if doc["cutCells"] else ""
            parts.append(f"{c['table']}: {len(rows)} rows (SQL endpoint{cut})")
    return "; ".join(parts) or "no config tables listed"


def step_extract(s: Settings) -> str:
    from .extract import describe_inputs, extract
    from .gitdates import load_dates
    from .schema_export import load_schema

    def existing(p: Path | None, what: str) -> Path | None:
        if p is None:
            return None
        if not p.exists():
            raise RefreshError(f"{what} file {p} does not exist yet: run that step once")
        return p

    dates = existing(s.path((s.dates or {}).get("out")), "dates")
    schema = existing(s.path((s.schema or {}).get("out")), "schema")
    configs = [str(existing(s.path(c["out"]), c["table"])) for c in s.config_tables]
    g = extract(s.repo, dates=load_dates(str(dates)) if dates else None, stale_days=s.stale_days,
                schema=load_schema(str(schema)) if schema else None,
                schema_ignore=list((s.schema or {}).get("ignore", [])), config_tables=configs,
                inputs=describe_inputs(s.repo, str(dates) if dates else None, str(schema) if schema else None, configs))
    s.out.parent.mkdir(parents=True, exist_ok=True)
    if s.out.exists():
        shutil.copyfile(s.out, previous_path(s.out))
    g.save(str(s.out), include_evidence=not s.no_evidence)
    st = g.stats()
    return f"{st['nodes']} nodes, {st['edges']} edges -> {s.out.name}"


RUNNERS: dict[str, Callable[[Settings], str]] = {
    "pull": step_pull, "dates": step_dates, "schema": step_schema, "config": step_config, "extract": step_extract,
}


def _sibling(out: Path, tag: str, ext: str) -> Path:
    """lineage.private.json -> lineage.<tag>.private<ext> (stays private), lineage.json -> lineage.<tag><ext>."""
    name = out.name.removesuffix(".json")
    private = name.endswith(".private")
    return out.with_name(f"{name.removesuffix('.private')}.{tag}{'.private' if private else ''}{ext}")


def previous_path(out: Path) -> Path:
    return _sibling(out, "previous", ".json")


def plan(s: Settings, skip: set[str], offline: bool) -> list[str]:
    """The steps this run takes, in order."""
    configured = {"pull": s.pull, "dates": s.dates is not None, "schema": s.schema is not None,
                  "config": bool(s.config_tables), "extract": True}
    return [k for k in STEPS if configured[k] and k not in skip and not (offline and k in NETWORK)]


# ---------------------------------------------------------------------------------------
# change report

def _names(doc: dict, types: tuple[str, ...]) -> set[str]:
    return {n["name"] for n in doc.get("nodes", []) if n["type"] in types}


def _pairs(doc: dict) -> set[tuple[str, str, str]]:
    return {(e["from"], e["to"], e["kind"]) for e in doc.get("edges", []) if e["confidence"] != "unresolved"}


def compare(old: dict, new: dict) -> dict:
    """What changed between two lineage files."""
    out: dict = {}
    for label, types in (("tables", ("table", "semantic_table")), ("notebooks", ("notebook",)), ("pipelines", ("pipeline",))):
        a, b = _names(old, types), _names(new, types)
        out[label] = {"before": len(a), "after": len(b), "added": sorted(b - a), "removed": sorted(a - b)}
    pa, pb = _pairs(old), _pairs(new)
    out["links"] = {"before": len(pa), "after": len(pb), "added": len(pb - pa), "removed": len(pa - pb)}
    oc, nc = (old.get("columns") or {}).get("stats", {}), (new.get("columns") or {}).get("stats", {})
    out["columnEdges"] = {"before": oc.get("edges", 0), "after": nc.get("edges", 0)}
    out["flows"] = {"before": len(old.get("flows", [])), "after": len(new.get("flows", []))}
    ow = {(w["item"], w["message"]) for w in old.get("warnings", [])}
    out["newWarnings"] = [f"{i}: {m}" for i, m in sorted({(w["item"], w["message"]) for w in new.get("warnings", [])} - ow)
                          if not i.startswith("config table ")]           # the per-run "added N links" lines
    return out


def report_markdown(c: dict, limit: int = 25) -> str:
    def delta(d: dict) -> str:
        n = d["after"] - d["before"]
        return f"{d['before']:,} -> {d['after']:,} ({'+' if n >= 0 else ''}{n:,})"

    lines = ["# Lineage changes since the last refresh", ""]
    for label in ("tables", "notebooks", "pipelines"):
        d = c[label]
        lines.append(f"- **{label.capitalize()}:** {delta(d)}")
        for word in ("added", "removed"):
            names = d[word]
            if names:
                more = f" and {len(names) - limit} more" if len(names) > limit else ""
                shown = ", ".join(f"`{n}`" for n in names[:limit]) + more
                lines.append(f"  - {word} ({len(names)}): {shown}")
    lk = c["links"]
    lines.append(f"- **Links (reads, writes, calls):** {delta(lk)}; {lk['added']:,} new, {lk['removed']:,} gone")
    lines.append(f"- **Config-table flows:** {delta(c['flows'])}")
    lines.append(f"- **Column links:** {delta(c['columnEdges'])}")
    if c["newWarnings"]:
        lines += ["", f"## New warnings ({len(c['newWarnings'])})", ""]
        lines += [f"- {w}" for w in c["newWarnings"][:limit]]
        if len(c["newWarnings"]) > limit:
            lines.append(f"- ... and {len(c['newWarnings']) - limit} more (see the lineage file)")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------------------

def run(s: Settings, steps: list[str], log: Callable[[str], None] = print,
        runners: dict[str, Callable[[Settings], str]] | None = None) -> dict | None:
    """Run the steps in order (stops at the first failure); returns the change report, if any."""
    from .schema_export import SchemaExportError
    runners = runners or RUNNERS
    for name in steps:
        try:
            log(f"[{name}] {runners[name](s)}")
        except (RefreshError, SchemaExportError, OSError) as exc:
            raise RefreshError(f"[{name}] failed: {exc}") from exc
        except Exception as exc:   # e.g. the Azure DevOps client's own errors
            raise RefreshError(f"[{name}] failed: {type(exc).__name__}: {exc}") from exc
    prev = previous_path(s.out)
    if "extract" not in steps or not prev.exists():
        return None
    c = compare(json.loads(prev.read_text(encoding="utf-8")), json.loads(s.out.read_text(encoding="utf-8")))
    md = report_markdown(c)
    _sibling(s.out, "changes", ".md").write_text(md, encoding="utf-8")
    log(md)
    return c


def main(config: str, skip: str = "", offline: bool = False) -> int:
    try:
        s = load_settings(config)
        unknown = {x for x in skip.split(",") if x} - set(STEPS)
        if unknown:
            raise RefreshError(f"unknown step(s) for --skip: {', '.join(sorted(unknown))} (steps: {', '.join(STEPS)})")
        steps = plan(s, {x for x in skip.split(",") if x}, offline)
        print(f"steps: {', '.join(steps)}")
        run(s, steps)
    except RefreshError as exc:
        print(f"refresh stopped: {exc}", file=sys.stderr)
        return 2
    return 0
