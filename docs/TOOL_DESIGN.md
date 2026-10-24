# Tool design: deriving MCP capabilities from user scenarios

## 1. Principles

### No catch-all reflection tools

The server does not expose tools of this shape:

```text
invoke_com_method(class_name, method_name, args)
execute_command(command_string)
```

Reasons: the permission boundary is too wide, arguments cannot be validated, results are hard to audit and
model tool choice becomes unstable.

### Medium granularity, batch first

Preferred:

```text
update_entity_properties(handles, properties)
update_entities_by_filter(filter, properties)
update_block_attributes(updates)
create_entities_batch(entities)
plot_layouts(layout_names, output_dir)
```

Such tools can be orchestrated by skills without splitting every object into many MCP round trips.

### Read before write

Every risky scenario follows:

```text
diagnose → query → dry_run preview → user confirmation → execute → read back and verify
```

### Filter on the CAD side

Large drawings (survey maps, GIS exports) contain tens of thousands of entities. Counting, selecting and bulk
property changes use COM selection sets with DXF group-code filters evaluated inside ZWCAD
(`count_entities`, `select_entities`, `update_entities_by_filter`) instead of iterating entities over COM.

### Never hang on dialogs

COM calls block while ZWCAD shows a modal dialog. Opening and plotting run inside a dialog guard: refuse to
start when a dialog is open, silence informational prompts via system variables, answer only known-safe dialogs
and report every dialog seen (`list_cad_dialogs`, `dismiss_cad_dialog`).

## 2. Scenarios and tools

### A: Quick inventory of an unknown drawing

Goal: understand layers, entities, blocks and layouts of a drawing received from a third party.

- diagnose_cad
- get_current_document
- count_entities (group_by layer/type) — or audit_drawing on small drawings
- list_layers
- list_block_references
- list_layouts

### B: Normalizing imported entities (layer 0, fixed colors, non-ByLayer linetypes)

- count_entities / select_entities
- ensure_layers
- update_entities_by_filter
- normalize_selected_entities
- update_entity_properties
- get_entity_details

### C: Parametric creation of simple geometry

Goal: avoid repetitive drawing of rectangles, hole patterns, text and dimensions.

- ensure_layers
- create_entities_batch
- get_entity_details

### D: Batch plotting of layouts

Goal: avoid switching layouts and naming PDFs by hand.

- get_current_document
- list_layouts
- plot_layouts
- create_batch_job / get_batch_job_status / verify_export_files

### E: Title block maintenance (attribute blocks)

- list_block_references
- get_block_attributes
- update_block_attributes
- save_document

## 3. Out of scope for now

- Arbitrary CAD command strings
- Deleting layers and forced purge
- XREF bind, detach and path repair
- Dynamic block parameter editing
- Boolean operations and complex 3D modelling
- Reflection-style writes to arbitrary object properties
- Automatic save over the original file

These are either high-risk or need clearer user scenarios and validation on real ZWCAD versions.
See `FORK_DESIGN.md` in the project notes for the planned architectural extensions.
