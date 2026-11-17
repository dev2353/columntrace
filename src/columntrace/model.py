"""Graph contract (lineage.json, schemaVersion 1).

Edge semantics (the stored edge reads like a sentence):
    notebook  --reads-->   table
    notebook  --writes-->  table
    pipeline  --calls-->   notebook | pipeline
    pipeline  --reads/writes--> table      (Copy, Lookup, Script activities)
    semantic_table --binds--> table
Data flow runs the other way for reads and binds (table -> consumer); see impact.py.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime

SCHEMA_VERSION = 1

NODE_TYPES = {
    "table", "notebook", "pipeline", "semantic_table", "unknown",
}
EDGE_KINDS = {"reads", "writes", "calls", "binds", "references"}
# "references": the notebook mentions the table as a string literal but we could not
# see how it is used (read or write unknown). Always confidence "inferred".
CONFIDENCES = ("parsed", "inferred", "unresolved")  # best to worst


@dataclass
class Node:
    id: str
    type: str
    name: str
    path: str | None = None
    item_id: str | None = None
    meta: dict = field(default_factory=dict)


@dataclass
class Edge:
    source: str
    target: str
    kind: str
    confidence: str
    evidence: list[dict] = field(default_factory=list)


class Graph:
    def __init__(self) -> None:
        self.nodes: dict[str, Node] = {}
        self.edges: dict[tuple[str, str, str, str], Edge] = {}
        self.warnings: list[dict] = []
        self.annotations: dict = {}     # summaries from later passes (e.g. liveness)
        # row-level table flows from config tables: (source table, target table, job) -> flow
        self.flows: dict[tuple, dict] = {}
        self.config_tables: list = []   # resolvers that need a second (column) pass
        # column lineage (optional): edges keyed by (job, from, from col, to, to col)
        self.column_edges: dict[tuple, dict] = {}
        self.column_tables: dict[str, list[str]] = {}
        self.column_stats: dict | None = None
        self.inputs: dict = {}                      # how current each input is (repo commit, export times)
        self.generated_at: datetime | None = None   # fixed for reproducible output, else the save time

    def add_node(self, node: Node) -> Node:
        existing = self.nodes.get(node.id)
        if existing:
            for k, v in node.meta.items():
                existing.meta.setdefault(k, v)
            if node.path and not existing.path:
                existing.path = node.path
            if node.item_id and not existing.item_id:
                existing.item_id = node.item_id
            return existing
        self.nodes[node.id] = node
        return node

    def add_edge(self, source: str, target: str, kind: str, confidence: str,
                 evidence: dict | None = None) -> None:
        assert kind in EDGE_KINDS and confidence in CONFIDENCES, (kind, confidence)
        # one edge per (source, target, kind): keep the best confidence, merge evidence
        rank = CONFIDENCES.index(confidence)
        for other in CONFIDENCES:
            if other == confidence:
                continue
            okey = (source, target, kind, other)
            existing = self.edges.get(okey)
            if existing is None:
                continue
            if CONFIDENCES.index(other) < rank:
                if evidence and len(existing.evidence) < 3 and evidence not in existing.evidence:
                    existing.evidence.append(evidence)
                return
            del self.edges[okey]
            evidence_carry = existing.evidence
            new_key = (source, target, kind, confidence)
            self.edges[new_key] = Edge(source, target, kind, confidence, evidence_carry[:3])
        key = (source, target, kind, confidence)
        edge = self.edges.get(key)
        if edge is None:
            edge = self.edges[key] = Edge(source, target, kind, confidence)
        if evidence and len(edge.evidence) < 3 and evidence not in edge.evidence:
            edge.evidence.append(evidence)

    def warn(self, item: str, message: str) -> None:
        self.warnings.append({"item": item, "message": message})

    def stats(self) -> dict:
        by_conf: dict[str, int] = {}
        by_kind: dict[str, int] = {}
        for e in self.edges.values():
            by_conf[e.confidence] = by_conf.get(e.confidence, 0) + 1
            by_kind[e.kind] = by_kind.get(e.kind, 0) + 1
        by_type: dict[str, int] = {}
        for n in self.nodes.values():
            by_type[n.type] = by_type.get(n.type, 0) + 1
        return {
            "nodes": len(self.nodes), "edges": len(self.edges),
            "nodesByType": by_type, "edgesByKind": by_kind,
            "edgesByConfidence": by_conf, "warnings": len(self.warnings),
        }

    def add_flow(self, source: str, target: str, job: str, confidence: str, evidence: dict | None = None) -> None:
        """A table-to-table flow known from one config row (no many-to-many guessing)."""
        key = (source, target, job)
        if key not in self.flows:
            self.flows[key] = {"from": source, "to": target, "job": job, "confidence": confidence,
                               "evidence": evidence or {}}

    def add_column_edge(self, job: str, source: str, source_col: str, target: str, target_col: str,
                        transform: str, expr: str, conf: str, line: int) -> None:
        key = (job, source, source_col, target, target_col)
        old = self.column_edges.get(key)
        if old and (old["confidence"] == "parsed" or conf == "inferred"):
            return
        self.column_edges[key] = {
            "from": source, "fromColumn": source_col, "to": target, "toColumn": target_col,
            "transform": transform, "expr": expr, "confidence": conf, "job": job, "line": line,
        }

    def columns_dict(self, include_evidence: bool = True) -> dict | None:
        if self.column_stats is None:
            return None
        edges = sorted(self.column_edges.values(),
                       key=lambda e: (e["to"], e["toColumn"], e["from"], e["fromColumn"], e["job"]))
        if not include_evidence:
            edges = [{k: v for k, v in e.items() if k not in ("expr", "line")} for e in edges]
        return {"schemaVersion": 1, "tables": self.column_tables, "edges": edges, "stats": self.column_stats}

    def to_dict(self, include_evidence: bool = True) -> dict:
        def edge_dict(e: Edge) -> dict:
            d = {"from": e.source, "to": e.target, "kind": e.kind,
                 "confidence": e.confidence}
            if include_evidence:
                d["evidence"] = e.evidence
            return d

        doc = {
            "meta": {
                "schemaVersion": SCHEMA_VERSION,
                "generatedAt": (self.generated_at or datetime.now(UTC)).isoformat(timespec="seconds"),
                "stats": self.stats(),
                "annotations": self.annotations,
                **({"inputs": self.inputs} if self.inputs else {}),
            },
            "nodes": [
                {"id": n.id, "type": n.type, "name": n.name, "path": n.path,
                 "itemId": n.item_id, "meta": n.meta}
                for n in sorted(self.nodes.values(), key=lambda x: x.id)
            ],
            "edges": [edge_dict(e) for e in sorted(
                self.edges.values(), key=lambda x: (x.source, x.target, x.kind))],
            "warnings": self.warnings,
        }
        if self.flows:
            flows = sorted(self.flows.values(), key=lambda f: (f["to"], f["from"], f["job"]))
            doc["flows"] = flows if include_evidence else [{k: v for k, v in f.items() if k != "evidence"} for f in flows]
        cols = self.columns_dict(include_evidence)
        if cols is not None:
            doc["columns"] = cols
        return doc

    def save(self, path: str, include_evidence: bool = True) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(include_evidence), f, indent=1)


def table_id(name: str) -> str:
    return f"table:{name.lower()}"


def unresolved_id(template: str) -> str:
    return f"unresolved:{template}"
