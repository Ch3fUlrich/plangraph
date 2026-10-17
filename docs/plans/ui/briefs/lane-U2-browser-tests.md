# Lane U2: UI-BROWSER-TESTS (8 h)

Read `COMMON.md` first. This lane comes before every fix lane: it turns the spec and the repros into red and green tests.

## Setup

- Python Playwright (`pytest-playwright`), headless Chromium. Add it to `requirements-ci.txt` and cache the browser in CI
  (`actions/cache` on `~/.cache/ms-playwright`).
- Run it as its own CI job `browser` in `tests.yml`, with the same path filters, a 15 min timeout, and the `tests` job untouched.
- Fixture: render a page from a FIXED fixture snapshot (`tests/fixtures/ui_snapshot.json`; derive it from `examples/snapshot.json`,
  scrubbed: no real hosts or names). It holds:
  - at least 3 projects;
  - a phase with more than 50 tasks;
  - cross-project deps;
  - questions (open and answered) and decisions;
  - activity events;
  - an item with an object `loop`.
- Serve it over `http://127.0.0.1` (a local server in a fixture), not `file://`.
- Offline: vendor libraries come inline or from a local copy. Tests never hit a CDN.

## Helpers

- Graph probes via `page.evaluate`:
  - viewport `{zoom, pan}`;
  - the model position of a node by key;
  - the rendered bounding box of a node by key;
  - the visible canvas rect;
  - "is the node inside the visible canvas".
- Wait for layout idle: a page hook, for example `window.__pgLayoutIdle()`, resolving when no relayout is pending. If the page has
  none, add the smallest one to the page in this lane and name it in the commit.
- Console capture: fail on any `error`. Warnings are checked against `tests/browser/allow_warnings.txt`, where each line names an
  owner item.

## Tests

- One test per `[V-…]` tag in `docs/ui/VIEWS.md`, at the sizes the tag names (default: 1440x900, 1024x768, 390x844).
- R1-R10 from the design, section 3, each as its own test with the measured numbers as the assertion. For example, R2: after the click,
  the node is inside the visible canvas.
- Tags and repros the current code fails are marked `xfail(strict=True, reason="<owning item>")`. strict means that when a fix lane
  makes one pass, the xfail must be removed in that lane.
- A tag that cannot be tested yet (no page support, for example Fleet) is `skip` with the owning item.

## Done

- The suite runs in CI in under 5 min.
- Every V-tag is a test (pass, xfail or skip with an owner).
- R1-R10 exist.
- Lane report: a table of tag, status and owner.
