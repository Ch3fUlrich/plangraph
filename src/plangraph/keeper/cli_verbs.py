"""CLI verbs: talk to the API with urllib and fall back to the local snapshot.

The token is read from ``PG_TOKEN_FILE`` (mode 600), never from argv and never
printed.  The keeper URL comes from ``--url`` or ``PG_URL``, resolved at call
time; there is no built-in default (D-A=2).  Exit codes: 0 ok, 2 refused,
3 unreachable. Unreachable reads use the local ``plan.snapshot.json`` (signature
checked when configured) + a stderr notice; unreachable writes only exit 3.
"""
from __future__ import annotations

import http.client
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from .. import views
from . import brief as brief_mod, fmt, outbox
from .snapshot_io import SignError, read_snapshot
from .cli_local import evidence_ref, local_alerts, local_digest, local_show

__all__ = ["Session", "RefusedCli", "MissingKeeperURL", "Unreachable",
           "parse_duration", "EXIT_OK", "EXIT_REFUSED", "EXIT_UNREACHABLE"]

EXIT_OK = 0
EXIT_REFUSED = 2
EXIT_UNREACHABLE = 3
TIMEOUT = 15.0
SLOW_TIMEOUT = 120.0  # close_merged reads git and the decisions log before it commits
#: seconds on top of the keeper's claim preflight budget for the write that follows it.
CLAIM_COMMIT_MARGIN_S = 15.0
CLAIM_VERBS = frozenset({"claim", "claim_batch"})
#: reads that build the whole plan view on the daemon (ready: 24 s measured on a
#: 4285-item plan, behind the keeper lock); the 15 s default gave up on a read
#: the daemon then finished.
SLOW_READS = frozenset({"ready", "blocked", "waves"})
NOTICE = "READ-ONLY fallback (keeper unreachable)"


class RefusedCli(Exception):
    """The API refused a verb (HTTP 4xx)."""

    def __init__(self, code: str, msg: str = ""):
        super().__init__(f"{code}: {msg}" if msg else code)
        self.code = code
        self.msg = msg


class Unreachable(Exception):
    """The keeper could not be reached (connection error or 5xx)."""

    def __init__(self, message, *, idempotency_key=None):
        super().__init__(message)
        self.idempotency_key = idempotency_key


class MissingKeeperURL(RefusedCli):
    """No keeper URL configured (D-A=2: the client has no default port)."""

    def __init__(self):
        super().__init__("no keeper URL: pass --url or set PG_URL")


def parse_duration(value) -> int:
    """``2h``/``30m``/``90s``/``7200`` -> seconds."""
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip().lower()
    if text.isdigit():
        return int(text)
    match = re.fullmatch(r"(\d+(?:\.\d+)?)([smhd])", text)
    if not match:
        raise ValueError(f"cannot parse duration {value!r}")
    scale = {"s": 1, "m": 60, "h": 3600, "d": 86400}[match.group(2)]
    return int(float(match.group(1)) * scale)


def _parse_error(raw, status):
    try:
        data = json.loads(raw.decode("utf-8"))
        return data.get("error", str(status)), data.get("msg", "")
    except (ValueError, AttributeError):
        return str(status), ""


class Session:
    """One CLI invocation: URL, token file and (for fallback) config path."""

    def __init__(self, json_mode=False, config_path=None, url=None):
        self.json_mode = bool(json_mode)
        self._url = url
        self.config_path = config_path or os.environ.get("PG_CONFIG")
        self._token = None
        self._config = None

    @property
    def url(self):
        url = self._url or os.environ.get("PG_URL")
        if not url:
            raise MissingKeeperURL()
        return url.rstrip("/")

    # --------------------------------------------------------------- token
    def token(self):
        if self._token is None:
            path = os.environ.get("PG_TOKEN_FILE")
            if not path:
                raise Unreachable("PG_TOKEN_FILE is not set")
            try:
                self._token = Path(path).read_text(encoding="utf-8").strip()
            except OSError as exc:
                raise Unreachable(f"cannot read token file: {exc}") from exc
        return self._token

    # ---------------------------------------------------------------- HTTP
    def claim_timeout(self) -> float:
        """The bound for a claim: the keeper's own claim budget plus its commit margin.

        The budget comes from the keeper's open health answer, so a keeper configured
        with a longer preflight is not given up on while it is still answering; a
        keeper that does not answer gets the default budget.
        """
        from .timing import DEFAULT_CLAIM_PREFLIGHT_S

        budget = DEFAULT_CLAIM_PREFLIGHT_S
        try:
            with urllib.request.urlopen(f"{self.url}/v1/health", timeout=TIMEOUT) as resp:
                value = json.loads(resp.read()).get("claim_preflight_s")
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
                budget = value
        except (OSError, ValueError, AttributeError, http.client.HTTPException):
            pass
        return budget + CLAIM_COMMIT_MARGIN_S

    def call(self, method, verb, params=None, body=None, timeout=None, *, idempotency_key=None):
        if method == "POST":
            from .api_routes import RETRYABLE_WRITES, check_write_key
            from .errors import Refused
            if verb in RETRYABLE_WRITES:
                if idempotency_key is None:
                    idempotency_key = os.environ.get("PG_IDEMPOTENCY_KEY") or uuid.uuid4().hex
                try:
                    check_write_key(idempotency_key)
                except Refused as exc:
                    raise RefusedCli(exc.code, exc.msg) from None
        if timeout is None and method == "POST" and verb in CLAIM_VERBS:
            timeout = self.claim_timeout()
        query = dict(params or {})
        query.setdefault("fmt", "json")
        query = {k: v for k, v in query.items() if v is not None}
        url = f"{self.url}/v1/{verb}?{urllib.parse.urlencode(query)}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Authorization", f"Bearer {self.token()}")
        if idempotency_key is not None:
            request.add_header("Idempotency-Key", idempotency_key)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=timeout or TIMEOUT) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            code, msg = _parse_error(exc.read(), exc.code)
            if exc.code >= 500:
                raise Unreachable(msg or f"server error {exc.code}",
                                  idempotency_key=idempotency_key) from None
            raise RefusedCli(code, msg) from None
        except (urllib.error.URLError, http.client.HTTPException,
                TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise Unreachable(str(reason), idempotency_key=idempotency_key) from None
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError:
            return {"raw": raw.decode("utf-8", "replace")}

    # ------------------------------------------------------------ fallback
    @property
    def config(self):
        if self._config is None:
            from . import config as config_mod
            if self.config_path:
                self._config = config_mod.load_config(self.config_path)
            else:
                self._config = False
        return self._config or None

    def snapshot_path(self) -> Path:
        cfg = self.config
        # No client config: the daemon's own default file, never the cwd.
        if cfg:
            return Path(cfg.snapshot)
        from . import config as config_mod
        return config_mod.default_snapshot()

    def local_snapshot(self) -> dict:
        try:   # M2B: the signature must verify when a snapshot_pubkey is configured
            snap = read_snapshot(self.snapshot_path(), getattr(self.config, "snapshot_pubkey", None))
        except (OSError, SignError) as exc:
            raise Unreachable(f"no usable local snapshot at {self.snapshot_path()}: {exc}") from exc
        from .snapshot_pull import behind_mark   # PG-LOCAL-COPY: a half-installed pull is not served
        if behind_mark(self.snapshot_path(), snap):
            raise Unreachable(f"local copy at {self.snapshot_path()} is older than the last pull; run pg sync pull")
        return snap

    def local_items(self):
        return self.local_snapshot().get("items") or []


# --------------------------------------------------------------------------- #
# output helpers
# --------------------------------------------------------------------------- #

def _emit(session, value, renderer) -> int:
    if session.json_mode:
        print(json.dumps(value))
    else:
        print(renderer(value) if callable(renderer) else value)
    return EXIT_OK


def _refuse(exc) -> int:
    print(str(exc), file=sys.stderr)
    return EXIT_REFUSED


def read(session, verb, params, renderer, local=None) -> int:
    """A read verb; falls back to the local snapshot when unreachable."""
    try:
        return _emit(session, session.call(
            "GET", verb, params, timeout=SLOW_TIMEOUT if verb in SLOW_READS else None), renderer)
    except RefusedCli as exc:
        return _refuse(exc)
    except Unreachable as exc:
        if local is None:
            print(f"keeper unreachable: {exc}", file=sys.stderr)
            return EXIT_UNREACHABLE
        print(NOTICE, file=sys.stderr)
        try:
            return local()
        except Unreachable as inner:   # no readable copy either: name it, no traceback
            print(f"keeper unreachable: {exc}; {inner}", file=sys.stderr)
            return EXIT_UNREACHABLE


def write(session, verb, body, renderer=None) -> int:
    try:
        if verb in outbox.QUEUE_VERBS and os.environ.get("PG_TOKEN_FILE") \
                and session.token().startswith("pgr1_"):
            code, result = outbox.write(session, verb, body)
            if code:
                return code
        else:
            result = session.call("POST", verb, body=body)
        status = _emit(session, result, renderer or (lambda r: fmt.result_text(verb, r)))
        if isinstance(result, dict) and (result.get("refused") or (
                verb in ("claim_batch", "heartbeat_batch")
                and any("error" in row for row in result["results"]))):
            return EXIT_REFUSED
        return status
    except RefusedCli as exc:
        return _refuse(exc)
    except Unreachable as exc:
        if exc.idempotency_key and outbox.queue_http_heartbeat(verb, body, exc.idempotency_key):
            return outbox.EXIT_QUEUED
        if exc.idempotency_key:
            exc = Unreachable(f"{exc}; outcome unknown, repeat with PG_IDEMPOTENCY_KEY={exc.idempotency_key}")
        return outbox.on_unreachable(exc, exit_unreachable=EXIT_UNREACHABLE)


def _epoch(session, key) -> int:
    return int(os.environ.get("PG_EPOCH") or session.call("GET", "show", {"key": key})["epoch"])


# --------------------------------------------------------------------------- #
# read verbs
# --------------------------------------------------------------------------- #

def cmd_ready(args, session):
    if getattr(args, "brief", False):
        session.json_mode = False
    n = None if args.all else args.n

    owner = getattr(args, "owner", None)
    mine = bool(getattr(args, "mine", False))
    agent = bool(getattr(args, "agent", False))

    def local():
        # Offline (snapshot) fallback: --mine needs the server's whoami, so it
        # falls back to no owner filter there; --owner and --agent still apply.
        return _emit(session, session.local_items(),
                     lambda items: fmt.ready_text(items, args.project, n,
                                                  owner=owner, agent=agent))
    if getattr(args, "needs_reconcile", False):
        try:
            result = session.call("GET", "ready", {"project": args.project, "n": n,
                "owner": owner, "mine": 1 if mine else None,
                "agent": 1 if agent else None}, timeout=SLOW_TIMEOUT)
            return _emit(session, {"needs_reconcile": result["needs_reconcile"]},
                         lambda r: fmt.ready_text([], server_items=True,
                             needs_reconcile=r["needs_reconcile"]))
        except RefusedCli as exc:
            return _refuse(exc)
        except Unreachable as exc:
            print(f"keeper unreachable: {exc}", file=sys.stderr)
            return EXIT_UNREACHABLE
    return read(session, "ready", {"project": args.project, "n": n,
                                   "all": 1 if args.all else None,
                                   "owner": owner, "mine": 1 if mine else None,
                                   "agent": 1 if agent else None},
                lambda r: fmt.ready_text(r["items"], unblocks=r.get("unblocks"),
                                         server_items=True,
                                         needs_reconcile=r.get("needs_reconcile"),
                                         in_progress=r.get("in_progress"),
                                         unknown=r.get("unknown"),
                                         warnings=r.get("warnings")), local)


def cmd_show(args, session):
    render = brief_mod.brief_text if args.brief else fmt.show_text
    def local():
        try:
            data = local_show(session.local_items(), args.key,
                              session.local_snapshot().get("aliases"))
        except KeyError:
            print(f"not found in the local snapshot: {args.key}", file=sys.stderr)
            return EXIT_REFUSED
        if args.brief:
            data = brief_mod.build(data, data.get("depends_on"))
        return _emit(session, data, render)
    params = {"key": args.key, "brief": 1 if args.brief else None}
    return read(session, "show", params, lambda r: render(r), local)


def cmd_blocked(args, session):
    def local():
        return _emit(session,
                     views.blocked(session.local_items(), project=args.project),
                     fmt.blocked_text)
    return read(session, "blocked", {"project": args.project},
                lambda r: fmt.blocked_text(r), local)


def cmd_waves(args, session):
    def local():
        return _emit(session, views.waves(session.local_items()), fmt.waves_text)
    return read(session, "waves", {}, lambda r: fmt.waves_text(r), local)


def cmd_critical(args, session):
    def local():
        return _emit(session, views.critical_path(session.local_items(),
                                                  project=args.project),
                     fmt.critical_text)
    return read(session, "critical_path", {"project": args.project},
                lambda r: fmt.critical_text(r), local)


def cmd_percent(args, session):
    def local():
        return _emit(session, views.percent_done(session.local_items(),
                                                 by=args.by,
                                                 project=args.project),
                     fmt.percent_text)
    return read(session, "percent", {"project": args.project, "by": args.by},
                lambda r: fmt.percent_text(r), local)


def cmd_alerts(args, session):
    def local():
        return _emit(session, local_alerts(session.local_items(), args.project),
                     fmt.alerts_text)
    return read(session, "alerts", {"project": args.project},
                lambda r: fmt.alerts_text(r), local)


def cmd_digest(args, session):
    def local():
        return _emit(session, local_digest(session.local_items()), fmt.digest_text)
    return read(session, "digest", {}, lambda r: fmt.digest_text(r), local)


def cmd_snapshot(args, session):
    def local():
        return _emit(session, session.local_snapshot(), fmt.snapshot_text)
    return read(session, "snapshot", {}, lambda r: fmt.snapshot_text(r), local)


def _sweep_claim_paths(card, state):
    """Keeper run_ids bind local spawner metadata to an authoritative active claim."""
    metadata = [card.get("claim"), card.get("claim_metadata")]
    ids = card.get("run_ids")
    if not isinstance(ids, list):
        raise ValueError("claim run metadata unavailable")
    for rid in ids:
        if not isinstance(rid, str) or rid in (".", "..") or Path(rid).name != rid or not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", rid):
            raise ValueError("invalid claim run id")
        record = state / "runs" / rid / "record.json"
        try:
            row = json.loads(record.read_text(encoding="utf-8"))
        except FileNotFoundError:
            if record.parent.exists():
                raise ValueError("claim run record unavailable") from None
            continue  # Runs belonging to another host have no local metadata.
        if not isinstance(row, dict) or not isinstance(row.get("status"), str):
            raise ValueError("invalid claim run record")
        metadata.append(row)
        worktree = row.get("run_worktree")
        if worktree is not None:
            if not isinstance(worktree, dict) or not isinstance(worktree.get("path"), str):
                raise ValueError("incomplete claim worktree metadata")
            metadata.append(worktree)
        attempts = row.get("worktree_attempts")
        if attempts is None:
            attempts = []
        if not isinstance(attempts, list) or any(not isinstance(a, dict) or not isinstance(a.get("path"), str) for a in attempts):
            raise ValueError("invalid claim worktree attempts")
        metadata.extend(attempts)
    paths = set()
    for meta in metadata:
        if meta is None:
            continue
        if not isinstance(meta, dict):
            raise ValueError("invalid claim metadata")
        for field in ("path", "worktree", "checkout", "cwd"):
            value = meta.get(field)
            if isinstance(value, dict) and field in ("worktree", "checkout"):
                if not isinstance(value.get("path"), str):
                    raise ValueError("incomplete claim checkout metadata")
                value = value["path"]
            if value is not None:
                if not isinstance(value, str) or not Path(value).is_absolute():
                    raise ValueError("invalid claim path")
                paths.add(value)
    return paths


def cmd_sweep_claims(args, session):
    """Fresh all-project protection authority; never use the offline fallback."""
    from .schema import STATES
    try:
        state_value = getattr(args, "state", None)
        if state_value is None:
            state_value = os.environ.get("PG_SWEEP_STATE", str(Path.home() / ".local/state/plangraph"))
        if not state_value:
            raise ValueError("empty sweep state path")
        state = Path(state_value)
        who = session.call("GET", "whoami")
        if who.get("role") not in ("router", "sweep") or who.get("projects") != []:
            raise ValueError("sweep claims needs an unscoped sweep or router token")
        snap = session.call("GET", "snapshot")
        if not isinstance(snap.get("items"), list) or "filter" in snap or type(snap.get("head_seq")) is not int:
            raise ValueError("incomplete snapshot")
        paths, branches = set(), set()
        for item in snap["items"]:
            if (not isinstance(item, dict) or item.get("state") not in STATES
                    or not isinstance(item.get("key"), str) or not item["key"]):
                raise ValueError("incomplete item")
            if item["state"] not in ("claimed", "in_review"):
                continue
            card = session.call("GET", "show", {"key": item["key"]})
            if not isinstance(card, dict) or card.get("key") != item["key"] or card.get("state") not in ("claimed", "in_review"):
                raise ValueError("incomplete active claim response")
            lanes = card.get("work_exists", {}).get("lanes")
            if not isinstance(lanes, list) or len(lanes) >= 5:
                raise ValueError("open lanes unavailable or truncated")
            for lane in lanes:
                if not isinstance(lane, str) or not lane:
                    raise ValueError("invalid lane")
                branches.add(lane)
                # Open lanes may include a remote prefix; export its local name too.
                if not lane.startswith("refs/") and "/" in lane:
                    branches.add(lane.split("/", 1)[1])
            claim_paths = _sweep_claim_paths({**item, **card}, state)
            if not claim_paths and not lanes:
                raise ValueError("active claim has no resolved path or lane")
            paths.update(claim_paths)
        # Refuse a claim set that changed while per-item authority was collected.
        if session.call("GET", "snapshot").get("head_seq") != snap.get("head_seq") or "head_seq" not in snap:
            raise ValueError("keeper changed during claims read")
        print(json.dumps({"paths": sorted(paths), "branches": sorted(branches)}, separators=(",", ":")))
        return EXIT_OK
    except RefusedCli as exc:
        return _refuse(exc)
    except (Unreachable, ValueError, TypeError, AttributeError, OSError) as exc:
        print(f"sweep claims unavailable: {exc}", file=sys.stderr)
        return EXIT_UNREACHABLE


def cmd_whoami(args, session):
    return read(session, "whoami", {}, lambda r: fmt.whoami_text(r))


# --------------------------------------------------------------------------- #
# write verbs
# --------------------------------------------------------------------------- #

def cmd_claim(args, session):
    body = {"key": args.key, **({"force_stale": args.force_stale} if args.force_stale else {})}
    if args.lease:
        body["lease_s"] = parse_duration(args.lease)
    return write(session, "claim", body, lambda r: fmt.result_text("claim", r))


def cmd_claim_batch(args, session):
    body = {"keys": args.keys}
    if args.lease:
        body["lease_s"] = parse_duration(args.lease)
    if args.force_stale is not None:
        body["force_stale"] = args.force_stale
    return write(session, "claim_batch", body)


def cmd_heartbeat_batch(args, session):
    try:
        claims = [{"key": key, "epoch": int(epoch)} for key, epoch in args.claims]
    except ValueError:
        return _refuse(RefusedCli("validation", "epoch must be an integer"))
    return write(session, "heartbeat_batch", {"claims": claims})


def cmd_delegate(args, session):
    return write(session, "delegate", {"key": args.key, "worker": args.worker})


def cmd_heartbeat(args, session):
    return write(session, "heartbeat",
                 {"key": args.key, "epoch": _epoch(session, args.key)})


def cmd_release(args, session):
    return write(session, "release",
                 {"key": args.key, "epoch": _epoch(session, args.key)})


def cmd_incident(args, session):
    return write(session, "incident",
                 {"key": args.key, "text": args.text, "evidence": args.evidence})


def cmd_evidence(args, session):
    ref = evidence_ref(args)
    return write(session, "add_evidence",
                 {"key": args.key, "epoch": _epoch(session, args.key),
                  "kind": args.kind, "ref": ref})


def cmd_submit(args, session):
    return write(session, "submit",
                 {"key": args.key, "epoch": _epoch(session, args.key)})


def cmd_verdict(args, session):
    ok = bool(args.ok)
    if not args.ok and not args.changes:
        print("verdict needs --pass or --changes", file=sys.stderr)
        return EXIT_REFUSED
    return write(session, "verdict",
                 {"key": args.key, "ok": ok,
                  "reason": args.changes or args.reason or ""},
                 lambda r: fmt.result_text("verdict", r))


def cmd_cancel(args, session):
    return write(session, "cancel", {"key": args.key, "reason": args.reason or ""})


def cmd_waive(args, session):
    return write(session, "waive_second_family",
                 {"key": args.key, "decision_id": args.decision_id},
                 lambda r: f"{r.get('key')} waiver {r.get('review_waiver')}")


def cmd_withdraw(args, session):
    return write(session, "withdraw_review",
                 {"key": args.key, "decision_id": args.decision_id},
                 lambda r: f"{r.get('key')} withdrawn from review -> {r.get('state')}")


def cmd_reopen(args, session):
    return write(session, "reopen",
                 {"key": args.key, "decision_id": args.decision_id,
                  "reason": args.reason},
                 lambda r: f"{args.key} reopened -> {r.get('state')} epoch {r.get('epoch')}")


def cmd_set_kind(args, session):
    return write(session, "set_kind", {"key": args.key, "kind": args.kind},
                 lambda r: f"{r.get('key')} kind {r.get('was')} -> {r.get('kind')}")


def cmd_close(args, session):
    if args.batch is None and getattr(args, "decision_ref", None) is None:
        return _refuse(RefusedCli("validation", "one of the arguments --decision-ref --batch is required"))
    if args.batch is not None and getattr(args, "epoch", None) is not None:
        return _refuse(RefusedCli("validation", "--epoch requires --decision-ref, not --batch"))
    return write(session, "close_on_decision",
                 {"key": args.key, "batch_ref": args.batch,
                  "decision_ref": getattr(args, "decision_ref", None),
                  "epoch": getattr(args, "epoch", None)},
                 lambda r: f"{r.get('key')} closed {r.get('state')}")


from .cli_output_verbs import cmd_legacy, cmd_priority, cmd_define, cmd_render  # noqa: E402,F401
