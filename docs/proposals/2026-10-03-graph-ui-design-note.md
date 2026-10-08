# GRAPH-UI design note (D-560) — 2026-10-03, plangraph-L1

Inputs: R1 (large-graph practice) and R2 (public Omnigraph UI), two free non-Claude research runs on a public question (no plan data in the
prompts; model `muse-spark-1.3-contributor-free`, family meta, resolved `assumed-default`); plus my own read of our deployed viewer (AutoOS
`infra/mcp-servers/servers/omnigraph-viewer`, the code behind omnigraph-ui.ohje.ooguy.com). Every source below was fetched by the run or read by me;
claims the run marked `unverified` stay unverified here (thresholds are my proposals, to tune in a prototype).

## 1. What the operator's reference actually is
- Public Omnigraph (ModernRelay/omnigraph, MIT) ships NO graph canvas UI; its companion `ModernRelay/notebook` shows text lenses (ego lists, paths)
  [R2 sources 1, 4, 5, 9, 14]. So "like the Omnigraph UI" = **our own viewer** (omnigraph-viewer, Flask + one self-contained vanilla JS/SVG page, 999 lines):
  - one **chip per project/graph**, each with a stable colour and node count; click = switch, ctrl/shift-click = add; "all graphs" shows everything;
  - one **tinted convex hull + label per cluster** (graph = project), clusters anchored apart so they read at a glance;
  - **focus-on-click**: the node's 1-hop neighbours gather in the centre, everything else drifts to a ring and fades; Esc/click empty space exits;
  - drag nodes, scroll zoom, pan; a **legend that toggles node types**; **search dims** non-matches; a table view with live filter; side detail on click.
  It is force-directed (no layering), no edge bundling, and it gets away with it because its graphs are small. Ours (about 560 nodes, 1500 edges) are not.

## 2. Best practice for a graph of our size (R1, sources are its list [1]-[14])
- Compound nodes (project > phase > task), **default all collapsed**; click expands in place; keep two levels (Cytoscape.js compound graphs [1];
  expand-collapse extension API [2]).
- **Edge aggregation**: collapsed clusters show ONE meta-edge per cluster pair with a count; intra-cluster edges hidden; hover/click reveals the breakdown [2].
  Edge budget (my proposal, unverified): overview <= 40 edges, one expanded phase <= 150.
- **Level of detail**: zoom bands (clusters + counts, then phase names + progress, then task labels for non-done tasks, then everything); hide labels by
  opacity, never relayout on zoom (unverified, cheap).
- **Layouts**: fcose is compound-aware and incremental (`randomize:false` keeps the mental map) [3]; dagre/ELK-layered give the best DAG reading [4][5][14];
  d3-force has no layering [13]. Libraries: Cytoscape.js is the right base (MIT, touch gestures, compound); Sigma has no compounds [7]; React Flow
  is DOM-per-node (needs collapsing at this size) [8]; yFiles is paid [12]. Caveat from R1: the expand-collapse extension README says it is no longer
  maintained [2], so own the cluster model instead of depending on the extension (T3b already does: `phc:`/`pc:` collapsed leaves).
- Interaction: focus+context, breadcrumbs `All > Project > Phase`, "next ready", keyboard (`/`, `0` fit, Esc up one level), minimap on desktop only.

## 3. Recommendation (what to build on the ui-build line)
Keep Cytoscape.js and the T3b-1 line (compound clusters, details, search, phone sheet), and make it behave like the omnigraph viewer where that helps:
1. **Overview = project chips + hulls.** Default: every project is one coloured hull holding its PHASE nodes (collapsed, with done/total + a progress bar),
   aggregated meta-edges with counts, project colours from the viewer's palette. Chip bar on top (click = fit that project, ctrl-click = add, "all").
2. **Expand in place**: click a phase to show its tasks (done tasks dimmed or hidden by a toggle), layered (dagre/ELK) inside the phase, fcose between clusters.
3. **Focus-on-click** as in the viewer: 1-hop neighbours stay, the rest fades; Esc exits; double-click = expand + focus.
4. **Edge budget**: never draw more than the budget; surplus edges become meta-edges; hover or focus reveals the real ones.
5. Legend that toggles kinds/states, search that dims, details panel (exists), phone: chips + breadcrumbs, no minimap.
Build steps for the writer (agy, $2 each, synthetic fixtures): G1 model: default-collapsed state + meta-edge aggregation (pure, tested);
G2 project colours, hulls and chips; G3 focus-on-click; G4 edge budget + hover reveal; G5 phone behaviour + regression pass.
Then the visual check by a non-writer agent on a local build against the real keeper, and the OPERATOR as the reviewer.

## 4. Honest limits
- Both research runs are free-model output: the sources exist (fetched), but every performance claim and every threshold above is a proposal, not a measurement.
- R2 found no public Omnigraph graph UI; the behaviours in section 1 come from reading our own viewer's code, not from upstream.
- Until T3b is merged, plan.ohje.ooguy.com still shows the OLD page; nothing deploys before the operator's OK.
