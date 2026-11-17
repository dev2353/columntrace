"""Table-level references (reads / writes) from SQL text, via sqlglot."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

from .strings import TOKEN_RE

# sqlglot warns for every OPTIMIZE/VACUUM/SHOW it handles as a generic Command
logging.getLogger("sqlglot").setLevel(logging.ERROR)


@dataclass(frozen=True)
class Ref:
    name: str           # lowercase schema.table (or bare name); may contain tokens
    mode: str           # "read" | "write"
    confidence: str     # "parsed" | "inferred"  (tokens are downgraded by the caller)


_FALLBACK_READ = re.compile(r"(?is)\b(?:FROM|JOIN)\s+([`\w.]+)")
_FALLBACK_WRITE = re.compile(
    r"(?is)\b(?:INSERT\s+(?:INTO|OVERWRITE)(?:\s+TABLE)?|MERGE\s+INTO|UPDATE|DELETE\s+FROM"
    r"|CREATE\s+(?:OR\s+REPLACE\s+)?TABLE(?:\s+IF\s+NOT\s+EXISTS)?)\s+([`\w.]+)")
_IDENT = re.compile(r"^[A-Za-z_][\w]*(\.[A-Za-z_][\w]*)?$")
# words the regex fallback can land on that are SQL, not table names (UPDATE SET, FROM VALUES ...)
_KEYWORDS = {"set", "select", "values", "where", "using", "on", "as", "table", "with", "when", "then", "lateral",
             "unnest", "join", "from", "into", "only"}


def _is_temp(create: exp.Create) -> bool:
    props = create.args.get("properties")
    if props and any(isinstance(p, exp.TemporaryProperty) for p in props.expressions):
        return True
    return False


def _table_parts(t: exp.Table) -> list[str]:
    """All name parts, including 4-part `workspace`.lakehouse.schema.table names."""
    names = [p.name or p.sql() for p in t.parts]
    return [n for n in names if n]


def _table_name(t: exp.Table) -> str | None:
    names = _table_parts(t)
    if not names:
        return None
    return ".".join(names[-2:]).lower()   # schema.table; catalog/workspace/lakehouse dropped


def _statement_refs(stmt: exp.Expression, temp_views: set[str]) -> list[Ref]:
    targets: list[exp.Table] = []
    skip: set[int] = set()

    if isinstance(stmt, exp.Insert):
        tgt = stmt.this.this if isinstance(stmt.this, exp.Schema) else stmt.this
        if isinstance(tgt, exp.Table):
            targets = [tgt]
    elif isinstance(stmt, exp.Merge):
        if isinstance(stmt.this, exp.Table):
            targets = [stmt.this]
    elif isinstance(stmt, exp.Create):
        this = stmt.this.this if isinstance(stmt.this, exp.Schema) else stmt.this
        if str(stmt.args.get("kind") or "").upper() in ("SCHEMA", "DATABASE"):
            return []                                     # CREATE SCHEMA cfg names a schema, not a table
        if isinstance(this, exp.Table):
            if _is_temp(stmt):
                nm = _table_name(this)
                if nm:
                    temp_views.add(nm)
                skip.add(id(this))
            else:
                targets = [this]
    elif isinstance(stmt, (exp.Update, exp.Delete)):
        if isinstance(stmt.this, exp.Table):
            targets = [stmt.this]
    elif isinstance(stmt, exp.TruncateTable):
        targets = [t for t in stmt.expressions if isinstance(t, exp.Table)]
    elif isinstance(stmt, (exp.Drop, exp.Alter, exp.Command, exp.Set, exp.Use,
                           exp.Describe, exp.Show)):
        return []

    ctes = {c.alias.lower() for c in stmt.find_all(exp.CTE) if c.alias}
    target_ids = {id(t) for t in targets}
    refs: list[Ref] = []
    for t in stmt.find_all(exp.Table):
        if id(t) in skip:
            continue
        name = _table_name(t)
        if not name:
            continue
        qualified = len(_table_parts(t)) >= 2
        if not qualified and (name in ctes or name in temp_views):
            continue
        mode = "write" if id(t) in target_ids else "read"
        refs.append(Ref(name, mode, "parsed" if qualified or TOKEN_RE.search(name) else "inferred"))
    return refs


_MLV = re.compile(
    r"(?is)\bCREATE\s+(?:OR\s+REPLACE\s+)?MATERIALIZED\s+LAKE\s+VIEW\s+(?:IF\s+NOT\s+EXISTS\s+)?([`\w.]+)")
_AS_BODY = re.compile(r"(?is)\bAS\s*(?=\(?\s*(?:SELECT|WITH)\b)")


def _rewrite_mlv(sql: str) -> str:
    """Fabric materialized lake views are not in sqlglot; treat them as plain views.

    ``CREATE MATERIALIZED LAKE VIEW x (constraints...) COMMENT '...' AS SELECT ...``
    becomes ``CREATE OR REPLACE VIEW x AS SELECT ...``: same lineage, parseable.
    """
    pieces: list[str] = []
    pos = 0
    for m in _MLV.finditer(sql):
        if m.start() < pos:
            continue
        body = _AS_BODY.search(sql, m.end())
        if not body:
            continue
        pieces.append(sql[pos:m.start()])
        pieces.append(f"CREATE OR REPLACE VIEW {m.group(1)} AS ")
        pos = body.end()
    pieces.append(sql[pos:])
    return "".join(pieces)


def _empty_schema(ref: Ref, sql: str) -> Ref:
    """sqlglot reads `FROM .tmp_x` as table tmp_x and drops the empty schema; the SQL text still shows
    it. Keep the dot so the name is treated as an unset parameter rather than a table."""
    if "." in ref.name or not re.search(rf"(?<![\w`\]\)])\.\s*`?{re.escape(ref.name)}\b", sql, re.I):
        return ref
    return Ref(f".{ref.name}", ref.mode, ref.confidence)


def extract_refs(sql: str, temp_views: set[str], fallback: bool = True) -> tuple[list[Ref], bool]:
    """Return (refs, used_fallback). ``temp_views`` is updated in place.

    With ``fallback=False`` an unparseable statement yields no refs instead of regex guesses.
    """
    sql = _rewrite_mlv(sql)
    try:
        statements = sqlglot.parse(sql, read="spark")
    except SqlglotError:
        statements = None

    if statements is not None:
        refs: list[Ref] = []
        for stmt in statements:
            if stmt is not None:
                refs.extend(_statement_refs(stmt, temp_views))
        return [_empty_schema(r, sql) for r in refs], False
    if not fallback:
        return [], False

    refs = []
    for m in _FALLBACK_WRITE.finditer(sql):
        name = m.group(1).strip("`").lower()
        if (_IDENT.match(name) or TOKEN_RE.search(name)) and name not in _KEYWORDS:
            refs.append(Ref(name, "write", "inferred"))
    for m in _FALLBACK_READ.finditer(sql):
        name = m.group(1).strip("`").lower()
        if (_IDENT.match(name) or TOKEN_RE.search(name)) and name not in temp_views and name not in _KEYWORDS:
            refs.append(Ref(name, "read", "inferred"))
    return refs, True
