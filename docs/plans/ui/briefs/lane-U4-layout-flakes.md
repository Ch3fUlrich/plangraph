# Lane U4: UI-NARROW-LAYOUT and UI-FLAKE-FIXES; Lane U5: UI-DEAD-CODE

Read `COMMON.md` first. Start U4 after U2 has merged.
- UI-FLAKE-FIXES also edits `page_graph_view.js` (`runSync` catch, edges). If U3 is running, take those two hunks last and rebase on
  U3's merged head first.
- U5 starts last.

## UI-NARROW-LAYOUT (4 h): R1

- Below 1200 px the sidebar starts closed. `#sb-scrim` exists or captures pointer events only while the user has the sidebar open
  (V-SIDE-1).
- Remove the `body:has(#details:not([hidden]))` rule that shrinks the sidebar to a rail (`page.css:219-224`; V-SIDE-2).
- The header stays on one line at 1024 px and up (V-SHELL-2). The stamp from U1 must fit.
- Phone: a compact legend (one row, or behind a toggle). The progress label is readable on the green fill (contrast at least 4.5:1).

## UI-FLAKE-FIXES (6 h): R8, R9, R10 and the code-reading list

- R10: `runSync` `.catch` (`page_graph_view.js:227`) logs the error with `console.error` and shows the reason (V-GRAPH-11). The same
  goes for other silent catches in `page_*.js` (grep `catch`).
- If the graph library is missing (CDN blocked), the canvas shows a message, not a blank page.
- R8: render records whether a knowledge export exists. The page requests `kg-export.json` only then. The Knowledge tab always shows,
  with its empty state (V-KG-1, V-SHELL-3). Fleet gets the same empty state (V-FLEET-1).
- R9: ghost stubs never become edges with an invalid endpoint (V-GRAPH-12). Drop the stub when its target is present.
- V-PROG-1: one counting helper (total = all minus cancelled; done = done) used by Progress, table groups, the sidebar and graph cards.
- V-TAB-1: the table subscribes to the global search.
- V-SET-1: a settings change does not call `PP.renderGraph` (no collapse reset). Apply only what changed.
- V-ALL-7: global keys act only on the graph page.
- Details: `loop` renders as `iteration/max (exit)`, never as an object dump (V-DET-3).
- The wheel sensitivity goes back to the library default, and the warning goes away.

## Lane U5: UI-DEAD-CODE (3 h)

Remove, or wire where U3 already did:
- the dagre path (`page_ui.js:168-345` and its inlined vendor libs);
- `page_graph_focus.js`, `page_graph_budget.js`, `page_graph_chips.js`, and the unused parts of `page_graph_cluster.js`, together with
  their now-meaningless tests;
- the `expanded` flag;
- the hidden `#panel-qa` and `QAPage` rebuild;
- the `#collapse-done` checkbox;
- the `showCritical` and `autoCollapseDone` toggles.

Name each in the commit. The browser suite and node tests stay green, and the page does not get larger.
