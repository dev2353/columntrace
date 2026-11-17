"""Static string evaluation for notebook code.

Resolves string constants, f-strings, concatenation and ``.format`` where the
parts are literals or earlier constant assignments. Anything that cannot be
resolved becomes a ``__dynN__`` token so downstream parsers (sqlglot) still see
valid identifiers, and the original expression can be shown to the user.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass

TOKEN_RE = re.compile(r"__dyn\d+__")


@dataclass
class Tpl:
    text: str

    @property
    def is_dynamic(self) -> bool:
        return bool(TOKEN_RE.search(self.text))


class StrEval:
    def __init__(self) -> None:
        self.consts: dict[str, Tpl] = {}
        self.funcs: dict[str, tuple[list[str], ast.AST]] = {}   # one-line helpers: name -> (params, returned expression)
        self._depth = 0
        self.dyn: dict[str, str] = {}   # token -> original expression source
        self._n = 0

    def _token(self, src: str) -> str:
        tok = f"__dyn{self._n}__"
        self._n += 1
        self.dyn[tok] = src
        return tok

    @staticmethod
    def _src(node: ast.AST) -> str:
        try:
            return ast.unparse(node)
        except Exception:  # pragma: no cover - defensive
            return "?"

    def eval(self, node: ast.AST) -> Tpl | None:
        """Return a template for string-like nodes, or None if not string-like."""
        if isinstance(node, ast.Constant):
            return Tpl(node.value) if isinstance(node.value, str) else None
        if isinstance(node, ast.Name):
            return self.consts.get(node.id)
        if isinstance(node, ast.JoinedStr):
            parts: list[str] = []
            for v in node.values:
                if isinstance(v, ast.Constant):
                    parts.append(str(v.value))
                elif isinstance(v, ast.FormattedValue):
                    inner = self.eval(v.value)
                    if inner is None and isinstance(v.value, ast.Constant):
                        inner = Tpl(str(v.value.value))
                    parts.append(inner.text if inner else self._token(self._src(v.value)))
            return Tpl("".join(parts))
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left, right = self.eval(node.left), self.eval(node.right)
            if left is None and right is None:
                return None
            lt = left.text if left else self._token(self._src(node.left))
            rt = right.text if right else self._token(self._src(node.right))
            return Tpl(lt + rt)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
            left = self.eval(node.left)
            if left is None:
                return None
            text = re.sub(r"%[sdr]", lambda _m: self._token(self._src(node.right)), left.text)
            return Tpl(text)
        if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, int):
            parts = self._split(node.value)                # "a.b".split(".")[0]
            if parts is not None and -len(parts) <= node.slice.value < len(parts):
                return Tpl(parts[node.slice.value])
            return None
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in self.funcs and self._depth < 5:
            # a helper like obj(schema, name) -> f"{LH}.`{schema}`.`{name}`": evaluate its return with the arguments
            params, body = self.funcs[node.func.id]
            saved = {p: self.consts.get(p) for p in params}
            for p, arg in zip(params, node.args, strict=False):
                self.consts[p] = self.eval(arg) or Tpl(self._token(self._src(arg)))
            self._depth += 1
            try:
                return self.eval(body)
            finally:
                self._depth -= 1
                for p, v in saved.items():
                    if v is None:
                        self.consts.pop(p, None)
                    else:
                        self.consts[p] = v
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            attr = node.func.attr
            if attr == "format":
                recv = self.eval(node.func.value)
                if recv is None:
                    return None
                text = re.sub(r"\{[^{}]*\}", lambda _m: self._token("format-arg"), recv.text)
                return Tpl(text)
            if attr in {"strip", "lstrip", "rstrip", "lower", "upper"}:
                recv = self.eval(node.func.value)
                if recv is None:
                    return None
                if recv.is_dynamic:
                    return recv
                return Tpl(getattr(recv.text, attr)())
        return None

    def _split(self, node: ast.AST) -> list[str] | None:
        """The parts of ``<constant string>.split(sep[, maxsplit])``, or None if not that shape."""
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "split"):
            return None
        recv = self.eval(node.func.value)
        if recv is None or recv.is_dynamic or not node.args:
            return None
        args = node.args
        if not (isinstance(args[0], ast.Constant) and isinstance(args[0].value, str) and args[0].value):
            return None
        if len(args) > 2 or (len(args) == 2 and not (isinstance(args[1], ast.Constant) and isinstance(args[1].value, int))):
            return None
        return recv.text.split(args[0].value, args[1].value) if len(args) == 2 else recv.text.split(args[0].value)

    def collect_constants(self, tree: ast.AST) -> None:
        """Record simple ``NAME = <string expr>`` assignments in source order."""
        for f in ast.walk(tree):
            if isinstance(f, ast.FunctionDef):
                body = [b for b in f.body if not (isinstance(b, ast.Expr) and isinstance(b.value, ast.Constant))]
                if len(body) == 1 and isinstance(body[0], ast.Return) and body[0].value is not None:
                    self.funcs[f.name] = ([a.arg for a in f.args.args], body[0].value)
        assigns = [n for n in ast.walk(tree) if isinstance(n, (ast.Assign, ast.AnnAssign))]
        for n in sorted(assigns, key=lambda x: x.lineno):
            if (isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Tuple)
                    and all(isinstance(t, ast.Name) for t in n.targets[0].elts)):
                parts = self._split(n.value)               # schema, table = name.split(".", 1)
                if parts is not None and len(parts) == len(n.targets[0].elts):
                    for t, part in zip(n.targets[0].elts, parts, strict=True):
                        self.consts[t.id] = Tpl(part)  # type: ignore[attr-defined]
                continue
            if isinstance(n, ast.Assign):
                if len(n.targets) != 1 or not isinstance(n.targets[0], ast.Name):
                    continue
                target, value = n.targets[0].id, n.value
            else:
                if not isinstance(n.target, ast.Name) or n.value is None:
                    continue
                target, value = n.target.id, n.value
            tpl = self.eval(value)
            if tpl is not None:
                self.consts[target] = tpl

    def render(self, text: str) -> str:
        """Replace tokens with ``{original expression}`` for display."""
        return TOKEN_RE.sub(lambda m: "{" + self.dyn.get(m.group(0), "?") + "}", text)
