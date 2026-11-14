"""Glue between the planner modules and a keeper ``Store`` (design 4.2 - 4.4).

Everything here is portable: reads go through ``store.conn.execute`` with ``?``
placeholders (SQLite and Postgres) and writes use the store verbs ``put_card``,
``set_state`` and ``ask_question``.

* :func:`running_write_sets`: write sets of the cards whose item is ``running``.
* :func:`gate_card`: route the card, run the readiness gate and move the item to
  ``ready`` or ``needs_input`` (with one planner question listing the findings).
* :func:`route_for_card` / :func:`pack_for_card`: routing and context pack for a stored card.
* :func:`import_batch`: read the cards of a batch YAML and store them.

Note on ``risk_tags``: the keeper stores ``'[]'`` for both "absent" and "reviewed, none",
so a stored card always reads back with a list.  The readiness code ``no_risk_tags`` is
therefore reachable only through direct ``readiness.check_card`` calls; ``gate_card``
passes the stored value unchanged.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Collection, Sequence

from . import card as card_mod
from . import context_pack, readiness, routing
from .legs import DEFAULT_LEGS, Leg

__all__ = ["GATEABLE_STATES", "GateResult", "running_write_sets", "gate_card",
           "route_for_card", "pack_for_card", "import_batch"]

#: Item states the gate may act on; any other state is left alone.
GATEABLE_STATES = ("open", "needs_input", "ready")
_QUESTION_CAP = 2000
_REASON_CAP = 500


@dataclass(frozen=True)
class GateResult:
    """Outcome of :func:`gate_card`.

    ``acted`` is False when the item state is not gateable (nothing was written).
    ``state`` is the item state after the call; ``question_id`` is the open question
    that carries the findings (new or already there), ``None`` when the card is ready.
    """

    key: str
    verdict: readiness.Verdict
    route: routing.Route | None
    route_error: str | None
    acted: bool
    state: str | None
    question_id: int | None

    @property
    def ready(self) -> bool:
        return self.verdict.ready


def _load_list(value: Any) -> list:
    try:
        parsed = json.loads(value) if isinstance(value, str) else (value or [])
    except (ValueError, TypeError):
        return []
    return parsed if isinstance(parsed, list) else []


def running_write_sets(store: Any, project: str,
                       exclude: str | None = None) -> dict[str, list[str]]:
    """Write sets of the cards of ``project`` whose item state is ``running``."""
    rows = store.conn.execute(
        "SELECT c.key, c.write_set FROM cards c JOIN items i ON i.key = c.key"
        " WHERE i.project = ? AND i.state = 'running' ORDER BY c.key", (project,)).fetchall()
    return {row[0]: [str(p) for p in _load_list(row[1])] for row in rows
            if row[0] != exclude}


def _item_state(store: Any, key: str) -> str | None:
    row = store.conn.execute("SELECT state FROM items WHERE key = ?", (key,)).fetchone()
    return None if row is None else row[0]


def _question_text(verdict: readiness.Verdict) -> str:
    lines = [verdict.summary()]
    lines += [f"{f.code}: {f.message}" for f in verdict.findings]
    return "\n".join(lines)[:_QUESTION_CAP]


def _open_question(store: Any, key: str, text: str) -> int | None:
    row = store.conn.execute(
        "SELECT id FROM questions WHERE key = ? AND state = 'open' AND text = ?"
        " ORDER BY id LIMIT 1", (key, text)).fetchone()
    return None if row is None else row[0]


def gate_card(store: Any, principal: Any, key: str, *, repo_root: Path | str,
              legs: Sequence[Leg] = DEFAULT_LEGS,
              dry_run: Callable[[str, Path], tuple[bool, str]] | None = None,
              subtasks: int = 1, unavailable: Collection[str] = (),
              require_heldout: bool = False) -> GateResult:
    """Gate the stored card of ``key`` and move its item to ``ready`` or ``needs_input``.

    A route failure adds a ``no_route`` finding.  A context pack above the first leg's
    ``max_request_tokens`` adds ``leg_request_too_big`` (built best effort: a pack that
    cannot be built at all is skipped, a ``PackTooBig`` is reported).  Only items in
    ``GATEABLE_STATES`` are touched.  Not ready: ``needs_input`` plus one open question
    without default (the planner queue); an identical open question is reused, so a
    repeat call writes nothing.
    """
    card = store.get_card(principal, key)
    project = key.split(":", 1)[0]
    route: routing.Route | None = None
    route_error: str | None = None
    try:
        route = routing.route_card(card, legs=legs, subtasks=subtasks,
                                   unavailable=unavailable)
    except ValueError as exc:  # RouteError is a ValueError
        route_error = str(exc)
    leg_max = route.first_leg.max_request_tokens if route is not None else None
    pack_tokens: int | None = None
    pack_error: str | None = None
    if route is not None:
        try:
            pack_tokens = context_pack.build_pack(
                card, repo_root=Path(repo_root), key=key,
                budget_tokens=context_pack.pack_budget(route.window_class)).tokens
        except context_pack.PackTooBig as exc:
            pack_tokens = None
            pack_error = str(exc)
        except (ValueError, OSError):
            pack_tokens = None  # best effort: no pack, no pack finding
    kw: dict[str, Any] = {}
    if dry_run is not None:
        kw["dry_run"] = dry_run
    verdict = readiness.check_card(
        card, key=key, repo_root=Path(repo_root),
        running_write_sets=running_write_sets(store, project, exclude=key),
        leg_max_request=leg_max, pack_tokens=pack_tokens,
        require_heldout=require_heldout, **kw)
    if route_error is not None:
        verdict = readiness.Verdict(
            key, verdict.findings + (readiness.Finding("no_route", route_error),))
    if pack_error is not None:
        verdict = readiness.Verdict(
            key, verdict.findings + (
                readiness.Finding("leg_request_too_big", f"pack too big: {pack_error}"),))

    state = _item_state(store, key)
    owned = store.conn.execute("SELECT exec_run_id FROM items WHERE key=?", (key,)).fetchone()
    if state == "needs_input" and owned is not None and owned[0] is not None:
        return GateResult(key, verdict, route, route_error, False, state, None)
    if state not in GATEABLE_STATES:
        return GateResult(key, verdict, route, route_error, False, state, None)
    if verdict.ready:
        if state != "ready":
            store.set_state(principal, key, "ready", reason="readiness gate ok")
        return GateResult(key, verdict, route, route_error, True, "ready", None)
    store.set_state(principal, key, "needs_input", reason=verdict.summary()[:_REASON_CAP])
    text = _question_text(verdict)
    qid = _open_question(store, key, text)
    if qid is None:
        qid = store.ask_question(principal, key, text, default_answer=None,
                                 risk_tags=list(card.get("risk_tags") or []))["id"]
    return GateResult(key, verdict, route, route_error, True, "needs_input", qid)


def route_for_card(store: Any, principal: Any, key: str, **route_kw: Any) -> routing.Route:
    """Route the stored card of ``key`` (``route_kw`` goes to ``routing.route_card``)."""
    return routing.route_card(store.get_card(principal, key), **route_kw)


def pack_for_card(store: Any, principal: Any, key: str, *, repo_root: Path | str,
                  window_class: str | None = None,
                  check_tail: str | None = None) -> context_pack.Pack:
    """Build the context pack of the stored card of ``key``.

    The budget comes from ``window_class`` or, failing that, the card's own class;
    ``ValueError`` when neither is set.
    """
    card = store.get_card(principal, key)
    window = window_class or card.get("window_class")
    if window is None:
        raise ValueError("window_class is required: neither given nor set on the card")
    return context_pack.build_pack(
        card, repo_root=Path(repo_root), key=key,
        budget_tokens=context_pack.pack_budget(window), check_tail=check_tail)


def import_batch(store: Any, principal: Any, path: Path | str,
                 default_project: str | None = None) -> list[str]:
    """Store the cards of the batch YAML at ``path``; return the keys written.

    ``default_project`` qualifies bare card ids (otherwise the batch ``project`` does).
    """
    if default_project is None:
        cards = card_mod.load_cards_yaml(path)
    else:
        import yaml

        with open(path, encoding="utf-8") as handle:
            cards = card_mod.cards_from_batch(yaml.safe_load(handle), default_project)
    return card_mod.apply_cards(store, principal, cards)


def _plan_convert_check(snapshot, batch):
    """Replay on disposable SQLite with the keeper's real validators and verbs."""
    import tempfile
    from ..keeper import auth, db
    from ..keeper.errors import Refused
    from ..keeper.store import Store
    from ..keeper.store_create import check_seat_clause_tier
    from ..keeper.events import event_hash, canonical
    seq = snapshot.get("head_seq", snapshot.get("seq", snapshot.get("last_seq")))
    base = batch["base_seq"]
    if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
        raise Refused("input", "cards snapshot needs a non-negative seq")
    if not isinstance(base, int) or isinstance(base, bool) or base < 0:
        raise Refused("input", "base_seq must be a non-negative integer")
    if seq > base and "item_seqs" not in snapshot:
        raise Refused("conflict", "cards snapshot is newer than base_seq; item_seqs required")
    if seq < base:
        raise Refused("conflict", "cards snapshot predates base_seq")
    if seq > base and any(item.get("key", f"{item['project']}:{item['id']}")
                          not in snapshot["item_seqs"] for item in snapshot["items"]):
        raise Refused("conflict", "item_seqs must cover every card when snapshot is newer than base_seq")
    with tempfile.TemporaryDirectory(prefix="pg-plan-convert-") as directory:
        conn = db.open_db(Path(directory) / "scratch.db", create=True, backend="sqlite")
        try:
            principal = auth.authenticate(conn, auth.add_principal(
                conn, batch["principal"], "router", []))
            store = Store(conn)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(items)")}
            for item in snapshot["items"]:
                row = {k: (canonical(v) if isinstance(v, (dict, list)) else v)
                       for k, v in item.items() if k in columns and v is not None}
                row.setdefault("key", f"{item['project']}:{item['id']}")
                names = list(row)
                conn.execute(f"INSERT INTO items({','.join(names)}) VALUES"
                             f"({','.join('?' for _ in names)})", tuple(row[k] for k in names))
            for item in snapshot["items"]:
                key = item.get("key", f"{item['project']}:{item['id']}")
                for dep in item.get("depends_on") or []:
                    conn.execute("INSERT INTO edges(src,dst,rel) VALUES(?,?,'depends_on')", (key, dep))
            # Only event ordering is needed by the real optimistic precondition.
            # Snapshots at base need no synthetic mutations; newer snapshots must
            # provide their per-key event high-water marks, never timestamps.
            for key, changed_seq in sorted(snapshot.get("item_seqs", {}).items(), key=lambda p: p[1]):
                if type(changed_seq) is not int or changed_seq < 0 or changed_seq > seq:
                    raise Refused("input", "item_seqs outside snapshot seq")
                if changed_seq <= base:
                    continue
                body = dict(ts="2000-01-01T00:00:00Z", actor=principal.name,
                            role="router", model=None, key=key, type="item_defined", payload={})
                conn.execute("INSERT INTO events(seq,ts,actor,role,model,key,type,payload,prev_hash,hash)"
                             " VALUES(?,?,?,?,?,?,?,?,?,?)", (changed_seq, body["ts"], principal.name,
                             "router", None, key, "item_defined", "{}", "", event_hash("", body)))
            # DONE locks depend on completion events, not the cached state alone.
            # Negative sequences preserve that history without inventing changes
            # after the snapshot base. No event-chain proof is claimed here.
            for index, item in enumerate(snapshot["items"]):
                if item.get("state") != "done":
                    continue
                key = item.get("key", f"{item['project']}:{item['id']}")
                conn.execute("INSERT INTO events(seq,ts,actor,role,key,type,payload,prev_hash,hash)"
                             " VALUES(?,?,?,?,?,'review_passed','{}','','offline-lock')",
                             (-index - 1, "2000-01-01T00:00:00Z", principal.name, "router", key))
            conn.commit()
            for op in batch["ops"]:
                if op["op"] == "edit":
                    before = store._item(op["key"])
                    if before and any(k in op["fields"] for k in ("done_criteria", "review_tier")):
                        check_seat_clause_tier(op["fields"].get("done_criteria", before["done_criteria"]),
                                               op["fields"].get("review_tier", before["review_tier"]))
            result = store.apply_ops(principal, batch["ops"], base_seq=base, dry_run=True)
            for entry in result["results"]:
                for warning in entry.get("warnings", []):
                    if warning.startswith("dropped cancelled dependency "):
                        raise Refused("depends_on_cancelled", warning)
        finally:
            conn.close()


def convert_plan(args):
    """Thin YAML/CLI adapter to Rust generation and offline keeper replay."""
    import hashlib
    import os
    import subprocess
    import sys
    import tempfile
    import yaml
    from ..keeper.errors import Refused
    root = Path(__file__).resolve().parents[3]
    sys.path.insert(0, str(root / "tools"))
    from apply_batch import _DupLoader, BatchError
    try:
        directory = Path(args.plan_dir).resolve()
        cards = Path(args.cards).resolve()
        files = [directory / name for name in (args.data or ["items.yaml", "changes.yaml"])]
        # Reuse the batch loader's duplicate-key refusal for every plan document.
        inputs = {str(path.resolve()): path.read_bytes() for path in [*files, cards]}
        documents = [{"path": str(path.resolve()), "data": yaml.load(
            inputs[str(path.resolve())].decode("utf-8"), Loader=_DupLoader)} for path in files]
        snapshot = json.loads(inputs[str(cards)])
        request = dict(project=args.project, principal=args.principal,
                       batch=args.batch or directory.name, snapshot=snapshot,
                       base_seq=args.base_seq if args.base_seq is not None else snapshot.get("head_seq", snapshot.get("seq")),
                       documents=documents, input_files=list(inputs),
                       input_hashes={p: hashlib.sha256(b).hexdigest() for p, b in inputs.items()})
        binary = os.environ.get("PG_PLAN_CONVERT_BIN")
        command = [binary] if binary else ["cargo", "run", "--offline", "--quiet", "--manifest-path",
            str(root / "rust/Cargo.toml"), "-p", "pg-worker-core", "--bin", "pg-plan-convert"]
        result = subprocess.run(command, input=json.dumps(request), text=True, capture_output=True)
        if result.returncode:
            print(result.stderr.strip(), file=sys.stderr)
            return 2
        generated = json.loads(result.stdout)
        _plan_convert_check(snapshot, generated["batch"])
        # Rust canonical serialization is the exact byte format hashed by the batch.
        manifest = json.dumps(generated["manifest"], sort_keys=True, separators=(",", ":"),
                              ensure_ascii=False).encode()
        if hashlib.sha256(manifest).hexdigest() != generated["batch"]["source_sha256"]:
            raise Refused("manifest", "Rust/Python canonical serialization mismatch")
        output = Path(args.output).resolve()
        if output.exists():
            raise Refused("output_exists", "output directory already exists; choose a fresh path")
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".plan-convert-", dir=output.parent) as temporary:
            staging = Path(temporary) / "result"
            staging.mkdir()
            (staging / "manifest.json").write_bytes(manifest)
            (staging / "batch.yaml").write_text(yaml.safe_dump(generated["batch"], sort_keys=False), encoding="utf-8")
            staging.rename(output)
        print(json.dumps({"batch": str(output / "batch.yaml"),
                          "manifest": str(output / "manifest.json"),
                          "ops": len(generated["batch"]["ops"]), "offline": "passed"}))
        return 0
    except (OSError, ValueError, KeyError, TypeError, yaml.YAMLError, BatchError, Refused) as exc:
        print(f"{getattr(exc, 'code', 'input')}: {getattr(exc, 'msg', str(exc))}", file=sys.stderr)
        return 2
