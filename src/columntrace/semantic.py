"""Semantic model (TMDL) parser: which warehouse/lakehouse table backs each model table."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class SemanticTable:
    name: str
    source: str | None        # schema.table
    mode: str | None          # directLake | import | ...
    measures: int
    line: int


_TABLE = re.compile(r"^table\s+(?:'([^']+)'|(\S+))\s*$")
_ENTITY = re.compile(r"^\s*entityName:\s*(\S+)")
_SCHEMA = re.compile(r"^\s*schemaName:\s*(\S+)")
_MODE = re.compile(r"^\s*mode:\s*(\w+)")
_MEASURE = re.compile(r"^\s*measure\s")
_M_ITEM = re.compile(r'Item\s*=\s*"([^"]+)"')
_M_SCHEMA = re.compile(r'Schema\s*=\s*"([^"]+)"')
_LINEAGE = re.compile(r"^\s*sourceLineageTag:\s*\[([^\]]+)\]\.\[([^\]]+)\]")


def parse_tmdl_table(text: str) -> SemanticTable | None:
    name = None
    entity = schema = mode = None
    lineage = None
    measures = 0
    line_no = 1
    for i, line in enumerate(text.splitlines(), 1):
        if name is None:
            m = _TABLE.match(line)
            if m:
                name, line_no = m.group(1) or m.group(2), i
            continue
        if _MEASURE.match(line):
            measures += 1
        m = _ENTITY.match(line)
        if m:
            entity = m.group(1).strip("'")
        m = _SCHEMA.match(line)
        if m:
            schema = m.group(1).strip("'")
        m = _MODE.match(line)
        if m and mode is None:
            mode = m.group(1)
        m = _LINEAGE.match(line)
        if m and lineage is None:
            lineage = f"{m.group(1)}.{m.group(2)}"
    if name is None:
        return None
    source = f"{schema or 'dbo'}.{entity}" if entity else None
    if source is None:
        mi, ms = _M_ITEM.search(text), _M_SCHEMA.search(text)
        if mi:
            source = f"{ms.group(1) if ms else 'dbo'}.{mi.group(1)}"
    if source is None and lineage:
        source = lineage
    return SemanticTable(name, source.lower() if source else None, mode, measures, line_no)
