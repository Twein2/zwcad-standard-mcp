"""Helpers for CAD-side filtered selection sets (pure Python, no COM imports)."""

from __future__ import annotations

import fnmatch
from typing import Any

from zwcad_standard_mcp.models import EntityFilter

# DXF group codes used by AutoCAD-compatible SelectionSet filters.
GROUP_TYPE = 0
GROUP_BLOCK_NAME = 2
GROUP_LAYER = 8
GROUP_COLOR = 62
GROUP_LAYOUT = 410

_OBJECT_NAME_TO_DXF = {
    "acdbline": "LINE",
    "acdbcircle": "CIRCLE",
    "acdbarc": "ARC",
    "acdbpolyline": "LWPOLYLINE",
    "acdb2dpolyline": "POLYLINE",
    "acdb3dpolyline": "POLYLINE",
    "acdbtext": "TEXT",
    "acdbmtext": "MTEXT",
    "acdbblockreference": "INSERT",
    "acdbpoint": "POINT",
    "acdbhatch": "HATCH",
    "acdbrasterimage": "IMAGE",
    "acdbellipse": "ELLIPSE",
    "acdbspline": "SPLINE",
    "acdbattributedefinition": "ATTDEF",
    "acdbaligneddimension": "DIMENSION",
    "acdbrotateddimension": "DIMENSION",
    "acdbradialdimension": "DIMENSION",
    "acdbdiametricdimension": "DIMENSION",
    "acdb3pointangulardimension": "DIMENSION",
}


# DXF entity names probed one CAD-side selection each by count_entities(group_by=["type"]).
# Anything not listed is reported as OTHER (count minus the sum of the probed types).
COMMON_DXF_TYPES = (
    "POINT", "LINE", "LWPOLYLINE", "POLYLINE", "ARC", "CIRCLE", "ELLIPSE", "SPLINE",
    "TEXT", "MTEXT", "INSERT", "ATTDEF", "DIMENSION", "LEADER", "MLEADER", "HATCH",
    "SOLID", "WIPEOUT", "IMAGE", "OLE2FRAME", "XLINE", "RAY", "3DFACE", "REGION",
    "TABLE", "VIEWPORT", "TOLERANCE", "MLINE", "3DSOLID", "PDFUNDERLAY",
)


def build_filter(spec: EntityFilter) -> tuple[list[int], list[Any]]:
    """Return (group codes, values) for SelectionSet.Select(acSelectionSetAll, ...).

    Lists are joined with commas, which ZWCAD/AutoCAD treat as OR inside one group code.
    """
    codes: list[int] = []
    values: list[Any] = []
    if spec.entity_types:
        codes.append(GROUP_TYPE)
        values.append(",".join(spec.entity_types))
    if spec.layers:
        codes.append(GROUP_LAYER)
        values.append(",".join(spec.layers))
    if spec.color is not None:
        codes.append(GROUP_COLOR)
        values.append(int(spec.color))
    if spec.block_names:
        codes.append(GROUP_BLOCK_NAME)
        values.append(",".join(spec.block_names))
    if spec.space == "model":
        codes.append(GROUP_LAYOUT)
        values.append("Model")
    elif spec.space == "paper":
        codes.append(GROUP_LAYOUT)
        values.append("~Model")
    return codes, values


def escape_layer_name(name: str) -> str:
    """Escape CAD wildcard characters so a real layer name matches only itself in a selection filter."""
    return "".join("`" + ch if ch in "#@.*?~[]-," else ch for ch in name)


def dxf_name(object_name: str) -> str:
    """Map a COM ObjectName (AcDbLine) to its DXF entity name (LINE)."""
    lowered = object_name.lower()
    if lowered in _OBJECT_NAME_TO_DXF:
        return _OBJECT_NAME_TO_DXF[lowered]
    return lowered[4:].upper() if lowered.startswith("acdb") else object_name.upper()


def _wild(value: str, patterns: list[str]) -> bool:
    """Approximate CAD wildcard matching (used by the fake adapter and for verification)."""
    value_u = value.upper()
    for pattern in patterns:
        for part in pattern.split(","):
            part = part.strip().upper()
            negate = part.startswith("~")
            if negate:
                part = part[1:]
            matched = fnmatch.fnmatchcase(value_u, part.replace("#", "[0-9]"))
            if matched != negate:
                return True
    return False


def matches(spec: EntityFilter, *, object_name: str, layer: str, layout: str, color: int | None = None,
            block_name: str | None = None) -> bool:
    """Python-side evaluation of an EntityFilter (fake adapter / tests)."""
    if spec.entity_types and dxf_name(object_name) not in spec.entity_types:
        return False
    if spec.layers and not _wild(layer, spec.layers):
        return False
    if spec.color is not None and (color if color is not None else 256) != spec.color:
        return False
    if spec.block_names and not (block_name and _wild(block_name, spec.block_names)):
        return False
    if spec.space == "model" and layout != "Model":
        return False
    if spec.space == "paper" and layout == "Model":
        return False
    return True
