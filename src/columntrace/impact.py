"""Impact analysis over lineage.json.

Stored edges read like sentences (notebook --reads--> table). For impact analysis
we walk *data flow*: a table flows into notebooks/pipelines that read it, and
out of notebooks/pipelines that write it; semantic tables bind to the table
beneath them, so they sit downstream of it. A pipeline that merely calls a
notebook is reported as affected, but its other children are not (that would
over-claim).
"""

from __future__ import annotations

from collections import deque

CONF_ORDER = {"parsed": 0, "inferred": 1, "unresolved": 2}


class FlowGraph:
    def __init__(self, data: dict) -> None:
        self.nodes = {n["id"]: n for n in data["nodes"]}
        self.down: dict[str, list[tuple[str, str, str]]] = {}   # node -> [(next, confidence, via)]
        self.up: dict[str, list[tuple[str, str, str]]] = {}
        for e in data["edges"]:
            s, t, k, c = e["from"], e["to"], e["kind"], e["confidence"]
            if k in ("reads", "references"):   # t -> s
                self._add(t, s, c, k)
            elif k == "writes":         # s -> t
                self._add(s, t, c, "writes")
            elif k == "binds":          # t -> s
                self._add(t, s, c, "binds")
            elif k == "calls":          # callee -> caller (orchestration only)
                self.down.setdefault(t, []).append((s, c, "called-by"))

    def _add(self, a: str, b: str, conf: str, via: str) -> None:
        self.down.setdefault(a, []).append((b, conf, via))
        self.up.setdefault(b, []).append((a, conf, via))

    def resolve(self, query: str) -> str | None:
        if query in self.nodes:
            return query
        q = query.lower()
        for cand in (f"table:{q}", f"notebook:{query}", f"pipeline:{query}"):
            if cand in self.nodes:
                return cand
        hits = [i for i, n in self.nodes.items() if n["name"].lower() == q]
        if len(hits) == 1:
            return hits[0]
        hits = [i for i, n in self.nodes.items() if q in n["name"].lower()]
        return hits[0] if len(hits) == 1 else None

    def walk(self, start: str, direction: str = "down", max_depth: int = 20,
             min_confidence: str = "unresolved") -> list[dict]:
        """``min_confidence`` is the weakest edge allowed: parsed < inferred < unresolved."""
        adj = self.down if direction == "down" else self.up
        limit = CONF_ORDER[min_confidence]
        seen = {start: (0, "parsed", "")}
        out: list[dict] = []
        queue: deque[tuple[str, int, str, str]] = deque([(start, 0, "parsed", "")])
        while queue:
            node, depth, conf, via_in = queue.popleft()
            if depth >= max_depth:
                continue
            ntype = self.nodes[node]["type"]
            # a pipeline reached only because it calls a notebook does not fan out
            if ntype == "pipeline" and via_in == "called-by":
                continue
            for nxt, c, via in adj.get(node, []):
                if nxt in seen or CONF_ORDER[c] > limit:
                    continue
                worst = conf if CONF_ORDER[conf] >= CONF_ORDER[c] else c
                seen[nxt] = (depth + 1, worst, via)
                out.append({"id": nxt, "type": self.nodes[nxt]["type"], "name": self.nodes[nxt]["name"],
                            "depth": depth + 1, "confidence": worst, "via": via, "from": node})
                queue.append((nxt, depth + 1, worst, via))
        return out


def impact_report_markdown(fg: FlowGraph, start: str, confidence: str = "inferred") -> str:
    """``confidence`` caps the weakest edge used: 'parsed' is the strict view."""
    node = fg.nodes[start]
    down = [r for r in fg.walk(start, "down", min_confidence=confidence) if r["type"] != "unknown"]
    up = [r for r in fg.walk(start, "up", min_confidence=confidence) if r["type"] != "unknown"]
    counts = {c: len([r for r in fg.walk(start, "down", min_confidence=c) if r["type"] != "unknown"])
              for c in ("parsed", "inferred")}
    lines = [f"# Impact report: {node['name']}", "",
             f"Downstream size by strictness: {counts['parsed']} using parsed edges only, "
             f"{counts['inferred']} including inferred edges. Showing: **{confidence}**.", ""]

    def section(title: str, rows: list[dict]) -> None:
        lines.append(f"## {title} ({len(rows)})")
        if not rows:
            lines.append("_none found_\n")
            return
        lines.extend(["| Depth | Type | Name | Confidence | Reached via |", "| --- | --- | --- | --- | --- |"])
        for r in sorted(rows, key=lambda x: (x["depth"], x["type"], x["name"])):
            lines.append(f"| {r['depth']} | {r['type']} | {r['name']} | {r['confidence']} | {r['via']} |")
        lines.append("")

    section("Downstream: breaks if this changes", down)
    section("Upstream: feeds this", up)
    lines.append("_Confidence is the weakest edge on the path. `inferred` edges come from config "
                 "resolution or unqualified names; `unresolved` items are never silently dropped._")
    return "\n".join(lines)
