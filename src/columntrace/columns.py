"""Column-level lineage from the SQL a notebook executes.

For every statement that writes a table (INSERT, CREATE TABLE/VIEW AS SELECT, MERGE,
materialized lake views, DataFrames saved from ``spark.sql``) each output column is traced
back through CTEs, subqueries, unions and temp views to the physical source columns it is
computed from. Join keys and filters decide *which rows* flow, not which columns, so they
are not edges.

Confidence: ``parsed`` when every step was explicit in the SQL; ``inferred`` when a step had
to be assumed (a ``*`` passed through by name, a column with no table prefix attributed to the
only table in scope, an INSERT without a column list mapped by position).

Schemas (from ``export-schema``) let ``SELECT *`` expand and unqualified columns resolve.
Without a table's schema a star stays an unresolved ``*`` edge, never silently dropped.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import Scope, build_scope
from sqlglot.schema import MappingSchema

from .notebooks import Stmt
from .sql_refs import _is_temp, _rewrite_mlv, _table_parts
from .strings import TOKEN_RE

MAX_DEPTH = 30
# sqlglot renamed the "with" arg to "with_" in recent versions; use whichever this install has
WITH_KEY = "with_" if "with_" in exp.Select.arg_types else "with"
EXPR_LIMIT = 160


@dataclass(frozen=True)
class ColEdge:
    source: str            # schema.table (or a bare name)
    source_col: str        # '*' when a star could not be expanded
    target: str
    target_col: str
    transform: str         # direct | expression | aggregate | star
    expr: str
    conf: str              # parsed | inferred
    line: int
    via: str


@dataclass
class ColStats:
    statements: int = 0
    with_edges: int = 0
    not_a_write: int = 0
    failed_parse: int = 0
    failed_qualify: int = 0
    dynamic_target: int = 0
    ambiguous_columns: int = 0
    unresolved_stars: int = 0
    temp_view_sources: int = 0   # sources that are temp views built outside SQL (DataFrame code): not traced
    unqualified_sources: int = 0  # other bare names (CTEs or views we never saw defined): not tables

    def add(self, other: ColStats) -> None:
        for k, v in other.__dict__.items():
            setattr(self, k, getattr(self, k) + v)


class ColumnSchema:
    """Column names per ``schema.table`` (lowercase), optionally minus ignored tables."""

    def __init__(self, tables: dict[str, list[str]] | None = None, ignore: list[str] | None = None) -> None:
        pats = [p.lower() for p in (ignore or [])]

        def skip(name: str) -> bool:
            return any((fnmatch.fnmatch(name, p) if "*" in p else p in name) for p in pats)

        self.tables: dict[str, list[str]] = {
            t.lower(): [c.lower() for c in cols] for t, cols in (tables or {}).items() if not skip(t.lower())
        }
        nested: dict[str, dict[str, dict[str, str]]] = {}
        for t, cols in self.tables.items():
            if "." not in t:
                continue
            db, name = t.split(".", 1)
            nested.setdefault(db, {})[name] = {c: "string" for c in cols}
        self.mapping = MappingSchema(nested, dialect="spark") if nested else MappingSchema(dialect="spark")

    def cols(self, table: str) -> list[str] | None:
        return self.tables.get(table.lower())


_RANK = {"direct": 0, "expression": 1, "aggregate": 2}


def _worse(a: str, b: str) -> str:
    return a if _RANK.get(a, 0) >= _RANK.get(b, 0) else b


@dataclass(frozen=True)
class _Leaf:
    table: str
    col: str
    inferred: bool = False
    transform: str = "direct"   # the strongest transform on the path from this source to the output
    expr: str = ""              # the expression where that transform happens ("" for a plain copy)


def _tname(t: exp.Table) -> str | None:
    parts = [p for p in _table_parts(t) if p]
    if not parts:
        return None
    name = ".".join(parts[-2:]).lower()
    return None if TOKEN_RE.search(name) else name


def _normalize_tables(root: exp.Expression) -> None:
    """Reduce workspace.lakehouse.schema.table to schema.table so schemas and names match."""
    for t in list(root.find_all(exp.Table)):
        parts = [p for p in _table_parts(t) if p]
        if len(parts) > 2:
            new = exp.Table(this=exp.to_identifier(parts[-1]), db=exp.to_identifier(parts[-2]))
            if t.args.get("alias"):
                new.set("alias", t.args["alias"].copy())
            t.replace(new)


def _core(q: exp.Expression) -> exp.Expression:
    while isinstance(q, exp.Subquery):
        q = q.this
    return q


def _attach_with(query: exp.Expression, with_: exp.With) -> exp.Expression:
    """Return a copy of ``query`` with the CTEs of ``with_`` defined ahead of its own."""
    q = query.copy()
    core = _core(q)
    existing = core.args.get(WITH_KEY)
    merged = exp.With(expressions=[c.copy() for c in with_.expressions] + (list(existing.expressions) if existing else []))
    if with_.args.get("recursive"):
        merged.set("recursive", True)
    core.set(WITH_KEY, merged)
    return q


def _classify(p: exp.Expression) -> str:
    inner = p.this if isinstance(p, exp.Alias) else p
    if isinstance(inner, exp.Column):
        return "direct"
    return "aggregate" if inner.find(exp.AggFunc) else "expression"


def _is_star(p: exp.Expression) -> bool:
    inner = p.this if isinstance(p, exp.Alias) else p
    return isinstance(inner, exp.Star) or (isinstance(inner, exp.Column) and isinstance(inner.this, exp.Star))


class _Analyzer:
    """One notebook's statements, in execution order (temp views carry over)."""

    def __init__(self, schema: ColumnSchema, temp_views: frozenset[str] = frozenset()) -> None:
        self.schema = schema
        self.temp_views = temp_views
        self.views: dict[str, exp.Expression] = {}
        self.stats = ColStats()
        self.edges: dict[tuple, ColEdge] = {}
        # id(scope) -> (scope, ids of its columns). The scope itself is kept: once a scope is freed
        # Python reuses its id, and a later statement must not pick up a dead scope's columns.
        self._col_ids: dict[int, tuple[Scope, set[int]]] = {}

    # ------------------------------------------------------------------ statements

    def run(self, statements: list[Stmt]) -> None:
        for st in statements:
            if TOKEN_RE.search(st.sql) and not self._plausible(st.sql):
                continue
            try:
                parsed = sqlglot.parse(_rewrite_mlv(st.sql), read="spark")
            except SqlglotError:
                self.stats.statements += 1
                self.stats.failed_parse += 1
                continue
            for stmt in parsed:
                if stmt is not None:
                    self._statement(stmt, st)

    @staticmethod
    def _plausible(sql: str) -> bool:
        return True   # tokens are harmless in value positions; targets are checked per statement

    def _statement(self, s: exp.Expression, st: Stmt) -> None:
        _normalize_tables(s)
        before = len(self.edges)
        if isinstance(s, exp.Create):
            self._create(s, st)
        elif isinstance(s, exp.Insert):
            self._insert(s, st)
        elif isinstance(s, exp.Merge):
            self._merge(s, st)
        else:
            return
        self.stats.statements += 1
        if len(self.edges) > before:
            self.stats.with_edges += 1

    def _create(self, s: exp.Create, st: Stmt) -> None:
        this = s.this.this if isinstance(s.this, exp.Schema) else s.this
        query = s.expression
        if not isinstance(this, exp.Table) or not isinstance(query, exp.Query):
            self.stats.not_a_write += 1
            return
        name = _tname(this)
        if _is_temp(s):                       # temp view: later statements may select from it
            if name:
                body = _core(query).copy()
                if name in self.views and any(not t.db and t.name.lower() == name for t in body.find_all(exp.Table)):
                    # redefined from itself (v = v UNION ...): keep the old definition inside the new one
                    body = self._with_views(body)
                self.views[name] = body
            return
        self._write(name, query, None, False, st)

    def _insert(self, s: exp.Insert, st: Stmt) -> None:
        this = s.this
        cols = [c.name.lower() for c in this.expressions] if isinstance(this, exp.Schema) else None
        table = this.this if isinstance(this, exp.Schema) else this
        query = s.expression
        if not isinstance(table, exp.Table) or not isinstance(query, exp.Query):
            self.stats.not_a_write += 1
            return
        # `WITH c AS (...) INSERT INTO t SELECT ... FROM c` keeps the WITH on the INSERT, not on the query
        if s.args.get(WITH_KEY):
            query = _attach_with(query, s.args[WITH_KEY])
        self._write(_tname(table), query, cols, True, st)

    # ------------------------------------------------------------------ INSERT / CTAS

    def _write(self, target: str | None, query: exp.Expression, cols: list[str] | None, positional: bool, st: Stmt) -> None:
        if not target:
            self.stats.dynamic_target += 1
            return
        outputs = self._outputs(query)
        if outputs is None:
            return
        out_names = [o[0] for o in outputs]
        names = out_names
        # assumed[i]: the target column for output i was guessed, not written in the SQL
        assumed = [False] * len(outputs)
        if cols is not None and len(cols) == len(outputs):
            names = cols
        elif positional and cols is None:
            target_cols = self.schema.cols(target)
            if target_cols and len(target_cols) == len(outputs) and "*" not in out_names:
                names = target_cols                                  # INSERT maps by position
                assumed = [t != o for t, o in zip(target_cols, out_names, strict=True)]
            elif target_cols:                                         # count differs: names are a guess
                assumed = [True] * len(outputs)
        for i, ((_name, leaves, transform, expr), tcol) in enumerate(zip(outputs, names, strict=True)):
            for leaf in leaves:
                self._emit(leaf, target, tcol, transform, expr, st, assumed[i])

    def _outputs(self, query: exp.Expression) -> list[tuple[str, list[_Leaf], str, str]] | None:
        """[(output name, source leaves, transform, expression)] for a query, or None on failure."""
        q = self._with_views(_core(query))
        try:
            qualified = qualify(q.copy(), dialect="spark", schema=self.schema.mapping,
                                validate_qualify_columns=False, identify=False)
            scope = build_scope(qualified)
        except Exception:   # sqlglot raises several error types for odd SQL
            self.stats.failed_qualify += 1
            return None
        if scope is None:
            self.stats.failed_qualify += 1
            return None
        return self._project(scope, qualified)

    def _project(self, scope: Scope, query: exp.Expression) -> list[tuple[str, list[_Leaf], str, str]]:
        if isinstance(query, exp.SetOperation):
            branches = [b for b in scope.set_operation_scopes]
            first = query.selects
            result = []
            for i, p in enumerate(first):
                leaves: list[_Leaf] = []
                for b in branches:
                    sels = b.expression.selects if isinstance(b.expression, exp.Select) else []
                    if i < len(sels):
                        leaves.extend(self._leaves(b, sels[i], 0))
                result.append((p.alias_or_name.lower(), self._dedupe(leaves), _classify(p), p.sql(dialect="spark")[:EXPR_LIMIT]))
            return result
        result = []
        for p in query.selects:
            if _is_star(p):
                result.extend(self._expand_star(scope, p))
                continue
            result.append((p.alias_or_name.lower(), self._dedupe(self._leaves(scope, p, 0)),
                           _classify(p), (p.this if isinstance(p, exp.Alias) else p).sql(dialect="spark")[:EXPR_LIMIT]))
        return result

    def _expand_star(self, scope: Scope, p: exp.Expression) -> list[tuple[str, list[_Leaf], str, str]]:
        inner = p.this if isinstance(p, exp.Alias) else p
        only = inner.table if isinstance(inner, exp.Column) else None
        out = []
        for alias, src in scope.sources.items():
            if only and alias != only:
                continue
            if isinstance(src, exp.Table):
                name = _tname(src)
                if name:
                    self.stats.unresolved_stars += 1
                    out.append(("*", [_Leaf(name, "*", True)], "star", "*"))
            elif isinstance(src, Scope):
                for n in self._names(src):
                    out.append((n, self._dedupe(self._inner(src, n, 1, True)), "direct", n))
        return out

    # ------------------------------------------------------------------ MERGE

    def _merge(self, m: exp.Merge, st: Stmt) -> None:
        target_t = m.this
        using = m.args.get("using")
        whens = m.args.get("whens")
        if not isinstance(target_t, exp.Table) or using is None or whens is None:
            self.stats.not_a_write += 1
            return
        target = _tname(target_t)
        if not target:
            self.stats.dynamic_target += 1
            return
        for w in whens.expressions:
            then = w.args.get("then")
            if isinstance(then, exp.Update):
                for e in then.expressions:
                    if isinstance(e, exp.Star):
                        self._merge_by_name(target, using, st)
                    elif isinstance(e, exp.EQ) and isinstance(e.this, exp.Column):
                        self._merge_assign(target, e.this.name.lower(), e.expression, using, st)
            elif isinstance(then, exp.Insert):
                if isinstance(then.this, exp.Star):
                    self._merge_by_name(target, using, st)
                elif isinstance(then.this, exp.Tuple) and isinstance(then.expression, exp.Tuple):
                    for c, v in zip(then.this.expressions, then.expression.expressions, strict=False):
                        self._merge_assign(target, c.name.lower(), v, using, st)

    def _merge_assign(self, target: str, tcol: str, value: exp.Expression, using: exp.Expression, st: Stmt) -> None:
        sel = exp.select(exp.alias_(value.copy(), "__o")).from_(using.copy())
        outs = self._outputs(sel)
        if not outs:
            return
        _name, leaves, transform, expr = outs[0]
        for leaf in leaves:
            if leaf.table != target:
                self._emit(leaf, target, tcol, transform, expr, st, False)

    def _merge_by_name(self, target: str, using: exp.Expression, st: Stmt) -> None:
        outs = self._outputs(exp.select("*").from_(using.copy()))
        for name, leaves, transform, expr in outs or []:
            for leaf in leaves:
                self._emit(leaf, target, name, transform, expr, st, True)

    # ------------------------------------------------------------------ tracing

    def _with_views(self, q: exp.Expression) -> exp.Expression:
        """Make earlier temp views visible to the statement as CTEs (dependencies first)."""
        if not self.views:
            return q
        q = q.copy()
        order: list[str] = []          # views in dependency order: a view after the views it reads
        visiting: set[str] = set()

        def uses(e: exp.Expression) -> list[str]:
            defined = {c.alias.lower() for c in e.find_all(exp.CTE) if c.alias}
            return [t.name.lower() for t in e.find_all(exp.Table)
                    if not t.db and t.name.lower() in self.views and t.name.lower() not in defined]

        def visit(n: str) -> None:
            if n in order or n in visiting:       # a view that reads itself (redefined in place) stops here
                return
            visiting.add(n)
            for m in uses(self.views[n]):
                visit(m)
            visiting.discard(n)
            order.append(n)

        for n in uses(q):
            visit(n)
        if not order:
            return q
        ctes = [exp.CTE(this=self.views[n].copy(), alias=exp.TableAlias(this=exp.to_identifier(n))) for n in order]
        existing = q.args.get(WITH_KEY)
        q.set(WITH_KEY, exp.With(expressions=ctes + (list(existing.expressions) if existing else [])))
        return q

    def _scope_cols(self, scope: Scope) -> set[int]:
        hit = self._col_ids.get(id(scope))
        if hit is None or hit[0] is not scope:
            hit = (scope, {id(c) for c in scope.columns})
            self._col_ids[id(scope)] = hit
        return hit[1]

    def _leaves(self, scope: Scope, expr: exp.Expression, depth: int) -> list[_Leaf]:
        ids = self._scope_cols(scope)
        here = _classify(expr)                       # how this level combines its inputs
        text = (expr.this if isinstance(expr, exp.Alias) else expr).sql(dialect="spark")[:EXPR_LIMIT]
        out: list[_Leaf] = []
        for col in expr.find_all(exp.Column):
            if id(col) in ids and not TOKEN_RE.search(col.name):
                for lf in self._resolve(scope, col, depth):
                    # keep the strongest transform seen so far, with the expression that caused it
                    stronger_here = _RANK.get(here, 0) > _RANK.get(lf.transform, 0)
                    out.append(_Leaf(lf.table, lf.col, lf.inferred, _worse(here, lf.transform),
                                     text if stronger_here else lf.expr))
        return out

    def _resolve(self, scope: Scope, col: exp.Column, depth: int) -> list[_Leaf]:
        if depth > MAX_DEPTH:
            return []
        alias, inferred = col.table, False
        if not alias:
            alias = self._pick_source(scope, col.name.lower())
            inferred = True
            if alias is None:
                self.stats.ambiguous_columns += 1
                return []
        src = scope.sources.get(alias)
        name = col.name.lower()
        if isinstance(src, exp.Table):
            t = _tname(src)
            return [_Leaf(t, name, inferred)] if t else []
        if isinstance(src, Scope):
            return self._inner(src, name, depth + 1, inferred)
        return []

    def _pick_source(self, scope: Scope, name: str) -> str | None:
        """Alias a column without a table prefix belongs to, or None when it cannot be decided.

        One source: that one. Several: the only source known to have the column; failing that,
        the only source whose columns are unknown (a table without a schema, a SELECT *).
        """
        if len(scope.sources) == 1:
            return next(iter(scope.sources))
        has: list[str] = []
        unknown: list[str] = []
        for alias, src in scope.sources.items():
            if isinstance(src, exp.Table):
                t = _tname(src)
                known = self.schema.cols(t) if t else None
                if known is None:
                    unknown.append(alias)
                elif name in known:
                    has.append(alias)
            elif isinstance(src, Scope):
                if any(_is_star(p) for p in src.expression.selects):
                    unknown.append(alias)
                elif name in self._names(src):
                    has.append(alias)
        if len(has) == 1:
            return has[0]
        if not has and len(unknown) == 1:
            return unknown[0]
        return None

    def _names(self, scope: Scope) -> list[str]:
        e = scope.expression
        return [p.alias_or_name.lower() for p in e.selects if not _is_star(p)]

    def _inner(self, scope: Scope, name: str, depth: int, inferred: bool) -> list[_Leaf]:
        """Source leaves of output column ``name`` of a CTE / derived table / union scope."""
        if depth > MAX_DEPTH:
            return []
        e = scope.expression
        leaves: list[_Leaf] = []
        if isinstance(e, exp.SetOperation) and e.args.get("by_name"):
            # UNION ... BY NAME (DataFrame unionByName): each branch gives its column of the same name
            for b in scope.set_operation_scopes:
                leaves.extend(self._inner(b, name, depth + 1, False))
        elif isinstance(e, exp.SetOperation):
            names = [p.alias_or_name.lower() for p in e.selects]
            if name not in names:
                return []
            i = names.index(name)
            for b in scope.set_operation_scopes:
                sels = b.expression.selects if isinstance(b.expression, exp.Select) else []
                if i < len(sels):
                    leaves.extend(self._leaves(b, sels[i], depth))
        else:
            proj = next((p for p in e.selects if not _is_star(p) and p.alias_or_name.lower() == name), None)
            if proj is not None:
                leaves.extend(self._leaves(scope, proj, depth))
            elif any(_is_star(p) for p in e.selects):   # an unexpanded star passes columns through by name
                for src in scope.sources.values():
                    if isinstance(src, exp.Table) and _tname(src):
                        leaves.append(_Leaf(_tname(src), name, True))
                    elif isinstance(src, Scope):
                        leaves.extend(self._inner(src, name, depth + 1, True))
        return [_Leaf(x.table, x.col, x.inferred or inferred, x.transform, x.expr) for x in leaves]

    @staticmethod
    def _dedupe(leaves: list[_Leaf]) -> list[_Leaf]:
        seen: dict[tuple[str, str], _Leaf] = {}
        for lf in leaves:
            k = (lf.table, lf.col)
            old = seen.get(k)
            if old is None:
                seen[k] = lf
                continue
            stronger = lf if _RANK.get(lf.transform, 0) > _RANK.get(old.transform, 0) else old
            seen[k] = _Leaf(old.table, old.col, old.inferred and lf.inferred, stronger.transform, stronger.expr)
        return list(seen.values())

    def _emit(self, leaf: _Leaf, target: str, tcol: str, transform: str, expr: str, st: Stmt, extra_inferred: bool) -> None:
        if "." not in leaf.table:
            if leaf.table in self.temp_views and leaf.table not in self.views:
                self.stats.temp_view_sources += 1     # a view made from DataFrame code: nothing to trace through
            else:
                self.stats.unqualified_sources += 1   # a CTE or view we could not see defined, not a table
            return
        conf = "inferred" if (leaf.inferred or extra_inferred or leaf.col == "*") else "parsed"
        if transform != "star":                       # the path's strongest transform wins over the outer level
            transform, expr = leaf.transform, leaf.expr or expr
        edge = ColEdge(leaf.table, leaf.col, target, tcol, transform, expr, conf, st.line, st.via)
        key = (edge.source, edge.source_col, edge.target, edge.target_col)
        old = self.edges.get(key)
        if old is None or (old.conf == "inferred" and conf == "parsed"):
            self.edges[key] = edge


def analyze_notebook(statements: list[Stmt], schema: ColumnSchema,
                     temp_views: frozenset[str] | set[str] = frozenset()) -> tuple[list[ColEdge], ColStats]:
    """Column edges and statistics for one notebook's executed SQL."""
    a = _Analyzer(schema, frozenset(temp_views))
    a.run(statements)
    return list(a.edges.values()), a.stats





