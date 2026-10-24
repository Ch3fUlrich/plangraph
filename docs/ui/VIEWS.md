# plangraph UI: view spec

Status: draft 2026-10-17 (plangraph-L1-plan2, operator order relayed by routing). Design and items:
`docs/plans/ui/2026-10-17-ui-plan.md`.

This file says what each view must show and do. It does not depend on the framework: the static page
(`src/plangraph/render.py` + `src/plangraph/web/`) and the later SvelteKit page (`plangraph:RT-P3-SVELTE`) must both pass it.
Every line tagged `[V-…]` is an acceptance check. The browser suite (`plangraph:UI-BROWSER-TESTS`) has one test per tag and
runs it at 1440x900, 1024x768 and 390x844 unless the line names a size.

## 0. Rules for every view

- [V-ALL-1] No console error on load or on any action in this spec. Warnings are allowed only when listed in the suite's allow-list,
  and each one has an owner item.
- [V-ALL-2] Every view has an empty state (says why it is empty, for example "no open questions") and an error state (says what failed,
  for example "knowledge export not found"). A view never renders blank without a message.
- [V-ALL-3] Text from the snapshot is inserted as text, never as HTML.
- [V-ALL-4] Every view's state that a user would want to share or reload (page, selection, filters, sort, range) is in the URL. A reload
  restores it.
- [V-ALL-5] Back and Forward move between pages and selections the user made. Back never leaves the app while there are app steps to
  undo. Pan and zoom do not create history steps.
- [V-ALL-6] Touch targets are at least 40 px at 390x844, and nothing overflows horizontally.
- [V-ALL-7] Keyboard shortcuts act only on the visible page.

## 1. Shell (header, page switch, footer)

- Shows: page tabs (Graph, Table, Needs you, Decisions, Activity, Progress, Knowledge, Fleet, Settings); the global search; the build
  stamp.
- [V-SHELL-1] Build stamp: the code sha (short) and the snapshot time and `head_seq`. The code sha is the renderer's git sha and not
  the keeper head hash. It is in a `<meta name="plangraph-build">` tag too.
- [V-SHELL-2] The header height stays the same on all pages and does not wrap at 1024 px or wider.
- [V-SHELL-3] A tab whose data is missing (Knowledge without an export, Fleet without the fleet API) stays visible and shows its empty
  state with the reason. It is not hidden silently.
- [V-SHELL-4] The data age is shown. If the snapshot is older than 15 min, the stamp turns amber and says so.

## 2. Sidebar (projects tree)

- Shows: project > phase > task tree with state colour, counts done/total, and an eye toggle per project and phase.
- Does: a click on a task selects it and focuses it in the graph; the eye toggles hide or show that part in the graph and table.
- [V-SIDE-1] At widths below 1200 px the sidebar starts closed. When the user opens it, a scrim closes it; the scrim is never present
  while the sidebar is closed, and the first click on the graph always reaches the graph.
- [V-SIDE-2] Opening the details panel does not collapse or hide the tree.
- [V-SIDE-3] The tree highlights the selected item and scrolls it into view.
- [V-SIDE-4] Eye-toggle state is in the URL, and after a reload the tree and the graph agree.
- [V-SIDE-5] Counts use the same rule as the Table and Progress (see 9).

## 3. Graph

- Purpose: see the structure (projects, phases, tasks, dependencies) and find what blocks what.
- Shows: projects as boxes, phases inside them, tasks inside phases; edges for dependencies; cross-project dependencies as stubs that
  name the target; state colour; badges for operator-waiting, claimed and in-review. It opens on the collapsed project overview, fitted
  to the screen.
- Does: select (click a task), expand or collapse (click a project or phase header), pan, zoom, focus (from search, tree, Needs you,
  Activity or a details link), and deselect (Escape or a click on empty canvas).
- Viewport rules (these define "no layout jumps"):
  - The page fits the view automatically only once, on first display. After that, only the user (pan, zoom, "Fit" button) or a
    focus action moves the viewport.
  - [V-GRAPH-1] Selecting a task moves no node in model coordinates and does not change zoom.
  - [V-GRAPH-2] If the details panel covers the selected node, the view pans (animated, max 250 ms) so that the node is at least 24 px
    inside the visible canvas. Otherwise pan stays as it was.
  - [V-GRAPH-3] Expanding or collapsing keeps the clicked header at the same screen position (within 4 px). If the expanded box does not
    fit, the view then zooms out until it fits (not below 0.3), keeping the header in view.
  - [V-GRAPH-4] Opening or closing the details panel and resizing the window never trigger a fit or a zoom reset.
  - [V-GRAPH-5] A focus action expands what is needed, then centres the target at zoom >= 0.75, in one animation. Nothing else moves
    the viewport during that animation.
- Input during layout:
  - [V-GRAPH-6] While a relayout runs, the graph shows a busy indicator. A click during it is applied after the layout to the element
    the user clicked (by key, not by screen position), or dropped with the indicator still visible. It never lands on a different
    element.
- Selection:
  - [V-GRAPH-7] The selected node is highlighted and its direct dependencies and dependents are emphasised; the rest is dimmed.
  - [V-GRAPH-8] Selection survives collapse and re-expand, page switches and reload (from the URL). After a reload the selected task's
    phase is expanded and the task is in view.
  - [V-GRAPH-9] A double click does exactly what a single click does.
- Phone:
  - [V-GRAPH-10] At 390x844 an expanded phase shows its tasks (no zero-size nodes) and no box overlaps another box.
- Errors:
  - [V-GRAPH-11] If the graph library or the layout fails, the canvas shows "graph unavailable: <reason>" and the console has the
    error. No exception is swallowed without a log line.
- Edges:
  - [V-GRAPH-12] No edge with an invalid endpoint is created (no library warning).

## 4. Details panel

- Shows: key, title, state, kind, project > phase path, owner, priority, estimate, review tier, done criteria, dependencies and
  dependents (each a link), evidence (kind, ref, verified, time), loop counters, choices and answer (questions), linked decisions, a
  Knowledge block when the export has one.
- Does: copy key; show in table; focus in graph; follow dependency links.
- [V-DET-1] At widths >= 1200 px the panel docks on the right; below that it opens as a sheet over the canvas. Either way V-GRAPH-2
  holds.
- [V-DET-2] A dependency link selects that item and focuses it in the graph (V-GRAPH-5).
- [V-DET-3] Every field renders as readable text; no `[object Object]`, no `undefined`.
- [V-DET-4] One selection renders the panel once.
- [V-DET-5] The panel closes on Escape, on its close button and on page switch to a page that has no selection (Activity, Progress,
  Settings). It stays on Graph and Table.

## 5. Table

- Shows: all items as rows with sortable, filterable columns; group by any column with a per-group progress bar.
- [V-TAB-1] The global search filters the table while it is shown.
- [V-TAB-2] Filters, sort, grouping and collapsed groups are in the URL.
- [V-TAB-3] Selecting a row selects the item (shared with the graph and details).
- [V-TAB-4] At 390x844 rows become cards (existing behaviour) and stay usable.

## 6. Needs you

- Purpose: everything that waits on the operator, ordered by what it unblocks.
- Shows: open questions, decisions to take and operator tasks, one row each: what is asked, the options with the default marked,
  the project, the age, and **"unblocks N"**: the number of agent-owned open items that wait only on this row, plus the downstream
  count. Default sort: unblocks N, then age.
- Does: filter by project, kind and state (open, answered); answer (once the operator write path exists, `plangraph:PG-ANSWER-UI`);
  until then copy an answer line.
- [V-NEED-1] The unblocks count equals the keeper's dependency graph (test against a fixture snapshot).
- [V-NEED-2] The "answered" filter shows answered rows with answer, who, when and the D-id (this is the Q&A history).
- [V-NEED-3] A row's ID link selects the item without reloading the page.
- [V-NEED-4] Jump focuses the item in the graph (V-GRAPH-5).

## 7. Decisions

- Purpose: the decision log: what was decided, by whom, when and what it touches.
- Shows: one row per D-row: id, text, attribute human or agent, actor, time, linked items (each a link), superseded or edited
  marker.
- Does: filter by attribute, project and time; search the text; edit a decision (operator only, `plangraph:PG-DEC-EDIT-NOTICE`).
- [V-DEC-1] Attribute and actor come from keeper decision records (`plangraph:PG-DEC-ATTRIBUTION`), not from heuristics in the page.
- [V-DEC-2] An edit shows the previous text, the editor and the time (hash-chained event); agents that read the item see the
  changed-since marker.
- [V-DEC-3] Without the operator principal, the edit control is not shown.

## 8. Activity

- Shows: events in a time range (24 h, 7 d, 30 d, custom): created, done, blocked and unblocked, counts per project, and a list.
- [V-ACT-1] The range is in the URL.
- [V-ACT-2] A key link switches to the graph and focuses the item.

## 9. Progress

- Shows: per project and phase: done / total and percent, plus what is running.
- [V-PROG-1] One counting rule for Progress, Table groups, the sidebar and graph cards: total = all items minus cancelled; done = done.
  The rule is written next to the bar.

## 10. Knowledge

- Shows: decisions, rules and superseded parts linked to plan items (`plangraph:KG-VIEW`); until that lands, the export view.
- [V-KG-1] If the export is missing, the tab shows its empty state with the reason (V-SHELL-3) and the console has no 404 error (the page
  asks for the export only when it was rendered with one).

## 11. Fleet

- Built by `plangraph:UI-FLEET-TAB`. Shows: per host the latest sample and 24 h trend with stale and down; live sessions with context %;
  provider availability; the last placement decisions with reasons; the rule editor (effective value, layer, per-host overrides).
- [V-FLEET-1] Until the fleet API exists, the tab shows its empty state with the reason.

## 12. Settings

- Shows: text size, theme, node budget and other toggles. Every toggle shown has an effect; a toggle without one is removed.
- [V-SET-1] Changing a setting does not reset the graph (no collapse, no viewport change).
