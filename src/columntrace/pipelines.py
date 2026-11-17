"""Data pipeline definition parser (pipeline-content.json)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from .sql_refs import extract_refs
from .strings import TOKEN_RE


@dataclass
class PipelineHit:
    kind: str                  # reads | writes | calls
    target: str                # table name / template / item key
    target_type: str           # table | notebook | pipeline
    confidence: str
    evidence: dict


@dataclass
class PipelineInfo:
    hits: list[PipelineHit] = field(default_factory=list)
    activities: int = 0
    warnings: list[str] = field(default_factory=list)


_INTERP = re.compile(r"@\{[^{}]*\}")


def _value(v) -> tuple[str | None, bool]:
    """Pipeline values are plain strings or {"value": ..., "type": "Expression"}."""
    if isinstance(v, dict):
        return v.get("value"), v.get("type") == "Expression"
    if isinstance(v, str):
        return v, False
    return None, False


def _iter_activities(acts):
    for a in acts or []:
        yield a
        tp = a.get("typeProperties", {}) or {}
        yield from _iter_activities(tp.get("activities"))
        yield from _iter_activities(tp.get("ifTrueActivities"))
        yield from _iter_activities(tp.get("ifFalseActivities"))
        for case in tp.get("cases", []) or []:
            yield from _iter_activities(case.get("activities"))
        yield from _iter_activities(tp.get("defaultActivities"))


def _table_from_dataset(ds: dict | None) -> tuple[str | None, bool]:
    """Return (schema.table or template, is_dynamic)."""
    if not ds:
        return None, False
    tp = ds.get("typeProperties") or {}
    if "table" not in tp:
        return None, False
    table, t_expr = _value(tp.get("table"))
    schema, s_expr = _value(tp.get("schema")) if "schema" in tp else ("dbo", False)
    if not table:
        return None, False
    if t_expr or s_expr:
        return f"{schema or '?'}.{table}", True
    return f"{schema}.{table}".lower(), False


def parse_pipeline(rel_path: str, text: str, logical_ids: dict[str, tuple[str, str]]) -> PipelineInfo:
    """``logical_ids`` maps logicalId/displayName -> (item type, display name)."""
    info = PipelineInfo()
    data = json.loads(text)
    acts = (data.get("properties") or {}).get("activities")

    def ev(activity: str, via: str, snippet: str = "") -> dict:
        return {"file": rel_path, "activity": activity, "via": via, "snippet": snippet[:110]}

    def add_sql(sql_val, activity: str, via: str) -> None:
        text_, is_expr = _value(sql_val)
        if not text_:
            return
        if is_expr and text_.lstrip().startswith("@") and not text_.lstrip().startswith("@{"):
            info.warnings.append(f"{activity}: SQL built by an expression, skipped")
            return
        dyn: dict[str, str] = {}

        def tok(m: re.Match) -> str:
            t = f"__dyn{len(dyn)}__"
            dyn[t] = m.group(0)[2:-1]
            return t

        sql = _INTERP.sub(tok, text_)
        refs, fallback = extract_refs(sql, set())
        for r in refs:
            conf = "inferred" if fallback else r.confidence
            name = TOKEN_RE.sub(lambda m: "{" + dyn.get(m.group(0), "?") + "}", r.name)
            if TOKEN_RE.search(r.name):
                conf = "unresolved"
            info.hits.append(PipelineHit("writes" if r.mode == "write" else "reads", name,
                                         "table", conf, ev(activity, via, sql.strip().splitlines()[0] if sql.strip() else "")))

    for a in _iter_activities(acts):
        info.activities += 1
        name, atype = a.get("name", "?"), a.get("type")
        tp = a.get("typeProperties", {}) or {}

        if atype == "TridentNotebook":
            nid = tp.get("notebookId")
            hit = logical_ids.get(nid or "")
            if hit:
                info.hits.append(PipelineHit("calls", hit[1], "notebook", "parsed", ev(name, atype)))
            else:
                info.hits.append(PipelineHit("calls", f"notebook:{nid}", "notebook", "unresolved", ev(name, atype)))
        elif atype in {"InvokePipeline", "ExecutePipeline"}:
            pid = tp.get("pipelineId") or (tp.get("pipeline") or {}).get("referenceName")
            hit = logical_ids.get(pid or "")
            if hit:
                info.hits.append(PipelineHit("calls", hit[1], "pipeline", "parsed", ev(name, atype)))
            else:
                info.hits.append(PipelineHit("calls", f"pipeline:{pid}", "pipeline", "unresolved", ev(name, atype)))
        elif atype == "Copy":
            src, sink = tp.get("source") or {}, tp.get("sink") or {}
            for side, kind in ((src, "reads"), (sink, "writes")):
                tbl, dyn = _table_from_dataset(side.get("datasetSettings"))
                if tbl:
                    info.hits.append(PipelineHit(kind, tbl, "table", "unresolved" if dyn else "parsed",
                                                 ev(name, "Copy " + side.get("type", ""), tbl)))
            add_sql(src.get("sqlReaderQuery"), name, "Copy query")
        elif atype == "Lookup":
            src = tp.get("source") or {}
            add_sql(src.get("sqlReaderQuery"), name, "Lookup query")
            tbl, dyn = _table_from_dataset(tp.get("datasetSettings"))
            if tbl and not src.get("sqlReaderQuery"):
                info.hits.append(PipelineHit("reads", tbl, "table", "unresolved" if dyn else "parsed",
                                             ev(name, "Lookup table", tbl)))
        elif atype == "Script":
            for s in tp.get("scripts", []) or []:
                add_sql(s.get("text"), name, "Script")
    return info
