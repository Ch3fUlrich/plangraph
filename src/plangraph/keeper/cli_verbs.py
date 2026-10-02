"""CLI verbs: talk to the API with urllib and fall back to the local snapshot.

The token is read from ``PG_TOKEN_FILE`` (mode 600), never from argv and never
printed.  Exit codes follow the contract: 0 ok, 2 refused, 3 unreachable.  On
an unreachable keeper a *read* verb is answered from the local
``plan.snapshot.json`` with :mod:`plangraph.views` and a one-line notice on
stderr; a write verb only exits 3.
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
from pathlib import Path

from .. import views
from . import fmt
from .credential_guard import mask_all
from .cli_local import evidence_ref, local_alerts, local_digest, local_show

__all__ = ["Session", "RefusedCli", "Unreachable", "parse_duration",
           "EXIT_OK", "EXIT_REFUSED", "EXIT_UNREACHABLE"]

EXIT_OK = 0
EXIT_REFUSED = 2
EXIT_UNREACHABLE = 3
TIMEOUT = 15.0
NOTICE = "READ-ONLY fallback (keeper unreachable)"


class RefusedCli(Exception):
    """The API refused a verb (HTTP 4xx)."""

    def __init__(self, code: str, msg: str = ""):
        super().__init__(f"{code}: {msg}" if msg else code)
        self.code = code
        self.msg = msg


class Unreachable(Exception):
    """The keeper could not be reached (connection error or 5xx)."""


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
        self.url = (url or os.environ.get("PG_URL")
                    or "http://127.0.0.1:8765").rstrip("/")
        self.config_path = config_path or os.environ.get("PG_CONFIG")
        self._token = None
        self._config = None

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
    def call(self, method, verb, params=None, body=None):
        query = dict(params or {})
        query.setdefault("fmt", "json")
        query = {k: v for k, v in query.items() if v is not None}
        url = f"{self.url}/v1/{verb}?{urllib.parse.urlencode(query)}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Authorization", f"Bearer {self.token()}")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            code, msg = _parse_error(exc.read(), exc.code)
            if exc.code >= 500:
                raise Unreachable(msg or f"server error {exc.code}") from None
            raise RefusedCli(code, msg) from None
        except (urllib.error.URLError, http.client.HTTPException,
                TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise Unreachable(str(reason)) from None
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
        return Path(cfg.snapshot) if cfg else Path("plan.snapshot.json")

    def local_snapshot(self) -> dict:
        path = self.snapshot_path()
        if not path.is_file():
            raise Unreachable(f"no local snapshot at {path}")
        return json.loads(path.read_text(encoding="utf-8"))

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
        return _emit(session, session.call("GET", verb, params), renderer)
    except RefusedCli as exc:
        return _refuse(exc)
    except Unreachable as exc:
        if local is None:
            print(f"keeper unreachable: {exc}", file=sys.stderr)
            return EXIT_UNREACHABLE
        print(NOTICE, file=sys.stderr)
        return local()


def write(session, verb, body, renderer=None) -> int:
    try:
        return _emit(session, session.call("POST", verb, body=body),
                     renderer or (lambda r: fmt.result_text(verb, r)))
    except RefusedCli as exc:
        return _refuse(exc)
    except Unreachable as exc:
        print(f"keeper unreachable: {exc}", file=sys.stderr)
        return EXIT_UNREACHABLE


def _epoch(session, key) -> int:
    return int(session.call("GET", "show", {"key": key})["epoch"])


# --------------------------------------------------------------------------- #
# read verbs
# --------------------------------------------------------------------------- #

def cmd_ready(args, session):
    if getattr(args, "brief", False):
        session.json_mode = False
    n = None if args.all else args.n

    def local():
        return _emit(session, session.local_items(),
                     lambda items: fmt.ready_text(items, args.project, n))
    return read(session, "ready", {"project": args.project, "n": n,
                                   "all": 1 if args.all else None},
                lambda r: fmt.ready_text(r["items"], args.project, n,
                                          r.get("unblocks")), local)


def cmd_show(args, session):
    def local():
        try:
            data = local_show(session.local_items(), args.key)
        except KeyError:
            print(f"not found in the local snapshot: {args.key}", file=sys.stderr)
            return EXIT_REFUSED
        return _emit(session, data, fmt.show_text)
    return read(session, "show", {"key": args.key},
                lambda r: fmt.show_text(r), local)


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


def cmd_whoami(args, session):
    return read(session, "whoami", {}, lambda r: fmt.whoami_text(r))


# --------------------------------------------------------------------------- #
# write verbs
# --------------------------------------------------------------------------- #

def cmd_claim(args, session):
    body = {"key": args.key}
    if args.lease:
        body["lease_s"] = parse_duration(args.lease)
    return write(session, "claim", body, lambda r: fmt.result_text("claim", r))


def cmd_delegate(args, session):
    return write(session, "delegate", {"key": args.key, "worker": args.worker})


def cmd_heartbeat(args, session):
    return write(session, "heartbeat",
                 {"key": args.key, "epoch": _epoch(session, args.key)})


def cmd_release(args, session):
    return write(session, "release",
                 {"key": args.key, "epoch": _epoch(session, args.key)})


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


def cmd_close(args, session):
    return write(session, "close_on_decision",
                 {"key": args.key, "batch_ref": args.batch},
                 lambda r: f"{r.get('key')} closed {r.get('state')}")


def cmd_legacy(args, session):
    return write(session, "set_legacy_state",
                 {"key": args.key, "state": args.state, "note": args.note},
                 lambda r: f"{r.get('key')} -> {r.get('state')}")


def cmd_priority(args, session):
    return write(session, "set_priority", {"key": args.key, "n": args.n},
                 lambda r: f"{r.get('key')} priority {r.get('priority')}")


def cmd_define(args, session):
    payload = json.loads(Path(args.file).read_text(encoding="utf-8"))
    if args.edit:
        fields = payload.get("fields", payload) if isinstance(payload, dict) else payload
        return write(session, "edit_definition", {"key": args.key, "fields": fields})
    items = payload.get("items") if isinstance(payload, dict) else payload
    tier = getattr(args, "tier", None)
    if tier:
        for definition in (items if isinstance(items, list) else [items]):
            if isinstance(definition, dict):
                definition["review_tier"] = tier
    return write(session, "create_items", {"items": items},
                 lambda r: f"created {len(r.get('created') or [])} items")


def cmd_render(args, session):
    from .. import render as render_mod
    try:
        snapshot = session.call("GET", "snapshot")
    except RefusedCli as exc:
        return _refuse(exc)
    except Unreachable:
        print(NOTICE, file=sys.stderr)
        try:
            snapshot = session.local_snapshot()
        except Unreachable as exc:
            print(f"keeper unreachable: {exc}", file=sys.stderr)
            return EXIT_UNREACHABLE
    snapshot, masked = mask_all(snapshot)  # D-374: last line of defence
    if masked:
        print(f"render: masked {masked} credential-shaped string(s)",
              file=sys.stderr)
    document = render_mod.render(snapshot, render_mod.default_vendor_dir(),
                                 cdn=args.cdn)
    out = Path(args.out)
    if out.parent and not out.parent.exists():
        out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(document, encoding="utf-8")
    return EXIT_OK
