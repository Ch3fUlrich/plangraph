#!/usr/bin/env python3
"""Apply an ordered ops YAML batch through the keeper HTTP API (X7d)."""
import argparse
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.request
from collections import Counter

import yaml

import apply_batch_atomic as atomic

DEFAULT_URL = "http://127.0.0.1:8787"
TIMEOUT = 15
OP_SPECS = {
    "create": (["item"], {}),
    "edit": (["key", "fields"], {"fields": dict}),
    "legacy_state": (["key", "state"], {}),
    "add_evidence": (["key", "kind"], {}),
    "close_on_decision": (["key"], {}),
    "cancel": (["key"], {}),
    "priority": (["key", "priority"], {"priority": int}),
    "rehome": (["key", "new_project"], {"new_project": str}),
    "set_kind": (["key", "kind"], {"kind": str}),
}
OP_ALLOWED = {
    "create": {"op", "item"},
    "edit": {"op", "key", "fields"},
    "legacy_state": {"op", "key", "state", "note"},
    "add_evidence": {"op", "key", "kind", "ref", "repo"},
    "close_on_decision": {"op", "key"},
    "cancel": {"op", "key", "reason"},
    "priority": {"op", "key", "priority"},
    "rehome": {"op", "key", "new_project", "host"},
    "set_kind": {"op", "key", "kind"},
}
TOP_ALLOWED = {"batch", "principal", "source", "source_sha256", "ops"}
PIN_MISMATCH_MARK = "sha256 mismatch"
LEGACY_STATES = ("claimed", "in_review")
# \Z ensures end of string; [^:@/\s] excludes whitespace (including newline)
KEY_RE = re.compile(r"^[^:@/\s]+:[^:@/\s]+\Z")
class BatchError(Exception):
    """A local (pre-flight or per-op) problem: reported as code ``error``."""
class ApiError(Exception):
    """A refusal or transport failure returned by the keeper API."""
    def __init__(self, code, msg):
        super().__init__(f"{code} {msg}")
        self.code = code
        self.msg = msg
def sha256_of(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()
class _DupLoader(yaml.SafeLoader):
    """Safe loader that refuses duplicate mapping keys (nested too)."""
def _dup_mapping(loader, node, deep=False):
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=True)
        try:
            dup = key in mapping
        except TypeError:
            raise BatchError("invalid mapping key") from None
        if dup:
            raise BatchError(f"duplicate key {key!r}")
        mapping[key] = loader.construct_object(value_node, deep=True)
    return mapping
_DupLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _dup_mapping)
def resolve_source(batch_file, source):
    base = os.path.dirname(os.path.realpath(batch_file))
    if not os.path.isabs(source):
        source = os.path.join(base, source)
    return os.path.realpath(source)
def load_batch(path):
    with open(path, "r", encoding="utf-8") as handle:
        try:
            data = yaml.load(handle, Loader=_DupLoader)
        except yaml.YAMLError as exc:
            raise BatchError(f"batch is not valid YAML: {exc}") from None
    if not isinstance(data, dict):
        raise BatchError("batch must be a YAML mapping")
    for key in data:
        if key not in TOP_ALLOWED:
            raise BatchError(f"unknown top-level key {key!r}")
    for field in ("batch", "principal", "source", "source_sha256"):
        if not data.get(field):
            raise BatchError(f"batch is missing {field!r}")
    ops = data.get("ops")
    if not isinstance(ops, list) or not ops:
        raise BatchError("ops must be a non-empty list")
    return data
def op_key(op):
    """Display key: a create lists every item key."""
    if op.get("op") == "create":
        item = op.get("item")
        items = item if isinstance(item, list) else [item]
        keys = [f"{e['project']}:{e['id']}" for e in items
                if isinstance(e, dict) and e.get("project") and e.get("id")]
        return ", ".join(keys) if keys else "-"
    return op.get("key") or "-"
def _check_item(item):
    if not isinstance(item, dict):
        raise BatchError("create item must be a mapping")
    for field in ("project", "id"):
        if not item.get(field):
            raise BatchError(f"create item is missing {field!r}")
def validate_op(op):
    if not isinstance(op, dict) or "op" not in op:
        raise BatchError("op must be a mapping with an 'op' field")
    name = op["op"]
    if not isinstance(name, str):
        raise BatchError(f"op field must be a string, got {type(name).__name__}")
    if name not in OP_SPECS:
        raise BatchError(f"unknown op {name!r}")
    unknown = set(op) - OP_ALLOWED[name]
    if unknown:
        raise BatchError(f"{name} has unknown field(s): "
                         f"{', '.join(sorted(map(str, unknown)))}")
    required, types = OP_SPECS[name]
    for field in required:
        if op.get(field) is None:
            raise BatchError(f"{name} is missing {field!r}")
        if field in types and not isinstance(op[field], types[field]):
            raise BatchError(f"{name}: {field!r} must be "
                             f"{types[field].__name__}")
    if name == "create":
        item = op["item"]
        if isinstance(item, list):
            if not item:
                raise BatchError("create item list is empty")
            for entry in item:
                _check_item(entry)
        else:
            _check_item(item)
    for f in ("kind", "note", "reason", "state", "repo", "key", "host"):
        if f in op and op[f] is not None and not isinstance(op[f], str):
            raise BatchError(f"{name}: {f!r} must be str")
    if op.get("ref") is not None and not isinstance(op.get("ref"), dict):
        raise BatchError("add_evidence: 'ref' must be a mapping")
    if name == "priority" and isinstance(op["priority"], bool):
        raise BatchError("priority: 'priority' must be int")
    if name == "legacy_state" and op["state"] not in LEGACY_STATES:
        raise BatchError(f"legacy_state: state must be one of "
                         f"{', '.join(LEGACY_STATES)}")
    if name == "rehome" and not op["new_project"]:
        raise BatchError("rehome: 'new_project' must be a non-empty string")
    key = op.get("key")
    if key is not None and not KEY_RE.match(str(key)):
        raise BatchError(f"key {key!r} must be 'project:ID'")
def build_request(op, source, digest):
    name = op["op"]
    if name == "create":
        item = op["item"]
        return "create_items", {"items": item if isinstance(item, list)
                                       else [item]}
    if name == "edit":
        return "edit_definition", {"key": op["key"], "fields": op["fields"]}
    if name == "legacy_state":
        return ("set_legacy_state", {"key": op["key"], "state": op["state"],
                                     "note": op.get("note", "")})
    if name == "add_evidence":
        ref = dict(op.get("ref") or {})
        if op.get("repo") is not None: ref["repo"] = op["repo"]
        return "add_evidence_router", {"key": op["key"], "kind": op["kind"],
                                       "ref": ref}
    if name == "close_on_decision":
        return ("close_on_decision",
                {"key": op["key"], "batch_ref": f"{source}@{digest}"})
    if name == "cancel":
        return "cancel", {"key": op["key"], "reason": op.get("reason", "")}
    if name == "priority":
        return "set_priority", {"key": op["key"], "n": op["priority"]}
    if name == "set_kind":
        return "set_kind", {"key": op["key"], "kind": op["kind"]}
    if name == "rehome":
        return "rehome", {"key": op["key"], "new_project": op["new_project"],
                          "host": op.get("host")}
    raise BatchError(f"unknown op {name!r}")
class Session:
    """Bearer-token calls to the keeper API; the token is never printed."""
    def __init__(self, url, token):
        self.url = url.rstrip("/")
        self.token = token
        # Build an opener that does NOT follow redirects and ignores HTTP_PROXY
        class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
            def http_error_302(self, req, fp, code, msg, headers):
                raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)
            http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            NoRedirectHandler()
        )
    def _call(self, verb, body=None):
        try:
            data = json.dumps(body, allow_nan=False).encode("utf-8") if body is not None else None
        except (TypeError, ValueError) as exc:
            raise BatchError(f"cannot encode {verb}: {exc.__class__.__name__}") from None
        request = urllib.request.Request(
            f"{self.url}/v1/{verb}", data=data,
            method="POST" if body is not None else "GET")
        request.add_header("Authorization", f"Bearer {self.token}")
        if body is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with self._opener.open(request, timeout=TIMEOUT) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            code, msg = _parse_error(exc.read(), exc.code)
            raise ApiError(code, msg) from None
        except (urllib.error.URLError, OSError) as exc:
            raise ApiError("unreachable", exc.__class__.__name__) from None
    def post(self, verb, body):
        return self._call(verb, body)
    def get(self, verb):
        try:
            raw = self._call(verb)
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ApiError("unreachable", f"bad whoami reply: {exc.__class__.__name__}") from None
        except ApiError:
            raise
        except Exception as exc:
            raise ApiError("unreachable", exc.__class__.__name__) from None
        if not isinstance(data, dict):
            raise ApiError("unreachable", "bad whoami reply")
        return data
def _parse_error(raw, status):
    try:
        data = json.loads(raw.decode("utf-8"))
        return data.get("error", str(status)), data.get("msg", "")
    except (ValueError, AttributeError):
        return str(status), ""
def read_token(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = handle.read()
    except UnicodeDecodeError as exc:
        raise BatchError(f"cannot read token file: {exc.__class__.__name__}") from None
    except (OSError, TypeError, ValueError) as exc:
        raise BatchError(f"cannot read token file: {exc}") from None
    token = raw.strip()
    if not token:
        raise BatchError("token file is empty")
    # Refuse any token that is not printable ASCII without whitespace
    if not token.isascii() or not token.isprintable() or any(ch.isspace() for ch in token):
        raise BatchError("token contains invalid characters (must be printable ASCII, no whitespace)")
    return token
def print_help(out):
    out.write("usage: apply_batch.py BATCH.yaml --url URL --token-file PATH "
              "[--dry-run] [--from N] [--per-op] [--allow-no-whoami]\n")
def main(argv=None, out=None, err=None):
    args_in = list(sys.argv[1:] if argv is None else argv)
    out = out or sys.stdout
    err = err or sys.stderr
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("batch_file", nargs="?")
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--token-file", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--from", dest="from_n", default=None)
    parser.add_argument("--allow-no-whoami", action="store_true")
    parser.add_argument("--per-op", action="store_true")
    try:
        opts = parser.parse_args(args_in)
    except SystemExit:
        print_help(err)
        return 2
    if not opts.batch_file:
        print_help(err)
        return 2
    start = 1
    if opts.from_n is not None:
        try:
            start = int(opts.from_n)
        except (ValueError, TypeError):
            err.write(f"error: --from must be an integer, "
                      f"got {opts.from_n!r}\n")
            return 2
    def line(text):
        out.write(text + "\n")
        out.flush()
    def fail(n, name, key, code, message):
        line(f"op {n} {name} {key}: {code} {message}")
        line(f"resume with --from {n}")
        return 1
    try:
        data = load_batch(opts.batch_file)
        ops = data["ops"]
        total = len(ops)
        if start < 1 or start > total:
            err.write(f"error: --from must be between 1 and {total}, "
                      f"got {opts.from_n!r}\n")
            return 2
        source = resolve_source(opts.batch_file, data["source"])
        try:
            digest = sha256_of(source)
        except OSError as exc:
            raise BatchError(f"cannot read source file: {exc}") from None
        if digest != data["source_sha256"]:
            line(f"source {source}: {PIN_MISMATCH_MARK} "
                 f"(refusing before any call)")
            return 1
        # Read and validate token early so token errors exit 2
        try:
            token = read_token(opts.token_file)
        except BatchError as exc:
            err.write(f"error: {exc}\n")
            return 2
        # Pre-validate all ops before any execution (dry-run or live)
        for n, op in enumerate(ops, 1):
            if n < start:
                continue
            name = op.get("op") if isinstance(op, dict) else "?"
            key = op_key(op) if isinstance(op, dict) else "-"
            # Initialize verb before try so except block can't see unbound/stale name
            verb = op.get("op") if isinstance(op, dict) else "?"
            try:
                validate_op(op)
                # Also ensure the request body is JSON-encodable
                verb, body = build_request(op, source, digest)
                json.dumps(body, allow_nan=False)
            except BatchError as exc:
                return fail(n, name, key, "error", str(exc))
            except (TypeError, ValueError) as exc:
                return fail(n, name, key, "error", f"cannot encode {verb}: {exc.__class__.__name__}")
        if opts.dry_run:
            for n, op in enumerate(ops, 1):
                if n < start:
                    continue
                name = op.get("op") if isinstance(op, dict) else "?"
                key = op_key(op) if isinstance(op, dict) else "-"
                line(f"plan {n} {op.get('op')} {key}")
            counts = Counter(op.get("op") for op in ops[start - 1:]
                             if isinstance(op, dict))
            line("dry run: " + (", ".join(f"{name} {counts[name]}"
                                          for name in sorted(counts))
                                 or "no ops") + " (no calls made)")
            return 0
        session = Session(opts.url, token)
    except (BatchError, OSError) as exc:
        err.write(f"error: {exc}\n")
        return 1
    try:
        who = session.get("whoami")
        who_name, who_role = who.get("name"), who.get("role")
        line(f"acting as {who_name} ({who_role})")
        if who_name != data["principal"] or who_role != "router":
            line(f"error: principal mismatch: batch wants "
                 f"{data['principal']!r} (router), token acts as "
                 f"{who_name!r} ({who_role}) (refusing before any call)")
            return 1
    except ApiError as exc:
        if opts.allow_no_whoami:
            msg = f" ({exc.msg})" if exc.msg else ""
            err.write(f"warning: whoami failed ({exc.code}){msg}, continuing due to --allow-no-whoami\n")
        else:
            msg = f" ({exc.msg})" if exc.msg else ""
            err.write(f"error: whoami failed: {exc.code}{msg}\n")
            return 2
    applied = Counter()
    done = 0
    if not opts.per_op:
        code, report = atomic.send(session, ops, start, source, digest,
                                   (ApiError, BatchError), op_key)
        for text in report:
            line(text)
        if code:
            return code
    for n, op in enumerate(ops, 1):
        if n < start:
            continue
        name = op.get("op") if isinstance(op, dict) else "?"
        key = op_key(op) if isinstance(op, dict) else "-"
        try:
            if opts.per_op:
                verb, body = build_request(op, source, digest)
                reply = atomic.parsed(session.post(verb, body))
                for text in atomic.warnings(reply): line(text)
                if reply is None:
                    raise ApiError("error", "unreadable keeper reply "
                                            "(op state unknown)")
        except BatchError as exc:
            return fail(n, name, key, "error", str(exc))
        except ApiError as exc:
            return fail(n, name, key, exc.code, exc.msg)
        line(f"ok {n} {name} {key}")
        applied[name] += 1
        done += 1
    skipped = start - 1
    counts = ", ".join(f"{name} {applied[name]}" for name in sorted(applied))
    summary = f"summary: {counts} (applied {done} of {total}" if counts \
        else f"summary: no ops (applied {done} of {total}"
    if skipped:
        summary += f", skipped {skipped} (before --from)"
    line(summary + ")")
    return 0
if __name__ == "__main__":
    sys.exit(main())
