from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


Scope = Literal["model", "paper", "all_layouts"]


class LayerSpec(BaseModel):
    name: str = Field(min_length=1, description="Layer name")
    color: int | None = Field(default=None, ge=0, le=256)
    linetype: str | None = None
    on: bool | None = None
    locked: bool | None = None
    frozen: bool | None = None


class EntityCreateSpec(BaseModel):
    entity_type: Literal[
        "line",
        "circle",
        "arc",
        "lwpolyline",
        "text",
        "mtext",
        "block",
        "dimension_aligned",
        "dimension_rotated",
        "dimension_radial",
        "dimension_diametric",
    ]
    params: dict[str, Any]
    layer: str = "0"


class EntityFilter(BaseModel):
    """CAD-side selection filter (evaluated by ZWCAD, not by iterating in Python).

    Values follow DXF conventions: entity types are DXF names (LINE, LWPOLYLINE, TEXT,
    MTEXT, INSERT, POINT, HATCH, DIMENSION ...); layer names accept CAD wildcards
    (``*``, ``?``, ``#``, ``~`` and comma-separated lists).
    """

    entity_types: list[str] | None = Field(default=None, description="DXF entity names, e.g. ['POINT', 'TEXT']")
    layers: list[str] | None = Field(default=None, description="Layer names or wildcard patterns")
    color: int | None = Field(default=None, ge=0, le=256, description="ACI color; 256 = ByLayer, 0 = ByBlock")
    block_names: list[str] | None = Field(default=None, description="Block names for INSERT entities")
    space: Literal["model", "paper", "all"] = "model"

    @field_validator("entity_types")
    @classmethod
    def upper_types(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        cleaned = [item.strip().upper() for item in value if item.strip()]
        return cleaned or None


class FilteredPropertyPatch(BaseModel):
    """Bulk property change for every entity matching ``filter``."""

    filter: EntityFilter
    layer: str | None = None
    color: int | None = Field(default=None, ge=0, le=256)
    linetype: str | None = None
    linetype_scale: float | None = Field(default=None, gt=0)
    lineweight: int | None = None


class EntityQuerySpec(BaseModel):
    scope: Scope = "model"
    entity_type: str | None = None
    layer: str | None = None
    text_contains: str | None = None
    block_name: str | None = None
    limit: int = Field(default=200, ge=1, le=100000)


class EntityPropertyPatch(BaseModel):
    handles: list[str] = Field(min_length=1)
    layer: str | None = None
    color: int | None = Field(default=None, ge=0, le=256)
    linetype: str | None = None
    linetype_scale: float | None = Field(default=None, gt=0)
    lineweight: int | None = None
    visible: bool | None = None

    @field_validator("handles")
    @classmethod
    def unique_handles(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(handle.strip() for handle in value if handle.strip()))


class BlockAttributePatch(BaseModel):
    handle: str = Field(min_length=1)
    attributes: dict[str, str] = Field(min_length=1)


class PlotScopeRequest(BaseModel):
    """Request to preview the final plot area for a given scope."""

    scope_type: Literal["display", "extents", "limits", "view", "window", "layout"] | None = Field(
        default=None,
        description="Plot scope type to preview. If omitted, the active layout's current PlotType is used.",
    )
    layout_name: str | None = Field(
        default=None,
        description="Layout to inspect. Defaults to the active layout.",
    )
    window_lower_left: list[float] | None = Field(
        default=None,
        description="Lower-left corner of the plot window (only for scope_type='window').",
    )
    window_upper_right: list[float] | None = Field(
        default=None,
        description="Upper-right corner of the plot window (only for scope_type='window').",
    )
    selected_handles: list[str] | None = Field(
        default=None,
        description="Handles to use when previewing a 'selected' scope.",
    )

    @field_validator("window_lower_left", "window_upper_right")
    @classmethod
    def _window_corner_2d(cls, value: list[float] | None) -> list[float] | None:
        if value is None:
            return value
        if len(value) < 2:
            raise ValueError("Window corner must have at least [x, y].")
        return [float(v) for v in value[:3]]


class TransformRequest(BaseModel):
    action: Literal["move", "copy", "rotate", "scale", "mirror"]
    handles: list[str] = Field(min_length=1)
    params: dict[str, Any]

    @field_validator("handles")
    @classmethod
    def unique_handles(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(handle.strip() for handle in value if handle.strip()))



class LayerMapRule(BaseModel):
    """Map every source layer matching ``source`` (CAD wildcards, comma lists) to ``target``."""

    source: str = Field(min_length=1, description="Source layer name or wildcard, e.g. 'EGB_*' or 'OT*,KR*'")
    target: str = Field(min_length=1, description="Target (office standard) layer name")


class LayerStandard(BaseModel):
    """Office layer standard: target layer definitions plus mapping rules (first matching rule wins)."""

    layers: list[LayerSpec] = Field(default_factory=list)
    rules: list[LayerMapRule] = Field(default_factory=list)


class LayerMapRequest(BaseModel):
    standard: LayerStandard | None = Field(default=None, description="Inline standard")
    standard_path: str | None = Field(default=None, description="Path to a JSON file with the same structure")
    purge_sources: bool = Field(default=True, description="Delete source layers once they are empty")
    purge_unused: bool = Field(default=False, description="Also try to delete every other empty layer")
    time_budget_s: float = Field(default=40.0, gt=1, le=50, description="Stop after this time (whole call); run again to continue")
    verbose: bool = Field(default=False, description="Return every step and layer name instead of a summary")


class TextUpdateRequest(BaseModel):
    """Bulk edit of TEXT/MTEXT content selected by a CAD-side filter."""

    filter: EntityFilter = Field(default_factory=lambda: EntityFilter(entity_types=["TEXT", "MTEXT"]))
    operation: Literal["set", "replace", "strip_formatting", "repair_encoding"]
    value: str | None = Field(default=None, description="New text for operation=set")
    find: str | None = Field(default=None, description="Text or regex to find for operation=replace")
    replacement: str = Field(default="", description="Replacement for operation=replace")
    regex: bool = False
    case_sensitive: bool = True
    wrong_codepage: str = Field(default="cp1250", description="Codepage the UTF-8 bytes were wrongly read with")
    time_budget_s: float = Field(default=45.0, gt=1, le=55)


class TextStyleSpec(BaseModel):
    """Text style with a TrueType font (avoids '?' for Polish characters, en dash and multiplication sign)."""

    name: str = Field(min_length=1)
    font: str = Field(default="Arial", description="TrueType family name, e.g. 'Arial', 'Arial Narrow', 'ISOCPEUR'")
    bold: bool = False
    italic: bool = False
    height: float = Field(default=0.0, ge=0, description="0 = height set per text")
    width_factor: float = Field(default=1.0, gt=0)
    oblique_deg: float = 0.0


class DimStyleSpec(BaseModel):
    """Dimension style created from DIM* system variables (CopyFrom the document)."""

    name: str = Field(min_length=1)
    text_style: str | None = Field(default=None, description="DIMTXSTY")
    text_height: float | None = Field(default=None, gt=0, description="DIMTXT")
    arrow_size: float | None = Field(default=None, ge=0, description="DIMASZ")
    arrow_block: str | None = Field(default=None, description="DIMBLK, e.g. '_ARCHTICK', '_OBLIQUE', '' for closed filled")
    overall_scale: float | None = Field(default=None, gt=0, description="DIMSCALE")
    linear_scale: float | None = Field(default=None, gt=0, description="DIMLFAC, e.g. 0.01 to show metres in a cm drawing")
    decimals: int | None = Field(default=None, ge=0, le=8, description="DIMDEC")
    decimal_separator: str | None = Field(default=None, max_length=1, description="DIMDSEP, e.g. ','")
    text_gap: float | None = Field(default=None, description="DIMGAP")
    ext_line_offset: float | None = Field(default=None, description="DIMEXO")
    ext_line_extension: float | None = Field(default=None, description="DIMEXE")
    extra: dict[str, float | int | str] = Field(default_factory=dict, description="Other DIM* variables")
    set_current: bool = False


class HatchRequest(BaseModel):
    outer_handle: str = Field(min_length=1, description="Closed boundary (polyline, circle, region)")
    inner_handles: list[str] = Field(default_factory=list, description="Islands, e.g. building outlines")
    pattern: str = Field(default="SOLID", description="Predefined pattern name: SOLID, ANSI31, ANSI37, AR-CONC ...")
    scale: float = Field(default=1.0, gt=0)
    angle_deg: float = 0.0
    layer: str | None = None
    color: int | None = Field(default=None, ge=0, le=256)
    associative: bool = True


class AreaLimits(BaseModel):
    min_pbc_percent: float | None = Field(default=None, description="Minimum biologically active area, % of plot")
    max_building_percent: float | None = Field(default=None, description="Maximum building footprint, % of plot")
    min_intensity: float | None = None
    max_intensity: float | None = None


class BoundaryDistanceRequest(BaseModel):
    """Distances from a building outline to every plot boundary segment (WT §12 checks)."""

    building_handle: str = Field(min_length=1)
    plot_handle: str = Field(min_length=1)
    min_distance_m: float | None = Field(default=None, ge=0, description="Required minimum, e.g. 4.0 (wall with windows)")
    boundary_min_m: dict[int, float] = Field(
        default_factory=dict, description="Per boundary index overrides, e.g. {2: 3.0} for a wall without windows"
    )
    report_within_m: float | None = Field(default=None, gt=0, description="Only report boundaries closer than this")
    create_dimensions: bool = False
    layer: str | None = Field(default=None, description="Layer for created dimensions (must exist)")
    units: Literal["auto", "mm", "cm", "m"] = "auto"


class AreaTakeoffRequest(BaseModel):
    """Site balance (bilans terenu) from closed outlines; areas are assumed not to overlap."""

    plot_handle: str = Field(min_length=1)
    building_handles: list[str] = Field(default_factory=list)
    paved_handles: list[str] = Field(default_factory=list)
    other_handles: dict[str, list[str]] = Field(default_factory=dict, description="Extra named groups, e.g. {'water': [...]}")
    gross_floor_area_m2: float | None = Field(default=None, ge=0, description="For building intensity (MPZP 'intensywność')")
    units: Literal["auto", "mm", "cm", "m"] = "auto"
    limits: AreaLimits = Field(default_factory=AreaLimits)


# --- LISP channel tools -------------------------------------------------------------------

Point2D = list[float]


class AreaFieldPair(BaseModel):
    text_handle: str = Field(min_length=1, description="MTEXT (or TEXT) that receives the area")
    source_handle: str = Field(min_length=1, description="Closed polyline, hatch, circle, region, ellipse or spline")


class AreaFieldRequest(BaseModel):
    """Live area fields in room / zone labels (port of the WklejPole / WklejPoleHatch LISP routines)."""

    pairs: list[AreaFieldPair] = Field(min_length=1)
    decimals: int = Field(default=1, ge=0, le=8)
    decimal_separator: Literal[".", ","] | None = Field(default=None, description="None keeps ZWCAD's default")
    with_layer: bool = Field(default=False, description="'<layer> - <area>' like WklejPoleHatch")
    keep_text: bool = Field(default=True, description="Keep the label text in front of the old area value")
    units: Literal["auto", "mm", "cm", "m"] = Field(default="auto", description="Drawing units; auto = INSUNITS")
    suffix: str | None = Field(default=None, description="Text after the field; default superscript m² in MTEXT")


class SunRulerRequest(BaseModel):
    """Equinox shadow ruler for the 'czas nasłonecznienia' check (port of LinijkaSlonca)."""

    base_point: Point2D = Field(min_length=2, max_length=3,
                                description="Evaluated point, e.g. a window (WCS); the lines point towards the sun")
    equinox: Literal["spring", "autumn"] = Field(default="spring", description="spring = 21 March, autumn = 23 September")
    obstruction_height_m: float = Field(default=15.0, gt=0, description="Real height of the obstructing building, m")
    terrain_height_m: float = Field(default=0.0, description="Height of the evaluated point above terrain (e.g. window), m; H = obstruction - this")
    latitude: float = Field(default=51.0, ge=-66, le=66)
    longitude: float = Field(default=17.0, ge=-180, le=180)
    start_direction_point: Point2D | None = Field(default=None, description="Point giving the shadow direction where exposure starts")
    end_direction_point: Point2D | None = Field(default=None, description="Point giving the shadow direction where exposure ends")
    start_time: float | None = Field(default=None, ge=7, le=17, description="Used when no start_direction_point (hours, e.g. 8.5)")
    end_time: float | None = Field(default=None, ge=7, le=17, description="Used when no end_direction_point")
    description_point: Point2D | None = Field(default=None, description="Insert the result description here (layer a_naslonecznienie_opis)")
    room_number: str | None = Field(default=None, description="Room label 'lokal N: 8:00-12:30 (4:30)'")
    room_label_point: Point2D | None = None
    hour_text_height: float | None = Field(default=None, gt=0, description="Drawing units; default 0.25 m")
    description_text_height: float | None = Field(default=None, gt=0, description="Drawing units; default 0.5 m")
    units: Literal["auto", "mm", "cm", "m"] = Field(default="auto", description="Drawing units; auto = INSUNITS (unitless = m)")
    make_block: bool = Field(default=True, description="Group the ruler into one block (as the original command)")


class VehicleOverrides(BaseModel):
    """Vehicle dimensions in mm (defaults: Porsche Cayenne E3). Keys as in CAYENNE.LSP."""

    model_config = {"extra": "forbid"}

    NAME: str | None = None
    L: float | None = Field(default=None, gt=0, description="Wheelbase")
    FO: float | None = Field(default=None, ge=0, description="Front overhang")
    RO: float | None = Field(default=None, ge=0, description="Rear overhang")
    W: float | None = Field(default=None, gt=0, description="Body width without mirrors")
    WM: float | None = Field(default=None, gt=0, description="Width with mirrors")
    MX: float | None = Field(default=None, ge=0, description="Mirror start behind the front axle")
    MD: float | None = Field(default=None, ge=0, description="Mirror length along the car")
    TRF: float | None = Field(default=None, gt=0, description="Front track (tyre centre to tyre centre)")
    TRR: float | None = Field(default=None, gt=0, description="Rear track")
    TW: float | None = Field(default=None, gt=0, description="Tyre width")
    TD: float | None = Field(default=None, gt=0, description="Tyre diameter")
    TC: float | None = Field(default=None, gt=0, description="Turning circle, kerb to kerb")


class SweptPathRequest(BaseModel):
    """Vehicle swept path (obwiednia ruchu) along a path (port of CAYENNE.LSP v2.2)."""

    path_handle: str = Field(min_length=1, description="Polyline / line / arc / spline: front axle centre (forward) or rear axle centre (reverse)")
    direction: Literal["forward", "reverse"] = "forward"
    against_drawing_direction: bool = Field(default=False, description="Drive from the end of the path to its start")
    step_mm: float = Field(default=50.0, gt=0, le=1000)
    outline_interval_mm: float = Field(default=0.0, ge=0, description="Draw the car outline every N mm (0 = never)")
    auto_fillet: bool = Field(default=True, description="Round sharp LWPOLYLINE corners with the minimum radius first (new path on SP_SCIEZKA)")
    units: Literal["auto", "mm", "cm", "m"] = "auto"
    vehicle: VehicleOverrides = Field(default_factory=VehicleOverrides)


class RoundPathRequest(BaseModel):
    """Round the corners of a polyline route with the vehicle's minimum radius (port of SCIEZKA)."""

    path_handle: str = Field(min_length=1)
    direction: Literal["forward", "reverse"] = "forward"
    radius_cm: float | None = Field(default=None, gt=0, description="Default: minimum radius for the direction")
    units: Literal["auto", "mm", "cm", "m"] = "auto"
    vehicle: VehicleOverrides = Field(default_factory=VehicleOverrides)


class OffsetRequest(BaseModel):
    handles: list[str] = Field(min_length=1)
    distance: float = Field(description="Drawing units; the sign picks the side (as COM Offset)")
    both_sides: bool = Field(default=False, description="Also offset by -distance")
    layer: str | None = Field(default=None, description="Put the new entities on this (existing) layer")
