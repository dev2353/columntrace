"""Notebook definition parser (notebook-content.py / notebook-content.sql).

Finds table reads/writes in Spark SQL (``spark.sql``, ``%%sql`` cells), PySpark
APIs (``saveAsTable``, ``spark.table`` ...), Delta helpers and the shared
``fabric_delta_schema_helpers`` path helpers, plus notebook-to-notebook calls.
"""

from __future__ import annotations

import ast
import re
import warnings
from dataclasses import dataclass, field

from .sql_refs import extract_refs
from .strings import TOKEN_RE, StrEval, Tpl

PY_CELL_RE = re.compile(r"^# (CELL|MARKDOWN|PARAMETERS CELL) \*+[ \t]*$", re.M)
SQL_CELL_RE = re.compile(r"^-- (CELL|MARKDOWN) \*+[ \t]*$", re.M)

# helper -> (arity, mode).  arity 2 = (schema, table), 1 = "schema.table"
TABLE_HELPERS: dict[str, tuple[int, str]] = {
    "lakehouse_table_path": (2, "read"),
    "read_lakehouse_table_path": (2, "read"),
    "write_lakehouse_table_path": (2, "write"),
    "write_table_path": (2, "write"),
    "read_delta_table": (1, "read"),
    "write_delta_table": (1, "write"),
}
SQL_CALLS = {"execute_query", "execute_mutation_sql", "execute", "safe_execute",
             "read_sql_query", "register_query_view"}
SQL_RE = re.compile(r"(?is)\b(select|insert|merge|create|update|delete|with|truncate)\b")
PATH_READERS = {"DeltaTable", "read_delta", "scan_delta"}
PATH_WRITERS = {"write_deltalake"}
TABLES_PATH_RE = re.compile(r"(?:^|/)Tables/(?:([A-Za-z_][\w]*)/)?([\w{}.\-]+)")   # also relative "Tables/s/t"
TEMP_VIEW_PY = re.compile(
    r"(?:createOrReplaceTempView|createTempView|createOrReplaceGlobalTempView|registerTempTable)"
    r"\(\s*[fF]?[\"']([^\"'{]+)[\"']")
TEMP_VIEW_SQL = re.compile(
    r"(?is)CREATE\s+(?:OR\s+REPLACE\s+)?(?:GLOBAL\s+)?TEMP(?:ORARY)?\s+VIEW\s+(?:IF\s+NOT\s+EXISTS\s+)?([\w.]+)")
MERGE_HINT = re.compile(r"\.(merge|whenMatched\w*|whenNotMatched\w*)\(|\.update\(|\.delete\(")


@dataclass
class Hit:
    name: str                 # table name or template with {expr}
    mode: str                 # read | write
    confidence: str           # parsed | inferred | unresolved
    evidence: dict


@dataclass
class Stmt:
    """A SQL statement the notebook executes, in execution order (column lineage input)."""
    sql: str                  # may contain __dynN__ tokens for run-time parts
    line: int
    via: str


# DataFrame methods that leave the columns unchanged: lineage can follow `df` through them
DF_PASSTHROUGH = {
    "write", "mode", "format", "option", "options", "partitionBy", "bucketBy", "sortBy", "cache", "persist",
    "repartition", "coalesce", "distinct", "filter", "where", "limit", "orderBy", "sort", "dropDuplicates",
}
# DataFrame methods that stack two DataFrames: a.unionByName(b) matches columns by name, the others by position
DF_UNIONS = {"union", "unionAll", "unionByName"}


def _chain(node: ast.AST) -> tuple[str | None, list[str]]:
    """Root variable and the method names applied to it: df.write.mode(..) -> ('df', ['mode','write'])."""
    methods: list[str] = []
    while True:
        if isinstance(node, ast.Call):
            node = node.func
        elif isinstance(node, ast.Attribute):
            methods.append(node.attr)
            node = node.value
        else:
            break
    return (node.id if isinstance(node, ast.Name) else None), methods


@dataclass
class NotebookInfo:
    statements: list[Stmt] = field(default_factory=list)
    temp_views: set[str] = field(default_factory=set)   # names registered as temp views anywhere in the notebook
    hits: list[Hit] = field(default_factory=list)
    runs: list[tuple[str, str, dict]] = field(default_factory=list)  # (name, confidence, evidence)
    cells: int = 0
    sql_cells: int = 0
    parse_errors: int = 0
    other_cells: int = 0        # cells in another language (%%spark, %%configure ...)
    fallback_sql: int = 0
    unresolved_sql: int = 0     # spark.sql(<variable we could not resolve>)
    unresolved_where: list[str] = field(default_factory=list)   # "line 12: cmd" for each of them
    literals: list[tuple[str, int]] = field(default_factory=list)  # "schema.table" string constants
    skipped: bool = False


@dataclass
class _NbShared:
    """What earlier cells teach later ones."""
    helpers: dict[str, tuple[int, str]] = field(default_factory=dict)   # function -> (SQL argument position, name)
    funcs: set[str] = field(default_factory=set)                          # functions defined in the notebook
    literals: dict[str, ast.AST] = field(default_factory=dict)           # name -> a literal list, tuple, set or dict


def _param_of(node: ast.AST, params: list[str], derived: dict[str, str]) -> str | None:
    """The function parameter an expression passes on: p, a local computed from p, or wrap(p)."""
    if isinstance(node, ast.Name):
        return node.id if node.id in params else derived.get(node.id)
    if isinstance(node, ast.Call) and node.args:
        return _param_of(node.args[0], params, derived)
    return None


# cell magics whose body is not Python: %%spark is Scala, %%configure is JSON session settings
NON_PYTHON_MAGICS = {"spark", "sparkr", "csharp", "configure", "html", "md", "markdown"}


def _text_scan(code: str, line: int, add_name, tag: str) -> None:
    """Table names found by plain text search, for code that cannot be parsed (all ``inferred``)."""
    for m in re.finditer(r"saveAsTable\(\s*[fF]?[\"']([\w.]+)[\"']", code):
        add_name(m.group(1), "write", "inferred", line, f"saveAsTable {tag}")
    for m in re.finditer(r"(?i)\b(?:FROM|JOIN)\s+([a-z_]\w*\.\w+)", code):
        add_name(m.group(1), "read", "inferred", line, f"FROM {tag}")


def _split_cells(text: str, is_sql: bool) -> list[tuple[str, str, int]]:
    """Return [(kind, body, start_line)] where kind in CELL|MARKDOWN|PARAMETERS CELL."""
    regex = SQL_CELL_RE if is_sql else PY_CELL_RE
    marker = "-- METADATA" if is_sql else "# METADATA"
    matches = list(regex.finditer(text))
    cells = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[m.end():end]
        cut = body.find(marker)
        if cut != -1:
            body = body[:cut]
        start_line = text.count("\n", 0, m.end()) + 1
        cells.append((m.group(1), body, start_line))
    return cells


def _short(s: str, n: int = 110) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


SQL_STMT = re.compile(
    r"(?is)^\s*(?:--[^\n]*\n\s*)*(?:select\b.+\bfrom\b|insert\s+(?:into|overwrite)\b|merge\s+into\b"
    r"|update\s+\S+\s+set\b|delete\s+from\b|with\s+\w+\s+as\s*\(|"
    r"create\s+(?:or\s+replace\s+)?(?:table|view|materialized)\b|truncate\s+table\b)")
LITERAL_NAME = re.compile(r"^[A-Za-z_][\w]*\.[A-Za-z_][\w]*$")


class _CellVisitor(ast.NodeVisitor):
    def __init__(self, ev: StrEval, temp_views: set[str], emit, emit_run, line0: int, dfs: dict[str, str],
                 shared: _NbShared | None = None):
        self.ev, self.temp_views, self.emit, self.emit_run, self.line0 = ev, temp_views, emit, emit_run, line0
        self.dfs = dfs                   # DataFrame variable -> the SQL that created it
        self.handled: set[int] = set()   # string nodes already passed to a known API
        self.shared = shared or _NbShared()
        self.loop: dict[str, list[Tpl]] = {}   # loop variables over literal collections -> their values
        self.quiet: set[str] = set()           # inside a SQL helper: its parameters are filled in by the callers

    def visit_Assign(self, node: ast.Assign) -> None:
        # df = spark.sql(...) / spark.table("t") / df2 = df.filter(...): remember the SQL behind df
        if len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            sql = self._df_sql(node.value)
            if sql is not None:
                self.dfs[node.targets[0].id] = sql
            if isinstance(node.value, (ast.List, ast.Tuple, ast.Set, ast.Dict)):
                self.shared.literals[node.targets[0].id] = node.value     # e.g. QUERIES = {"t": """SELECT ..."""}
        self.generic_visit(node)

    # ---- SQL helpers, loops and literal collections of queries

    def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        """A function that runs one of its own parameters as SQL is a helper: its callers give the SQL."""
        self.shared.funcs.add(node.name)
        params = [a.arg for a in [*node.args.posonlyargs, *node.args.args]]
        derived: dict[str, str] = {}
        for n in ast.walk(node):
            if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name):
                src = _param_of(n.value, params, derived)
                if src:
                    derived[n.targets[0].id] = src              # body = normalize_sql(sql)
        sql_param = None
        for n in ast.walk(node):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "sql" and n.args:
                sql_param = _param_of(n.args[0], params, derived)
                if sql_param:
                    break
        if sql_param is None or (params and params[0] == "self"):
            self.generic_visit(node)
            return
        self.shared.helpers[node.name] = (params.index(sql_param), sql_param)
        outer, self.quiet = self.quiet, self.quiet | set(params) | set(derived)
        self.generic_visit(node)
        self.quiet = outer

    visit_AsyncFunctionDef = visit_FunctionDef

    def _items(self, node: ast.AST) -> list[ast.AST] | None:
        """The elements a for loop over ``node`` visits, when they are written in the notebook."""
        mode = None
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and not node.args
                and node.func.attr in ("items", "values", "keys")):
            mode, node = node.func.attr, node.func.value
        if isinstance(node, ast.Name):
            node = self.shared.literals.get(node.id, node)
        if isinstance(node, ast.Dict):
            pairs = [(k, v) for k, v in zip(node.keys, node.values, strict=True) if k is not None]
            if mode == "items":
                return [ast.Tuple(elts=[k, v], ctx=ast.Load()) for k, v in pairs]
            return [v for _, v in pairs] if mode == "values" else [k for k, _ in pairs]
        if isinstance(node, (ast.List, ast.Tuple, ast.Set)) and mode is None:
            return list(node.elts)
        return None

    def _bind(self, target: ast.AST, it: ast.AST) -> dict[str, list[Tpl]]:
        """Loop variable -> every string value it takes, for loops over literal collections."""
        out: dict[str, list[Tpl]] = {}
        for item in (self._items(it) or [])[:200]:
            if isinstance(target, ast.Name):
                out.setdefault(target.id, []).extend(self._sql_values(item))
            elif isinstance(target, ast.Tuple) and isinstance(item, ast.Tuple) and len(item.elts) == len(target.elts):
                for t, e in zip(target.elts, item.elts, strict=True):
                    if isinstance(t, ast.Name):
                        out.setdefault(t.id, []).extend(self._sql_values(e))
        return {k: v for k, v in out.items() if v}

    def visit_For(self, node: ast.For) -> None:
        self.visit(node.iter)
        outer = dict(self.loop)
        self.loop.update(self._bind(node.target, node.iter))
        for st in node.body:
            self.visit(st)
        self.loop = outer
        for st in node.orelse:
            self.visit(st)

    def _visit_comprehension(self, node: ast.ListComp | ast.SetComp | ast.GeneratorExp | ast.DictComp) -> None:
        outer = dict(self.loop)
        for g in node.generators:
            self.visit(g.iter)
            self.loop.update(self._bind(g.target, g.iter))
            for cond in g.ifs:
                self.visit(cond)
        for part in (getattr(node, "elt", None), getattr(node, "key", None), getattr(node, "value", None)):
            if part is not None:
                self.visit(part)
        self.loop = outer

    visit_ListComp = visit_SetComp = visit_GeneratorExp = visit_DictComp = _visit_comprehension

    def _sql_values(self, node: ast.AST, seen: frozenset[str] = frozenset()) -> list[Tpl]:
        """Every SQL text an expression can hold, when the notebook spells them out; [] otherwise.
        ``seen`` holds the collection names being expanded, so `x = [x, ...]` cannot loop forever."""
        if isinstance(node, ast.Name) and node.id in self.loop:
            return self.loop[node.id]
        bound = next((n.id for n in ast.walk(node) if isinstance(n, ast.Name) and n.id in self.loop), None)
        if bound is not None and not isinstance(node, ast.Name):
            # f"MERGE INTO {tbl} ..." inside `for tbl in [...]`: one text per value of the loop variable
            saved, out = self.ev.consts.get(bound), []
            for value in self.loop[bound][:50]:
                self.ev.consts[bound] = value
                t = self.ev.eval(node)
                if t is not None:
                    out.append(t)
            if saved is None:
                self.ev.consts.pop(bound, None)
            else:
                self.ev.consts[bound] = saved
            if out:
                return out
        tpl = self.ev.eval(node)
        if tpl is not None:
            return [tpl]
        if isinstance(node, ast.Name) and node.id in self.shared.literals and node.id not in seen:
            return self._sql_values(self.shared.literals[node.id], seen | {node.id})
        if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
            return [t for e in node.elts for t in self._sql_values(e, seen)]
        if isinstance(node, ast.Dict):
            return [t for v in node.values for t in self._sql_values(v, seen)]
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name):
            coll = self.shared.literals.get(node.value.id)
            if isinstance(coll, ast.Dict):
                key = self.ev.eval(node.slice)
                if key is not None and not key.is_dynamic:
                    for k, v in zip(coll.keys, coll.values, strict=True):
                        kt = self.ev.eval(k) if k is not None else None
                        if kt is not None and kt.text == key.text:
                            return self._sql_values(v, seen | {node.value.id})
                if node.value.id not in seen:
                    return self._sql_values(coll, seen | {node.value.id})   # QUERIES[table], key known only at run time
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in self.shared.funcs
                and node.args):
            return self._sql_values(node.args[0], seen)          # the notebook's own wrapper: normalize_sql("""...""")
        return []

    def _quiet(self, node: ast.AST) -> bool:
        return any(isinstance(n, ast.Name) and n.id in self.quiet for n in ast.walk(node))

    def _run_sql(self, arg: ast.AST, line: int, via: str) -> None:
        vals = self._sql_values(arg)
        if vals:
            self.handled.add(id(arg))
            for v in vals:
                self.emit.sql(v.text, line, via)
        elif not self._quiet(arg):
            self.emit.unresolved_sql(line, ast.unparse(arg)[:60])

    def _df_sql(self, node: ast.AST) -> str | None:
        """The SQL behind a DataFrame expression, if it is built by ``spark.sql(...)`` or
        ``spark.table(...)`` (or a variable holding one) and only passed through column-preserving
        methods; otherwise None."""
        methods: list[str] = []
        while True:
            if isinstance(node, ast.Call):
                f = node.func
                if isinstance(f, ast.Attribute) and f.attr == "sql" and node.args:
                    tpl = self.ev.eval(node.args[0])
                    return tpl.text if tpl is not None and all(m in DF_PASSTHROUGH for m in methods) else None
                if isinstance(f, ast.Attribute) and f.attr == "table" and node.args and (
                        (isinstance(f.value, ast.Name) and f.value.id in {"spark", "session"})
                        or (isinstance(f.value, ast.Attribute) and f.value.attr == "read")):
                    tpl = self.ev.eval(node.args[0])
                    ok = tpl is not None and not tpl.is_dynamic and all(m in DF_PASSTHROUGH for m in methods)
                    return f"SELECT * FROM {tpl.text}" if ok and tpl is not None else None
                if isinstance(f, ast.Attribute) and f.attr in DF_UNIONS and node.args:
                    # a.union(b): both sides must be SQL we can see; unionByName becomes UNION ALL BY NAME
                    left, right = self._df_sql(f.value), self._df_sql(node.args[0])
                    if left is None or right is None or not all(m in DF_PASSTHROUGH for m in methods):
                        return None
                    by = " BY NAME" if f.attr == "unionByName" else ""
                    return f"SELECT * FROM ({left}) AS _u1 UNION ALL{by} SELECT * FROM ({right}) AS _u2"
                node = f
            elif isinstance(node, ast.Attribute):
                methods.append(node.attr)
                node = node.value
            elif isinstance(node, ast.Name):
                if node.id in self.dfs and all(m in DF_PASSTHROUGH for m in methods):
                    return self.dfs[node.id]
                return None
            else:
                return None

    def _literal(self, node: ast.AST) -> None:
        """Strings that were not passed to a known API: SQL held for later, or a table name."""
        if id(node) in self.handled:
            return
        tpl = self.ev.eval(node)
        if tpl is None:
            return
        line = self._ln(node)
        if SQL_STMT.match(tpl.text):
            self.emit.sql_literal(tpl.text, line)
        elif isinstance(node, ast.Constant) and LITERAL_NAME.match(node.value):
            self.emit.literal(node.value, line)

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str):
            self._literal(node)

    def visit_JoinedStr(self, node: ast.JoinedStr) -> None:
        self._literal(node)   # do not descend: nested parts are not separate strings

    def _ln(self, node: ast.AST) -> int:
        return self.line0 + getattr(node, "lineno", 1) - 1

    def _table_from(self, node: ast.AST) -> Tpl:
        """Table arguments never disappear: unresolvable ones become a token."""
        tpl = self.ev.eval(node)
        if tpl is None:
            tpl = Tpl(self.ev._token(self.ev._src(node)))
        return tpl

    def visit_Call(self, node: ast.Call) -> None:
        fn = node.func
        attr = fn.attr if isinstance(fn, ast.Attribute) else None
        ident = fn.id if isinstance(fn, ast.Name) else None
        name = attr or ident
        args = node.args
        line = self._ln(node)

        if name == "sql" and args:
            self._run_sql(args[0], line, "spark.sql")
        elif ident in self.shared.helpers:
            pos, pname = self.shared.helpers[ident]
            arg = args[pos] if len(args) > pos else next((k.value for k in node.keywords if k.arg == pname), None)
            if arg is not None:
                self._run_sql(arg, line, f"{ident}()")
        elif (name in {"map", "submit"} and len(args) >= 2 and isinstance(args[0], ast.Name)
              and args[0].id in self.shared.helpers):
            # map(helper, statements) / executor.submit(helper, sql)
            pos = self.shared.helpers[args[0].id][0]
            if name == "map":
                self._run_sql(args[1], line, f"{args[0].id}()")
            elif len(args) > pos + 1:
                self._run_sql(args[pos + 1], line, f"{args[0].id}()")
        elif name in SQL_CALLS and (args or node.keywords):
            # DuckDB / helper style: any string argument that looks like a SQL statement
            for a in [*args, *(k.value for k in node.keywords)]:
                tpl = self.ev.eval(a)
                if tpl is not None and len(tpl.text) > 12 and SQL_RE.search(tpl.text):
                    self.handled.add(id(a))
                    self.emit.sql(tpl.text, line, name)
        elif name in {"saveAsTable", "insertInto"}:
            target = args[0] if args else next(
                (k.value for k in node.keywords if k.arg in {"name", "tableName"}), None)
            if target is not None:
                ttpl = self._table_from(target)
                self.emit.table(ttpl.text, "write", line, name)
                df_sql = self._df_sql(fn.value) if isinstance(fn, ast.Attribute) else None
                if df_sql is not None and not ttpl.is_dynamic:
                    head = "CREATE TABLE" if name == "saveAsTable" else "INSERT INTO"
                    glue = " AS " if name == "saveAsTable" else " "
                    self.emit.statement(f"{head} {ttpl.text}{glue}{df_sql}", line, f"DataFrame.{name}")
        elif name in {"createOrReplaceTempView", "createTempView"} and args and isinstance(fn, ast.Attribute):
            vtpl = self.ev.eval(args[0])
            df_sql = self._df_sql(fn.value)
            if vtpl is not None and not vtpl.is_dynamic and df_sql is not None:
                self.emit.statement(f"CREATE OR REPLACE TEMP VIEW {vtpl.text} AS {df_sql}", line, f"DataFrame.{name}")
        elif name == "table" and isinstance(fn, ast.Attribute) and args:
            recv = fn.value
            is_spark = (isinstance(recv, ast.Name) and recv.id in {"spark", "session"}) or (
                isinstance(recv, ast.Attribute) and recv.attr == "read")
            if is_spark:
                self.emit.table(self._table_from(args[0]).text, "read", line, "spark.table")
        elif name == "forName" and args:
            self.emit.table(self._table_from(args[-1]).text, "delta_handle", line, "DeltaTable.forName")
        elif name in TABLE_HELPERS and isinstance(fn, ast.Name):
            arity, mode = TABLE_HELPERS[name]
            if any(k.arg == "access" and isinstance(k.value, ast.Constant) and k.value.value == "write"
                   for k in node.keywords):
                mode = "write"
            if arity == 2 and len(args) >= 2:
                a, b = self._table_from(args[0]), self._table_from(args[1])
                self.emit.table(f"{a.text}.{b.text}", mode, line, name)
            elif arity == 1 and args:
                self.emit.table(self._table_from(args[0]).text, mode, line, name)
        elif name == "forPath" and args:
            # DeltaTable.forPath(spark, "Tables/schema/table"): a write when the notebook merges into it
            tpl = self.ev.eval(args[-1])
            m = TABLES_PATH_RE.search(tpl.text) if tpl is not None else None
            if m:
                self.emit.table(f"{m.group(1) or 'dbo'}.{m.group(2)}", "delta_handle", line, "DeltaTable.forPath")
        elif name in PATH_READERS | PATH_WRITERS and args:
            tpl = self.ev.eval(args[0])
            if tpl is not None and TABLES_PATH_RE.search(tpl.text):
                m = TABLES_PATH_RE.search(tpl.text)
                if m:
                    full = f"{m.group(1) or 'dbo'}.{m.group(2)}"
                    self.emit.table(full, "write" if name in PATH_WRITERS else "read", line, name)
        elif name == "run" and isinstance(fn, ast.Attribute) and args and "notebook" in ast.unparse(fn.value):
            self.emit_run(self._table_from(args[0]).text, line, "notebook.run")
        self.generic_visit(node)


def parse_notebook(path: str, rel_path: str, text: str) -> NotebookInfo:
    info = NotebookInfo()
    is_sql = path.lower().endswith(".sql")
    cells = _split_cells(text, is_sql)
    if not cells:
        info.skipped = True
        return info

    ev = StrEval()
    temp_views: set[str] = set()
    temp_views.update(m.group(1).lower() for m in TEMP_VIEW_PY.finditer(text))
    temp_views.update(m.group(1).lower() for m in TEMP_VIEW_SQL.finditer(text))
    has_merge = bool(MERGE_HINT.search(text))
    info.temp_views = temp_views

    def evidence(line: int, via: str, snippet: str) -> dict:
        return {"file": rel_path, "line": line, "via": via, "snippet": _short(snippet)}

    def add_name(raw: str, mode: str, conf: str, line: int, via: str) -> None:
        raw = raw.strip().strip("`").lower() if not TOKEN_RE.search(raw) else raw.strip()
        if not raw:
            return
        if mode == "read" and "." not in raw and raw in temp_views:
            return                      # a view this notebook registered itself, not a table
        # drop a leading catalog/workspace/lakehouse part (4- or 3-part names)
        parts = [p for p in re.split(r"\.(?![^{]*})", raw)]
        if len(parts) > 2:
            raw = ".".join(parts[-2:])
        if "." not in raw and mode == "write" and via.split(":")[0] in SQL_CALLS:
            temp_views.add(raw)         # con.execute("CREATE TABLE t ...") without a schema: a DuckDB in-memory table
            return
        if raw.startswith(".") or raw.endswith(".") or ".." in raw:
            # an empty part, e.g. f"{db}.tmp_{name}" with empty defaults: an unset parameter, not a table
            info.hits.append(Hit(raw, mode, "unresolved", evidence(line, via, raw)))
        elif TOKEN_RE.search(raw):
            rendered = ev.render(raw)
            info.hits.append(Hit(rendered, mode, "unresolved", evidence(line, via, rendered)))
        else:
            info.hits.append(Hit(raw, mode, conf, evidence(line, via, raw)))

    class Emit:
        @staticmethod
        def table(name: str, mode: str, line: int, via: str) -> None:
            if mode == "delta_handle":
                mode = "write" if has_merge else "read"
            if "." not in name and not TOKEN_RE.search(name):
                conf = "inferred"
            else:
                conf = "parsed"
            add_name(name, mode, conf, line, via)

        @staticmethod
        def unresolved_sql(line: int = 0, src: str = "") -> None:
            info.unresolved_sql += 1
            info.unresolved_where.append(f"line {line}: {src}" if src else f"line {line}")

        @staticmethod
        def literal(name: str, line: int) -> None:
            info.literals.append((name.lower(), line))

        @staticmethod
        def sql_literal(sql_text: str, line: int) -> None:
            # SQL held in a string that we did not see executed: only trust a clean parse
            refs, _ = extract_refs(sql_text, temp_views, fallback=False)
            first = sql_text.strip().splitlines()[0] if sql_text.strip() else ""
            for r in refs:
                add_name(r.name, r.mode, "inferred", line, f"SQL string: {first}")

        @staticmethod
        def statement(sql_text: str, line: int, via: str) -> None:
            info.statements.append(Stmt(sql_text, line, via))

        @staticmethod
        def sql(sql_text: str, line: int, via: str) -> None:
            info.statements.append(Stmt(sql_text, line, via))
            refs, fallback = extract_refs(sql_text, temp_views)
            if fallback:
                info.fallback_sql += 1
            first = sql_text.strip().splitlines()[0] if sql_text.strip() else ""
            for r in refs:
                conf = "inferred" if fallback else r.confidence
                add_name(r.name, r.mode, conf, line, f"{via}: {first}")

    def emit_run(name: str, line: int, via: str) -> None:
        if TOKEN_RE.search(name):
            info.runs.append((ev.render(name), "unresolved", evidence(line, via, ev.render(name))))
        else:
            info.runs.append((name, "parsed", evidence(line, via, name)))

    emit = Emit()
    visitor_emit = type("E", (), {"table": staticmethod(emit.table), "sql": staticmethod(emit.sql),
                                  "unresolved_sql": staticmethod(emit.unresolved_sql),
                                  "literal": staticmethod(emit.literal),
                                  "sql_literal": staticmethod(emit.sql_literal),
                                  "statement": staticmethod(emit.statement)})
    dfs: dict[str, str] = {}
    shared = _NbShared()

    for kind, body, start_line in cells:
        if kind == "MARKDOWN":
            continue
        info.cells += 1
        raw_lines = body.split("\n")
        first = next((ln for ln in raw_lines if ln.strip()), "")

        # SQL cells
        if is_sql or re.match(r"#\s*MAGIC\s+%%sql", first):
            info.sql_cells += 1
            sql_lines = []
            for ln in raw_lines:
                if is_sql:
                    sql_lines.append(ln)
                else:
                    m = re.match(r"#\s*MAGIC ?(.*)$", ln)
                    if m and not m.group(1).startswith("%%sql"):
                        sql_lines.append(m.group(1))
            emit.sql("\n".join(sql_lines), start_line, "%%sql" if not is_sql else "sql notebook")
            continue

        # cells in another language (Scala, R, C#, session settings): no Python to parse, so only the
        # text scan, and no parse warning
        magic = re.match(r"#\s*MAGIC\s+%%(\w+)", first)
        if magic and magic.group(1).lower() in NON_PYTHON_MAGICS:
            info.other_cells += 1
            other = "\n".join(re.sub(r"^#\s*MAGIC ?", "", ln) for ln in raw_lines)
            _text_scan(other, start_line, add_name, f"(%%{magic.group(1).lower()})")
            continue

        # Python cells: strip magics, keep line numbers
        py_lines = []
        for idx, ln in enumerate(raw_lines):
            m = re.match(r"#\s*MAGIC ?(.*)$", ln)
            if m:
                inner = m.group(1)
                run = re.match(r"%run\s+(\S+)", inner)
                if run:
                    emit_run(run.group(1), start_line + idx, "%run")
                ln = inner if not inner.startswith("%") else ""
            if ln.lstrip().startswith(("%", "!")):
                run = re.match(r"\s*%run\s+(\S+)", ln)
                if run:
                    emit_run(run.group(1), start_line + idx, "%run")
                ln = ""
            py_lines.append(ln)
        code = "\n".join(py_lines)
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", SyntaxWarning)   # e.g. '\$' escapes in the notebook's own code
                tree = ast.parse(code)
        except SyntaxError:
            info.parse_errors += 1
            _text_scan(code, start_line, add_name, "(regex)")
            continue
        ev.collect_constants(tree)
        _CellVisitor(ev, temp_views, visitor_emit, emit_run, start_line, dfs, shared).visit(tree)

    return info


