# Lane U5: UI-DEAD-CODE (3 h)

Read `COMMON.md` first. Start last: after U3 (all three items) and U4 have merged.


Remove, or wire where U3 already did:
- the dagre path (`page_ui.js:168-345` and its inlined vendor libs);
- `page_graph_focus.js`, `page_graph_budget.js`, `page_graph_chips.js`, and the unused parts of `page_graph_cluster.js`, together with
  their now-meaningless tests;
- the `expanded` flag;
- the hidden `#panel-qa` and `QAPage` rebuild;
- the `#collapse-done` checkbox;
- the `showCritical` and `autoCollapseDone` toggles.

Name each in the commit. The browser suite and node tests stay green, and the page does not get larger.
