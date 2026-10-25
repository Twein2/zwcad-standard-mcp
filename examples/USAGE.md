# Agent usage examples

## 1. Quick drawing check

User:

> Tell me how many layers the active drawing has, what the main entities are and which blocks carry attributes. Do not change anything.

Calls:

1. `diagnose_cad`
2. `count_entities` with `{"filter": {"space": "all"}, "group_by": ["layer", "type"]}` (or `audit_drawing` on small drawings)
3. `list_block_references(scope="all_layouts", has_attributes=true)`

## 2. Normalize a survey map imported on layer 0

User:

> Move all points from layer 0 to MO_GEO_POINTS and set them to ByLayer. Preview first.

1. `ensure_layers` with `[{"name": "MO_GEO_POINTS", "color": 8}]`
2. `update_entities_by_filter` dry run:

```json
{
  "patch": {
    "filter": {"entity_types": ["POINT"], "layers": ["0"]},
    "layer": "MO_GEO_POINTS",
    "color": 256
  },
  "dry_run": true
}
```

3. After the user confirms: the same call with `"dry_run": false, "confirm": true`.
4. Verify with `count_entities` (`{"layers": ["0"]}` should return 0).

## 3. Normalize the user's selection

The user selects entities in ZWCAD and says:

> Put these on OUTLINE and make them ByLayer; preview first.

```json
{
  "target_layer": "OUTLINE",
  "set_color_bylayer": true,
  "set_linetype_bylayer": true,
  "dry_run": true
}
```

After confirmation call again with `dry_run=false, confirm=true`.

## 4. Update title block attributes

Find the blocks:

```json
{
  "scope": "all_layouts",
  "block_name": "A3_TITLE_BLOCK",
  "has_attributes": true,
  "limit": 50
}
```

Preview the update:

```json
{
  "updates": [
    {
      "handle": "3AF",
      "attributes": {
        "DESIGNER": "Jan Kowalski",
        "CHECKER": "Anna Nowak",
        "DATE": "2026-07-21"
      }
    }
  ],
  "dry_run": true
}
```

Execute after confirmation, then verify with `get_block_attributes`.

## 5. Plot several layouts

```json
{
  "layout_names": ["A01", "A02", "A03"],
  "output_dir": "D:\\Project\\PDF",
  "plot_configuration": "DWG To PDF.pc5",
  "extension": "pdf",
  "dry_run": true
}
```

If ZWCAD shows a dialog during plotting, the result lists it under `dialogs`; `list_cad_dialogs` shows dialogs
that are still open.

## 6. Plan a car route and check it with a swept path

Read a boundary and two vectors, plan a collision-free route with gear changes outside ZWCAD,
draw one polyline per gear and verify each with `vehicle_swept_path`. Full walkthrough, scripts and
results: [`vehicle_route/README.md`](vehicle_route/README.md).
