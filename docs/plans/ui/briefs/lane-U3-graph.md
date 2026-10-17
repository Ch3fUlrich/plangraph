# Lane U3: UI-GRAPH-CLICK, then UI-GRAPH-EXPAND, then UI-URL-HISTORY

Read `COMMON.md` first. Start after U2 merged. One item at a time, in this order: they touch the same files (`page_graph_view.js`,
`page_graph_viewport.js`, `page_graph_pan.js`, `page_boot.js`, `page_details.js`).

## UI-GRAPH-CLICK (8 h): R2, R3

Causes (code reading, `origin/main` ce3c2de):
- `selectNode` (`page_graph_view.js:233`) sets `ui.sel`. The details panel docks (`page_base.css:226-231`), and `#cy` narrows.
- The ResizeObserver (`:354`) and boot `resizeGraph` (`page_boot.js:114-123`) then call `refreshInitialViewport`. Until the user has
  panned or zoomed (`userTouched`, `:319`, never reset), that refits or re-zooms. That is R3.
- Nothing keeps the selected node visible after the canvas shrinks. That is R2.
- `selectNode` also renders the panel twice (store subscriber plus `PP.renderDetails`, `:240`).
- Details links call a dead local `focusNode` (`page_details.js:255`, wrong element id).

Target behaviour: VIEWS.md 3 (viewport rules) and 4.
- Fit only once, on first display. Remove the refit from resize and panel paths.
- After the panel opens, if the selected node is outside the visible canvas minus 24 px, pan it in (max 250 ms). Otherwise leave pan
  alone.
- One render per selection.
- Details links use `PP.focusNode`.
- Neighbour emphasis (V-GRAPH-7): wire the existing neighbour or dim code if it fits the budget. Otherwise write the smallest new one.
  Say which in the commit.
- Double click acts like a single click. Remove the `dbltap` and `expanded` toggle, or make it a no-op.

## UI-GRAPH-EXPAND (8 h): R4, R5, R6

Causes:
- A header tap changes the collapse sets. A full `sync()` follows: elements are rebuilt, ELK runs per project and nodes are animated
  (`:193-200`). New nodes are added without a position (`:167`), so they come from (0,0). Nothing anchors the clicked header. That is
  R4.
- Taps during the async layout hit whatever is under the pointer then. That is R5.
- On a phone the expanded phase is a 59x33 stub with zero-size tasks, and project boxes overlap. That is R6; the cause is not proven,
  so measure first.

Target behaviour:
- V-GRAPH-3: record the clicked header's screen position before the layout and restore it after. Then zoom out to fit the expanded
  box (min 0.3) if needed.
- V-GRAPH-6: a busy flag during layout. A tap during it is queued by element key and applied after, or dropped while the indicator
  shows. Never re-hit by position.
- New nodes start at their parent's position.
- V-GRAPH-8: selection re-applied after every sync, and after load once the layout is done.
- V-GRAPH-10: fix the phone layout. Find the cause first and write it in the commit.

## UI-URL-HISTORY (4 h): R7

- One module owns URL state (page, selection, collapse, filters, table and needs state, activity range). Hash and query codecs live
  in that one place.
- `pushState` for page switches and selection changes; `replaceState` for everything else. A `popstate` handler restores state.
- `pg=` covers every page.
- Compact collapse encoding: ids or a short hash per phase instead of full names, so the hash stays under 1 KB on the live snapshot.
  Old links still parse.
- Checks: V-ALL-4, V-ALL-5, V-SIDE-4, V-TAB-2, V-ACT-1, V-NEED-3.
