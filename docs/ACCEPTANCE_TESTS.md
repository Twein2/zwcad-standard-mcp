# Acceptance tests on a real ZWCAD installation

## A. Environment and connection

1. ZWCAD not running: `diagnose_cad` returns `CAD_CONNECTION_ERROR`.
2. ZWCAD running without an open DWG: `NO_ACTIVE_DOCUMENT`.
3. A different ProgID can be selected through the environment variable.
4. After closing and restarting ZWCAD the next call reconnects.
5. Several open drawings: `list_documents` and `activate_document` work.

## B. Safety policy

1. All write tools default to `dry_run=true`.
2. Write tools refuse to execute without `confirm=true`.
3. `delete_entities` requires `confirm=true` and `second_confirm=true`.
4. `save_document` requires `confirm=true` and a `file_path`.
5. Read-only documents can be inspected but not modified.
6. Exceeding the batch limit returns `BATCH_LIMIT_EXCEEDED`.
7. A modal ZWCAD dialog open before `open_document`/`plot_layouts` returns `MODAL_DIALOG_OPEN` with its title.

## C. Layers and entities

1. List layers with color, linetype, locked and frozen state.
2. Create a new layer; update an existing layer.
3. Read the user's pre-selection.
4. Query entities by type and layer.
5. Read details of several handles.
6. Bulk change of layer, color ByLayer, linetype ByLayer.
7. Move, copy, rotate, scale and mirror.
8. A bulk change is undone with a single Undo.
9. Deletion is previewed before it is confirmed.
10. `count_entities` with `group_by=["layer","type"]` on a drawing with 10,000+ entities returns within seconds.
11. `update_entities_by_filter` dry run reports the per-layer count; execution changes all matched entities and
    refuses a non-existent target layer.

## D. Drawing

Verify each type: line, circle, arc, lwpolyline, text, mtext, block, dimension_aligned, dimension_rotated,
dimension_radial, dimension_diametric. Every result returns a handle that can be queried again.

## E. Layouts and plotting

1. List Model and all paper layouts.
2. Activate a given layout.
3. Plot one PDF with a company PC3/PC5 configuration.
4. Plot several layouts with correct file names.
5. If one layout fails, the others continue and per-layout results are returned.
6. The original active layout is restored afterwards.
7. A missing or empty output file is reported as a failure.
8. Batch job on a folder where one drawing has unresolved references: the dialog is answered automatically
   (or reported) and the job does not hang.

## F. Blocks and title blocks

1. List block definitions.
2. Find attributed block references across layouts.
3. Return EffectiveName correctly (dynamic blocks need separate validation).
4. Read attribute TAGs and values.
5. Dry run shows old and new values.
6. Update existing TAGs and read back.
7. Non-existent TAGs appear in `unmatched_tags`.
8. Update several title blocks in one call.
9. Constant attributes, attributes inside XREFs and nested blocks are not modified.

## G. Large drawings

Use drawings with about 1,000, 10,000 and 100,000 entities and record:

- `count_entities` and `audit_drawing` duration
- `query_entities` duration
- reading 200 handles
- `update_entities_by_filter` duration for 2,000 entities
- COM memory use and stability
