# ZWCAD automation API inventory (Standard edition)

Sources: ZWCAD developer documentation (https://www.zwsoft.com/support/zwcad-devdoc): VBA/COM Reference 2025
(112 objects), LISP Reference 2024 (536 entries incl. DCL), LISP Guide 2026, COM Automation Developing Guide.
Live checks: ZWCAD Standard 2024 (Polish Windows, cp1250), 2026-10-25.

ZWCAD Standard exposes **COM/ActiveX** (out-of-process, used by this server) and **AutoLISP/Visual LISP**
(in-process, used through the LISP channel). ZRX (C++) and ZRX.NET need ZWCAD Professional. The COM type
library is `ZWCAD` (interfaces `ZcadApplication`, `ZcadDocument`, `ZcadLine`, ...; constants `zc*` instead of
`ac*`); ProgIDs `ZWCAD.Application` and `ZWCAD.Application.<year>`. The object model mirrors the AutoCAD
ActiveX model.

Legend: **used** = called by the server today; **candidate** = useful, not used yet (proposed tool or fix).

## 1. COM object model: what the server uses and what it does not

| Object | Used | Candidates (not used yet) | Why it matters |
|---|---|---|---|
| Application | `ActiveDocument`, `Documents`, `Version`, `HWND`, `ZoomExtents`, `ZoomWindow` | `GetZcadState().IsQuiescent`, `ZoomPrevious`, `ZoomScaled`, `ZoomCenter`, events `BeginModal`/`EndModal`, `EndCommand`, `BeginPlot`/`EndPlot` | Idle check before `SendCommand` and plotting (replaces polling `CMDACTIVE`); modal events instead of window enumeration (#13, #37) |
| Document | `SendCommand`, `GetVariable`/`SetVariable`, `SelectionSets`, `HandleToObject`, `StartUndoMark`/`EndUndoMark`, `SaveAs`, `Plot`, `Layouts`, styles | `PurgeAll`, `AuditInfo(FixErr)`, `Regen(zcAllViewports)`, `SummaryInfo`, `Dictionaries`, `Groups`, `Views`, `CopyObjects`, events `ObjectAdded/Modified/Erased`, `EndCommand` | Purge/audit tool; `Regen` after style changes (#39 display); `SummaryInfo` custom keys for the project sheet (title blocks, step 3) |
| Utility | — | `TranslateCoordinates`, `AngleFromXAxis`, `PolarPoint`, `RealToString`/`DistanceToReal`, `GetRemoteFile` | Unit-aware formatting (dimension texts, area tables) without own code |
| SelectionSet | `Select` (filters), `Clear`, `Delete`, `Count`, `Item` | `SelectByPolygon` (window/crossing polygon), `SelectAtPoint`, `SelectOnScreen` (user picks, Claude reads), `Erase`, `Highlight` | "Everything inside this plot" queries; interactive pick on request |
| ModelSpace / Block | `AddLine/Circle/Arc/LightWeightPolyline/Text/MText/Hatch/Dim*/InsertBlock/MLeader` | `AddTable`, `AddRegion`, `AddPoint`, `AddSpline`, `AddLeader`, `AddMLine`, `AddRaster`, `AttachExternalReference`, `Bind`/`Detach`/`Reload`/`Unload` | Area tables on the drawing (site balance), regions for Boolean area takeoff, xref management (#16) |
| Region | `Area` | `Boolean(zcUnion/zcIntersection/zcSubtraction)`, `Explode` | Exact areas of overlapping zones (plot − building − paving) |
| Table | (`Rows`, `Columns` read) | 97 methods: `SetText`, `SetCellTextStyle`, `MergeCells`, `SetColumnWidth`, `InsertRows`, `SetFieldId`, ... | Generated area/room tables with live fields |
| LWPolyline | `Coordinates`, `Closed`, `Area`, `Offset` | `GetBulge`/`SetBulge`, `AddVertex`, `IntersectWith`, `Explode` | Arc-aware geometry in Python (boundary distances, swept paths without LISP) |
| Hatch | `AppendOuterLoop/InnerLoop`, `Evaluate`, `PatternScale/Angle`, `Area` | `SetPattern`, `GetLoopAt`, `InsertLoopAt` | Read hatch boundaries back; change pattern of existing hatches |
| SortentsTable | — | `MoveToBottom`/`MoveToTop`/`MoveAbove`/`SwapOrder` | Draw order (hatches under lines) without the `DRAWORDER` command |
| Plot | `PlotToFile` | `SetLayoutsToPlot`, `StartBatchMode(entryCount)`, `PlotToDevice`, `DisplayPlotPreview` | Batch plotting in one call (#37 step 2) |
| Layout / PlotConfiguration | read/write of device, media, scale, window | `GetPaperSize`, `GetPaperMargins`, `SetCustomScale`/`GetCustomScale`, `GetPlotStyleTableNames`, `RefreshPlotDeviceInfo` | Fit-check of a sheet before plotting |
| LayerStateManager | `Save`, `Delete`, ... | `Restore`, `Import`, `Rename` | Save/restore layer visibility around plotting |
| Dictionary / XRecord | read | `AddXRecord`, `SetXRecordData`/`GetXRecordData`, `AddObject` | Store the project sheet JSON inside the DWG |
| BlockReference | attributes, `InsertBlock` | `GetDynamicBlockProperties`, `Explode`, `GetConstantAttributes` | Dynamic blocks (doors/windows) — listed as a limitation |
| Group | — | `Groups.Add`, `AppendItems` | Group generated objects (rulers, dimension chains) without blocks |
| ExternalReference / RasterImage | read | `ImageFile` repair, `ClipBoundary`, `Reload` | Missing references blocking batch jobs (#16) |
| Preferences* | — | `PreferencesFiles.SupportPath`, `PreferencesOutput.DefaultPlotToFilePath` | Install-time checks |
| SecurityParams, MenuBar/Toolbars, ZPVB (`LoadZPVB`) | — | not needed | |

`Application.Eval` evaluates **VBA**, not LISP, so it cannot replace the LISP channel.

## 2. AutoLISP: documented functions and what the channel can use

All 536 documented entries are standard AutoLISP/Visual LISP plus DCL. Groups that matter for the server:

| Group | Functions | Use |
|---|---|---|
| Selection and entities | `ssget` (filters, `"_X"`, `"_WP"`/`"_CP"`), `ssnamex`, `entget`/`entmod`/`entmake`/`entmakex`/`entupd`, `handent`, `nentselp` | Bulk read/write (used: `zwmcp:count-by`, swept path, sun ruler) |
| Curves | `vlax-curve-*` (17 functions: closest point, distance at point, area, derivatives) | Swept path (used), boundary distances, chainage |
| ActiveX | `vlax-*`, `vla-*` (via `vl-load-com`) | Everything the COM model offers, in-process (fast) |
| Persistent data | `vlax-ldata-put/get/list/delete`, `dictadd`/`dictsearch`, `regapp` + XDATA (`xdroom`, `xdsize`) | Project sheet or tool metadata stored in the drawing |
| Cross-document | `vl-bb-set`/`vl-bb-ref` (blackboard), `vl-doc-set`/`vl-doc-ref` | Share state between drawings (e.g. one bundle version flag) |
| DB state | `acad-push-dbmod` / `acad-pop-dbmod` | Keep read-only calls from marking the drawing modified (#4) |
| Errors | `vl-catch-all-apply`, `vl-exit-with-error`, `*error*` | Used; `vl-exit-with-error` to replace `zwmcp:fail`'s `(exit)` |
| Reactors | `vlr-command-reactor`, `vlr-object-reactor`, `vlr-sysvar-reactor`, ... | Event-driven completion instead of polling files |
| Files and system | `vl-directory-files`, `vl-file-*`, `vl-registry-*`, built-in **DOSLib** `dos_*` (40 functions: `dos_filep`, `dos_dir`, `dos_clipboard`, `dos_execute`, ...) | DOSLib is part of ZWCAD; `dos_execute`/`dos_delete` stay out of tools (safety) |
| Text | `textbox`, `wcmatch`, `vl-string-*` | Text extents for label placement; wildcard matching |
| UI (not for automation) | `getpoint`, `entsel`, `getkword`, DCL | Interactive; never used by tools |

Live check 2026-10-25 (`execute_lisp`): `dos_filep`, `vl-bb-set`, `vlax-ldata-put`, `acad-push-dbmod`,
`vl-exit-with-error`, `vlr-command-reactor`, `entmakex`, `textbox`, `vl-registry-read` are all defined.
`GetZcadState` (via `vlax-invoke-method`) works in-process (`IsQuiescent` is false while LISP runs, as expected). ZWCAD 2024 LISP is
**not Unicode** in file I/O (`(ascii (chr 322))` ≠ 322): strings are exchanged in the ANSI codepage (cp1250).
`SECURELOAD` does not exist (`getvar` returns nil); the channel does not depend on it anyway.

## 3. Proposed next steps derived from the inventory

1. `Plot.SetLayoutsToPlot` + `StartBatchMode` + detached runner → batch PDF job with per-file results (#37).
2. `SummaryInfo.SetCustomByKey` / XRecord → project sheet stored in the DWG; title block fill with TAG
   validation (FORK_DESIGN step 5).
3. `GetZcadState().IsQuiescent` before `SendCommand`; `acad-push-dbmod` around read-only LISP calls.
4. `AddTable` + fields → site balance / room area table generated from `area_takeoff`.
5. `Region.Boolean` → exact areas for overlapping zones.
6. `SortentsTable.MoveToBottom` → draw order without commands (sun ruler, hatches).
7. `Document.PurgeAll` / `AuditInfo` → `purge_audit` tool for imported maps.
8. `SelectionSet.SelectByPolygon` / `ssget "_WP"` → "entities inside this plot" queries.
