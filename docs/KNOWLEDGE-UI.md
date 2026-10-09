# Knowledge UI (PG-KG-UI, D-879)

The plan page shows Omnigraph knowledge next to plan items. Data comes only from `kg-export.json`, the static file
the render job writes beside the page (PG-KG-EXPORT, see `docs/KEEPER.md`). The page loads it with a same-origin
`XMLHttpRequest` (the page keeps its "no `fetch(`" invariant) and never talks to Omnigraph.

- **Item detail**: a "Knowledge" block lists each `kg_refs` entry as a node row: type, title, status (Decision status,
  Rule severity, Component kind), an excerpt, and a "superseded -> <current node> (status)" marker when a Decision
  was replaced. A ref whose graph was read but holds no such node shows "no such node" (dangling); a ref into an
  unreadable graph shows "unavailable". Items without refs show no block.
- **Knowledge tab** (header, after Progress): pick a plan project; nodes of that project grouped by type, each with
  the reverse index (node -> items that reference it, directly or through a superseded predecessor). An item link
  focuses the item in the graph.
- **Hidden, never broken**: a missing file, a non-200 answer, bad JSON, `{"enabled": false}`, or an export with no
  nodes and no refs leaves the tab and the blocks hidden and raises no error. `file://` previews behave the same.
- **Safety**: every export string is length-capped and written with `textContent`; the code never uses `innerHTML`,
  `outerHTML`, `insertAdjacentHTML`, `document.write` or `eval` (asserted by `tests/test_ui_knowledge.py`, with an XSS
  fixture). Export maps are built on prototype-less objects and refs must match `graph:Type:slug`.

Files: `src/plangraph/web/page_kg.js` (data, item block, loader), `page_kg_tab.js` (tab), `page_kg.css`;
wired in `render.py` (`JS_FILES`, `CSS_FILES`), `page_pages.js`, `page_details.js`, `page_template.html`.

Size: the new-UI page budget (`tests/test_ui_pure.py`, A13/V3/§12) went from 500 KB to 520 KB; the Knowledge code adds about
11 KB inline. Caps in the loader: 8000 nodes, 20000 keys per map, 40 refs per item, 200 items per node; item keys must
look like `project:id`, graph names are <= 64 chars. An export without nodes is treated as disabled.
