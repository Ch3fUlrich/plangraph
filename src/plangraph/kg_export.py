"""PG-KG-EXPORT (D-879): render-time, read-only export of Omnigraph knowledge for the static plan page.

Runs inside ``pg render`` (no new service). Reads Omnigraph with a read-only token, resolves the nodes named by
item ``kg_refs`` through ``Supersedes`` to the current node, adds per-project knowledge (title + status only, no
embeddings) and writes ``kg-export.json`` next to the page plus ``kg-export-status.json`` (dangling refs).

Off without a token: ``PG_KG_URL`` or ``PG_KG_TOKEN_FILE`` unset, or the token file missing/empty -> writes
``{"enabled": false}`` and exits 0. Any Omnigraph failure also exits 0 (the page must still render).

Env: ``PG_KG_URL`` (base URL), ``PG_KG_TOKEN_FILE`` (0600 file), ``PG_KG_GRAPHS`` (comma list of extra graph ids to
export in full), ``PG_KG_STATUS_FILE`` (status path override). The token is never logged or put in a message.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

EXPORT_NAME = "kg-export.json"
STATUS_NAME = "kg-export-status.json"
TIMEOUT_S = 8.0
MAX_RESPONSE_BYTES = 8_000_000
EXCERPT_MAX = 240
TITLE_MAX = 300
MAX_LISTED = 1500          # per graph, listed (non-referenced) nodes
MAX_ROWS = 5000
MAX_GRAPHS = 12
DEADLINE_S = 60.0
LISTED_TYPES = ("Decision", "Rule", "Component")
_REF = re.compile(r"([a-z0-9][a-z0-9-]{0,62}):(Decision|Rule|Convention|Component|Preference|Project):"
                  r"([a-z0-9][a-z0-9._-]{0,127})")
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# type -> (query name, query text, title field, status field, extra field(s))
_NODE_Q = {
    "Decision": ("kg_decision", "query kg_decision() { match { $n: Decision } return { $n.slug, $n.title, "
                 f"$n.status, $n.rationale }} limit {MAX_ROWS} }}"),
    "Rule": ("kg_rule", f"query kg_rule() {{ match {{ $n: Rule }} return {{ $n.slug, $n.statement, $n.severity }} "
             f"limit {MAX_ROWS} }}"),
    "Component": ("kg_component", f"query kg_component() {{ match {{ $n: Component }} return {{ $n.slug, $n.name, "
                  f"$n.kind }} limit {MAX_ROWS} }}"),
    "Convention": ("kg_convention", f"query kg_convention() {{ match {{ $n: Convention }} return {{ $n.slug, "
                   f"$n.name }} limit {MAX_ROWS} }}"),
    "Preference": ("kg_preference", f"query kg_preference() {{ match {{ $n: Preference }} return {{ $n.slug, "
                   f"$n.statement }} limit {MAX_ROWS} }}"),
    "Project": ("kg_project", f"query kg_project() {{ match {{ $n: Project }} return {{ $n.slug, $n.name }} "
                f"limit {MAX_ROWS} }}"),
}
_EDGE_Q = {
    "supersedes": ("kg_supersedes", f"query kg_supersedes() {{ match {{ $a: Decision $a supersedes $b }} "
                   f"return {{ $a.slug, $b.slug }} limit {MAX_ROWS} }}"),
    "Decision": ("kg_decided_in", f"query kg_decided_in() {{ match {{ $n: Decision $n decidedIn $p }} "
                 f"return {{ $n.slug, $p.slug }} limit {MAX_ROWS} }}"),
    "Rule": ("kg_constrains_project", f"query kg_constrains_project() {{ match {{ $n: Rule $n constrainsProject "
             f"$p }} return {{ $n.slug, $p.slug }} limit {MAX_ROWS} }}"),
    "Component": ("kg_part_of", f"query kg_part_of() {{ match {{ $n: Component $n partOf $p }} "
                  f"return {{ $n.slug, $p.slug }} limit {MAX_ROWS} }}"),
    "Convention": ("kg_applies_to", f"query kg_applies_to() {{ match {{ $n: Convention $n appliesTo $p }} "
                   f"return {{ $n.slug, $p.slug }} limit {MAX_ROWS} }}"),
}


class KgError(Exception):
    """An Omnigraph read failed. The message never carries the token. ``missing``: the graph does not exist."""

    def __init__(self, message, missing=False):
        super().__init__(message)
        self.missing = missing


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: the bearer token must only ever go to the configured host."""

    def redirect_request(self, *args, **kwargs):
        return None


# no ProxyHandler: the bearer token must not go to an environment proxy either
_OPENER = urllib.request.build_opener(_NoRedirect, urllib.request.ProxyHandler({}))


# --------------------------------------------------------------------------- #
class OmnigraphClient:
    """Tiny read-only client: ``POST <base>/graphs/<id>/query``; callable as a transport."""

    def __init__(self, base_url, token, timeout=TIMEOUT_S):
        base = str(base_url or "").rstrip("/")
        if not re.match(r"https?://[^\s/]+", base):
            raise KgError("Omnigraph URL must be http(s)")
        self.base, self._token, self.timeout = base, token, float(timeout)

    def __call__(self, graph, name, text, params=None):
        url = f"{self.base}/graphs/{urllib.parse.quote(str(graph), safe='')}/query"
        body = json.dumps({"query": text, "name": name, "params": params or {}, "branch": "main"}).encode()
        req = urllib.request.Request(url, data=body, method="POST", headers={
            "Content-Type": "application/json", "Accept": "application/json",
            "Authorization": f"Bearer {self._token}"})
        try:
            with _OPENER.open(req, timeout=self.timeout) as resp:  # scheme checked; redirects refused
                raw, t_end = b"", time.monotonic() + 3 * self.timeout   # total budget: a slow drip cannot hold the render
                while len(raw) <= MAX_RESPONSE_BYTES:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    raw += chunk
                    if time.monotonic() > t_end:
                        raise KgError(f"graph {graph}: read too slow")
        except urllib.error.HTTPError as exc:
            raise KgError(f"graph {graph}: HTTP {exc.code}", missing=exc.code == 404) from None
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise KgError(f"graph {graph}: {type(exc).__name__}") from None
        if len(raw) > MAX_RESPONSE_BYTES:
            raise KgError(f"graph {graph}: response too large")
        try:
            rows = json.loads(raw).get("rows")
        except (ValueError, AttributeError):
            raise KgError(f"graph {graph}: bad response") from None
        if not isinstance(rows, list):
            raise KgError(f"graph {graph}: bad response")
        return rows


# --------------------------------------------------------------------------- #
def _clean(value, limit):
    if not isinstance(value, str):
        return None
    return _CTRL.sub("", value)[:limit]


def _slug(row, key="n.slug"):
    s = row.get(key) if isinstance(row, dict) else None
    return s if isinstance(s, str) and 0 < len(s) <= 160 else None


def _node_rows(rows):
    return [r for r in rows if isinstance(r, dict) and _slug(r)]


def _make_node(graph, typ, row):
    slug = _slug(row)
    node = {"graph": graph, "type": typ, "slug": slug, "title": None, "status": None, "severity": None,
            "kind": None, "excerpt": None, "project": None, "projects": [], "superseded_by": None, "current": None}
    if typ == "Decision":
        node["title"], node["status"] = _clean(row.get("n.title"), TITLE_MAX), _clean(row.get("n.status"), 20)
        node["excerpt"] = _clean(row.get("n.rationale"), EXCERPT_MAX)
    elif typ == "Rule":
        node["title"], node["severity"] = _clean(row.get("n.statement"), TITLE_MAX), _clean(row.get("n.severity"), 10)
    elif typ == "Component":
        node["title"], node["kind"] = _clean(row.get("n.name"), TITLE_MAX), _clean(row.get("n.kind"), 60)
    elif typ == "Preference":
        node["title"] = _clean(row.get("n.statement"), TITLE_MAX)
    else:
        node["title"] = _clean(row.get("n.name"), TITLE_MAX)
    if not node["title"]:
        node["title"] = slug
    return node


def _fetch_graph(transport, graph, need_types, deadline=None):
    """({(Type, slug): node}, {slug: [superseded-by slugs]}, row_limit_hit) for one graph; raises KgError."""
    nodes, hub, limited = {}, {}, []

    def call(name, text):
        if deadline is not None and time.monotonic() > deadline:
            raise KgError(f"graph {graph}: deadline")
        rows = transport(graph, name, text, None)
        if isinstance(rows, list) and len(rows) >= MAX_ROWS:
            limited.append(name)  # the server cut the page: absent nodes may only be missing from it
        return rows

    for typ in _NODE_Q:
        if typ not in LISTED_TYPES and typ not in need_types:
            continue
        name, text = _NODE_Q[typ]
        for row in _node_rows(call(name, text)):
            nodes[(typ, _slug(row))] = _make_node(graph, typ, row)
    for typ, (name, text) in _EDGE_Q.items():
        if typ not in ("supersedes",) and not any(t == typ for t, _ in nodes):
            continue
        for row in call(name, text):
            if not isinstance(row, dict):
                continue
            if typ == "supersedes":
                a, b = _slug(row, "a.slug"), _slug(row, "b.slug")
                if a and b:
                    hub.setdefault(("sup", b), []).append(a)
            else:
                n, p = _slug(row), _slug(row, "p.slug")
                if n and p:
                    hub.setdefault((typ, n), []).append(p)
    for (typ, slug), node in nodes.items():
        projs = sorted(set(hub.get((typ, slug), [])))
        node["project"] = projs[0] if projs else None
        node["projects"] = projs
    return nodes, {s: sorted(set(v)) for (k, s), v in hub.items() if k == "sup"}, bool(limited)


def _pick_successor(cands, nodes):
    for c in cands:
        n = nodes.get(("Decision", c))
        if n and n["status"] == "accepted":
            return c
    return cands[0]


def parse_ref(ref):
    m = _REF.fullmatch(ref) if isinstance(ref, str) else None
    return m.groups() if m else None


def build_export(items, transport, extra_graphs=(), max_listed=MAX_LISTED, deadline=None):
    """(export dict, dangling list, errors list). Read-only; ``transport(graph, name, text, params) -> rows``."""
    wanted, item_refs, item_project = {}, {}, {}
    for it in items:
        key = it.get("key") if isinstance(it, dict) else None
        if not isinstance(key, str):
            continue
        raw = it.get("kg_refs")
        refs = [r for r in (raw if isinstance(raw, (list, tuple)) else []) if parse_ref(r)]
        if refs:
            item_refs[key] = sorted(set(refs))
            item_project[key] = it.get("project")
        for r in refs:
            g, t, _s = parse_ref(r)
            wanted.setdefault(g, set()).add(t)
    graphs = sorted(set(wanted) | {g for g in extra_graphs if g})[:MAX_GRAPHS]
    fetched, errors = {}, []
    for g in graphs:
        if deadline is not None and time.monotonic() > deadline:
            errors.append({"graph": g, "error": "deadline"})
            continue
        try:
            fetched[g] = _fetch_graph(transport, g, wanted.get(g, set()), deadline)
        except KgError as exc:
            errors.append({"graph": g, "error": str(exc)[:200], "missing": bool(exc.missing)})
    failed = sorted(e["graph"] for e in errors)

    def ref_of(n):
        return f"{n['graph']}:{n['type']}:{n['slug']}"

    out, by_ref = {}, {}
    for g, (nodes, sups, _lim) in fetched.items():
        for (typ, slug), node in nodes.items():
            by_ref[f"{g}:{typ}:{slug}"] = (node, sups)
    limited = sorted(g for g, v in fetched.items() if v[2])

    memo = {}

    def current_of(ref):
        """End of the Supersedes chain from *ref*; cycle-safe, memoised so a long chain costs O(n) overall."""
        path, cur = [], ref
        while cur not in memo:
            path.append(cur)
            n = by_ref[cur][0]
            nxt = by_ref[cur][1].get(n["slug"]) if n["type"] == "Decision" else None
            cand = f"{n['graph']}:Decision:{_pick_successor(nxt, fetched[n['graph']][0])}" if nxt else None
            if not cand or cand not in by_ref or cand in path:
                memo[cur] = cur
                break
            cur = cand
        end = memo[cur]
        for r in path:
            memo[r] = end
        return end

    def add(ref):
        stack = [ref]  # iterative: a long supersession chain must not hit the recursion limit
        while stack:
            ref = stack.pop()
            if ref in out or ref not in by_ref:
                continue
            node = dict(by_ref[ref][0])
            out[ref] = node
            cur = current_of(ref)
            node["current"] = cur
            sups = by_ref[ref][1].get(node["slug"])
            if sups:
                nxt = f"{node['graph']}:Decision:{_pick_successor(sups, fetched[node['graph']][0])}"
                node["superseded_by"] = nxt if nxt in by_ref else None
                if node["superseded_by"]:
                    stack.append(nxt)  # intermediate links of a chain must be in the export too
            if cur != ref:
                stack.append(cur)

    dangling, unresolved = {}, set()
    for key, refs in item_refs.items():
        for r in refs:
            g = parse_ref(r)[0]
            if g in failed or g not in fetched or (r not in by_ref and g in limited):
                unresolved.add(g)
            elif r in by_ref:
                add(r)
            else:
                dangling.setdefault(r, []).append(key)

    # per-project knowledge: listed (non-referenced) nodes, capped per graph
    truncated, listed = False, {}
    for g, (nodes, _s, _l) in fetched.items():
        n = 0
        for (typ, slug), node in sorted(nodes.items()):
            if typ not in LISTED_TYPES:
                continue
            if typ == "Decision" and node["status"] not in ("accepted", "proposed"):
                continue
            ref = f"{g}:{typ}:{slug}"
            if ref in out:
                continue
            if n >= max_listed:
                truncated = True
                continue
            n += 1
            add(ref)
            listed[ref] = True

    reverse = {}
    for key, refs in item_refs.items():
        for r in refs:
            for target in {r, out[r]["current"]} if r in out else ():
                reverse.setdefault(target, set()).add(key)
    reverse = {r: sorted(v) for r, v in sorted(reverse.items())}

    projects = {}
    for ref, node in out.items():
        if node["type"] in LISTED_TYPES:
            for proj in node["projects"]:
                projects.setdefault(proj, set()).add(ref)
    for key, refs in item_refs.items():
        proj = item_project.get(key)
        if isinstance(proj, str):
            for r in refs:
                if r in out:
                    projects.setdefault(proj, set()).update({r, out[r]["current"]})
    export = {
        "enabled": True,
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "graphs": sorted(fetched),
        "unresolved_graphs": sorted(unresolved | set(failed)),
        "truncated": truncated or bool(limited),
        "nodes": dict(sorted(out.items())),
        "items": dict(sorted(item_refs.items())),
        "reverse": reverse,
        "projects": {p: {"nodes": sorted(v)} for p, v in sorted(projects.items())},
    }
    dang = [{"ref": r, "items": sorted(set(v))} for r, v in sorted(dangling.items())]
    return export, dang, errors


# --------------------------------------------------------------------------- #
def _atomic_write(path: Path, text: str):
    tmp = path.with_name(f".{path.name}.tmp")  # fixed name: a crash leaves at most one, replaced next run
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def _read_enabled(path):
    """The enabled export on disk as a dict, else None."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("enabled") is True else None


def _read_token(path):
    try:
        text = Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


def run(items, out_dir, env=None, transport=None):
    """Write kg-export.json (+ status) into *out_dir*. Always returns 0: the page must render without it."""
    env = os.environ if env is None else env
    out_dir = Path(out_dir)
    status_path = Path(env.get("PG_KG_STATUS_FILE") or out_dir / STATUS_NAME)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        token = _read_token(env["PG_KG_TOKEN_FILE"]) if env.get("PG_KG_TOKEN_FILE") else None
        if not env.get("PG_KG_URL") or not token:
            _atomic_write(out_dir / EXPORT_NAME, json.dumps({"enabled": False}))
            _atomic_write(status_path, json.dumps({"enabled": False, "generated": now}))
            return 0
        if transport is None:
            transport = OmnigraphClient(env["PG_KG_URL"], token)
        extra = [g.strip() for g in (env.get("PG_KG_GRAPHS") or "").split(",") if g.strip()]
        export, dangling, errors = build_export(
            items, transport, extra, deadline=time.monotonic() + DEADLINE_S)
        ok = not errors
        kept = None
        prev = _read_enabled(out_dir / EXPORT_NAME)
        if prev is not None and any(not e.get("missing") for e in errors):  # transient failure: keep the last good export
            kept = prev
        else:
            from .keeper.credential_guard import mask_all
            export, _n = mask_all(export)
            _atomic_write(out_dir / EXPORT_NAME, json.dumps(export, separators=(",", ":"), sort_keys=True))
        _atomic_write(status_path, json.dumps({
            "enabled": True, "generated": now, "ok": ok, "errors": errors, "kept_previous": kept is not None,
            "graphs": (kept or export).get("graphs", []), "nodes": len((kept or export).get("nodes", {})),
            "dangling": dangling, "unresolved_graphs": (kept or export).get("unresolved_graphs", [])},
            indent=1, sort_keys=True))
    except Exception as exc:  # noqa: BLE001 - the export must never break the render
        print(f"kg-export: skipped ({type(exc).__name__})", file=sys.stderr)
    return 0


def main(argv=None):  # pragma: no cover - thin CLI: python3 -m plangraph.kg_export SNAPSHOT OUT_DIR
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 2:
        print("usage: python3 -m plangraph.kg_export SNAPSHOT.json OUT_DIR", file=sys.stderr)
        return 2
    snap = json.loads(Path(argv[0]).read_text(encoding="utf-8"))
    return run(snap.get("items") or [], argv[1])


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
