# Table view design note (D-586) — 2026-10-04, plangraph-L1

Operator: "the list page has no table; I want a table showing everything properly, with filter, search and so on." The ui-build line has no table view; UI-TABLE was blocked behind the
never-started SvelteKit shell. Router ruling: build it on the EXISTING static page (a "Table" tab beside the graph, keeper JSON as today), read-only first; batch priority writes stay with UI-WRITES.
Reuse, do not research again: docs/ui/UI-PLAN.md §6 (Table: native table, sticky header, columns, grouping, cards on the phone) and §7 (the shared Query/FilterBar), plus the GRAPH-UI design note
(palette from GU-G2a, 560-item scale).
## Scale decision
UI-PLAN §6 said "<= 400 rows, no virtualisation". The graph now has 589 items and grows, and the router asks for 1000+ rows fast: window the rows (render only the visible range plus overscan; fixed row
height in a scroll container; group headers are rows) and measure it (rendered row count bounded, filter/sort/search within a stated budget on 1000+ synthetic rows).
## Columns and behaviour
Columns: state badge, project dot/colour, title (+ key chip), phase, owner, priority, est h, kind, operator-needed, unblocks; a column chooser (persisted in the URL). Multi-column sort (stable).
Full-text search over title, key, owner, description, done criteria. Filters: project, phase, state, owner, priority, kind, operator-needed; counts "37 of 589". Grouping per project with collapsible
phase groups (collapse state in the URL). Row colour per project (palette from GU-G2a projectColors). Inline row details; row click opens the existing details panel. Filters, sort, search and columns
live in the URL query string (history.replaceState) so a view can be shared and survives reload. Phone (< 600 px): cards (title wrapping, badge + project stripe, meta line), sticky group headers, a
filter sheet; no horizontal scroll.
## Steps (agy writer, <= 3 substantive files each, synthetic fixtures, real-browser probe by workst, one non-Google seat)
TB1 UI-TABLE-MODEL (pure: columns, sort, filter, search, grouping, windowing, URL state; node-run tests; perf on 1000+ rows) -> TB2 UI-TABLE-VIEW (DOM, strict fake DOM tests, probe at 1440x900)
-> TB3 UI-TABLE-WIRE (Table tab, filter/search bar, URL persistence, selection to the details panel, measured hook; touches the page shell, so after the GRAPH-UI wiring G2b) -> TB4 UI-TABLE-PHONE.
TB1 and TB2 are NEW files and can start now, in parallel with GRAPH-UI, alternating slots (max 2 agy writers fleet-wide). Visual check by a non-writer, then the OPERATOR reviews a local preview; no deploy before his OK.
