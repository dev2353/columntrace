"""Liveness status, staleness and variant families for notebooks and pipelines.

Signals are repo-only (call graph, schedules, last-change dates), so every status
is a *hint*, not proof that something runs. Statuses:

scheduled      the item has an enabled schedule in the repo
orchestrated   reachable by `calls` edges from a scheduled item
called         called by a pipeline/notebook, but not reachable from any scheduled item
entry-point    (notebooks) never called, but it calls other items: likely run by hand or externally
feeds-others   never called, but it writes tables that another item reads
dead-end       never called and nothing reads what it writes
uncalled       (pipelines only) never called and not scheduled
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import UTC, datetime

from .model import Graph

VARIANT_SUFFIX = re.compile(
    r"(_python|_spark|_pyspark|_v\d+(_tbd)?|_old|_bak|_backup|_copy|_test|_dev|_tmp|_deprecated|_last_month)+$",
    re.I)


def family_key(name: str) -> str:
    return VARIANT_SUFFIX.sub("", name).lower()


def _parse(date: str) -> datetime:
    return datetime.fromisoformat(date.replace("Z", "+00:00"))


def annotate(graph: Graph, dates: dict[str, dict] | None = None, now: datetime | None = None,
             stale_days: int = 365, bulk_threshold: int = 50) -> dict:
    """Write liveness/lastChange/family metadata onto nodes; return a summary."""
    now = now or datetime.now(UTC)
    items = [n for n in graph.nodes.values() if n.type in ("notebook", "pipeline")]

    callers: dict[str, set[str]] = defaultdict(set)
    callees: dict[str, set[str]] = defaultdict(set)
    readers: dict[str, set[str]] = defaultdict(set)    # table -> items that read/reference it
    writes: dict[str, set[str]] = defaultdict(set)     # item -> tables it writes
    for (src, dst, kind, conf) in graph.edges:
        if kind == "calls":
            callers[dst].add(src)
            callees[src].add(dst)
        elif kind in ("reads", "references") and conf != "unresolved":
            readers[dst].add(src)
        elif kind == "writes" and conf != "unresolved":
            writes[src].add(dst)

    scheduled = {n.id for n in items if (n.meta.get("schedules") or {}).get("enabled", 0) > 0}
    reachable: set[str] = set()
    stack = list(scheduled)
    while stack:
        cur = stack.pop()
        for nxt in callees.get(cur, ()):
            if nxt not in reachable and nxt in graph.nodes:
                reachable.add(nxt)
                stack.append(nxt)

    for n in items:
        if n.id in scheduled:
            status = "scheduled"
        elif n.id in reachable:
            status = "orchestrated"
        elif callers.get(n.id):
            status = "called"
        elif n.type == "pipeline":
            status = "uncalled"
        elif callees.get(n.id):
            status = "entry-point"
        else:
            feeds = any(readers.get(t, set()) - {n.id} for t in writes.get(n.id, ()))
            status = "feeds-others" if feeds else "dead-end"
        n.meta["liveness"] = status

    # variant families
    fams: dict[tuple[str, str], list] = defaultdict(list)
    for n in items:
        fams[(n.type, family_key(n.name))].append(n)

    # last-change dates + bulk-commit spread
    spread: dict[str, int] = defaultdict(int)
    matched = 0
    if dates:
        for n in items:
            d = dates.get(n.path or "")
            if d:
                spread[d["commit"]] += 1
        for n in items:
            d = dates.get(n.path or "")
            if not d:
                continue
            matched += 1
            n.meta["lastChange"] = d["lastChange"]
            n.meta["commit"] = d["commit"]
            n.meta["commitSpread"] = spread[d["commit"]]
            if spread[d["commit"]] >= bulk_threshold:
                n.meta["bulkCommit"] = True
            age = (now - _parse(d["lastChange"])).days
            n.meta["ageDays"] = age
            n.meta["stale"] = age > stale_days

    for (_t, key), members in fams.items():
        if len(members) < 2:
            continue
        dated = [m for m in members if m.meta.get("lastChange")]
        newest = max(dated, key=lambda m: _parse(m.meta["lastChange"])).id if dated else None
        for m in members:
            m.meta["family"] = key
            m.meta["familyMembers"] = len(members)
            if newest:
                m.meta["newestInFamily"] = (m.id == newest)

    counts: dict[str, int] = defaultdict(int)
    for n in items:
        counts[f"{n.type}:{n.meta['liveness']}"] += 1
    summary = {
        "liveness": dict(sorted(counts.items())),
        "families": sum(1 for v in fams.values() if len(v) > 1),
        "datesMatched": matched, "datesAvailable": len(dates or {}),
        "itemsWithoutDate": (len(items) - matched) if dates else None,
        "stale": sum(1 for n in items if n.meta.get("stale")),
        "staleDays": stale_days,
    }
    graph.annotations["liveness"] = summary
    return summary
