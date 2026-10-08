# UI/UX redesign of the plan visualizer — two-seat cross-family review record (2026-10-06, operator-ordered)

Operator order: the page lacked design investment; AI agents were to review the live page
(pictures + hard data), tabs/buttons/sidebar behavior fixed to best practice, with a UX
research track. Executed per the autoOS unattended-orchestration protocol
(`tools/autoos-agent.py` via OmniRoute, all runs `--isolate --lean --read-only`,
writer family zhipu/GLM fenced with `--not-family zhipu`).

## Evidence + research (commit 0957402..c3ed1c0 in the review clone)
- 5 BEFORE screenshots (desktop graph/details/table/search, phone 390x844) + DOM geometry
  audits of every control (`dom-audit-desktop.json`, `dom-audit-phone.json`): sidebar
  300px wide × **2478px tall** on a 720px viewport (whole page scrolled), chips 44–48px
  while header controls were smaller (inverted hierarchy), tab strip below the header.
- Research seat (t2-worker-clean): `UX-BEST-PRACTICES.md` — 15 principles with named
  sources (Nielsen, NN/g, Material, HIG, WCAG 2.5.8 / 1.4.11, Few) + a 10-item pass/fail
  checklist.

## Round 1 — both seats NOT READY (vision = gemini-3.8-flash pin; structure = ovh-gpt-oss-120b pin)
Vision (looked at the PNGs) 9 findings incl. 2 BLOCKERs (unbounded sidebar/page scroll;
phone chip stack pushing the graph below the fold) + a catch the text seat missed:
dark `--border` contrast 1.43:1 vs WCAG 1.4.11's 3:1. Structure: fix plan table + the
same checklist failures (7 FAIL). Agreed independently on the blockers.

## Implementation (runner tree, uncommitted; page_base.css ≤ 400 cap kept)
- **App shell**: `html,body { height:100%; overflow:hidden }`, `.layout` flex-fills the
  viewport with `min-height:0; overflow:hidden`; main scrolls internally; `#cy` flex-fills
  (canvas share 70.6%); sidebar `height:100%` + internal `.sb-body` scroll.
- **Tabs into the header** (template move): compact segmented pills, active = bold/700,
  32px; phone keeps the bottom tab bar.
- **Size tiers** (P3): 44px floor kept ≤700 (touch); desktop tiers — chips 28px, Jump 28,
  icon buttons 36, sb-tabs 38, checkbox 20.
- **Phone**: single-row horizontally scrolling chip groups; duplicate quick-filter row
  removed; Inbox/Menu converted from full-screen takeover to an 82vw off-canvas drawer;
  bottom-sheet full snap capped 92vh → 60vh.
- **Details**: 320px dock + `:has(#details:not([hidden]))` auto-rail collapse of the
  sidebar <1440px; field typography (uppercase keys, 13px values); 32px close button.
- **Contrast**: dark `--border` → `#5a6a80` (measured **3.13:1** vs panel, WCAG 1.4.11
  PASS) and `--muted` → `#a6b1c2` in BOTH the prefers-color-scheme block and the
  `html[data-theme="dark"]` forced block (the vision seat's round-2 catch: the forced
  block still carried the old values).

## Rounds 2–3
Round 2 (both seats, on the diff + after-audits): structure NOT READY on a stale audit I
had committed (captured mid-viewport-switch, `pageScrolls: true`) — corrected audit
(`pageScrolls: false`) committed; vision NOT READY with a REAL catch: the forced-dark
token block still carried `#2b313a` (my round-1 fix had covered only one of the two
token definitions) — fixed. Round 3: **both seats READY, zero findings**; 10/10
checklist PASS (tab strip fixed+prominent, sidebar ≤100vh internal scroll, phone drawer,
28px chips, canvas ≥70%, contrast 3.13:1). Verification: 72 UI/render tests green, all
changed files ≤ 400 lines, before/after screenshots + audits archived in
`C:/fleet/oc/plangraph/ux-review/` (review clone: C:/Users/mauls/AppData/Local/Temp/ux-review/repo).

## Notes for routing
- The redesign touches page.css/page_base.css/page_template.html (runner tree, uncommitted,
  on `parked/gu-tb1-wip` + TB1 work) — acceptance via the normal record → ACCEPT → merge
  path; real-browser probe satisfied by the before/after screenshot sets.
- Deviations from the research checklist, accepted with reasons: search sits after the
  title (x≈249) not ≤16px from the edge — standard app-bar pattern (title anchor first);
  phone chips stay 44px touch height (WCAG 2.5.8) while densifying desktop to 28px.
