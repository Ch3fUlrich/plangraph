#!/usr/bin/env python3
"""Wake one local OpenCode v2 lane, with durable at-most-once delivery.

The cursor reserves a line BEFORE POST (fsync + replace + directory fsync).
A crash or ambiguous HTTP failure can lose that line, never replay it. There
is no server idempotency contract, so exactly-once delivery is not possible.
The cursor also remembers the last observed idle snapshot across invocations.
"""

import argparse
import base64
import fcntl
import hashlib
import http.client
import json
import math
import os
from pathlib import Path
import stat
import sys
import tempfile
import time
from datetime import datetime
import urllib.error
import urllib.parse
import urllib.request


class ServerDown(Exception):
    """No response from the local server."""


def atomic_cursor(path, state):
    path = Path(path)
    fd, name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(state, out)
            out.flush()
            os.fsync(out.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def request(args, password, method, suffix, body=None):
    token = base64.b64encode(("opencode:" + password).encode()).decode()
    url = "http://127.0.0.1:%d/api/session/%s/%s" % (
        args.port, urllib.parse.quote(args.sid, safe=""), suffix)
    req = urllib.request.Request(
        url, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": "Basic " + token, "Content-Type": "application/json"})
    # Do not follow redirects or send credentials through environment proxies.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *unused):
            return None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(req, timeout=5) as response:
            raw = response.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError:
        raise ValueError("HTTP request rejected") from None
    except http.client.RemoteDisconnected:
        raise ServerDown() from None
    except http.client.HTTPException:
        raise ValueError("invalid HTTP response") from None
    except (urllib.error.URLError, OSError):
        raise ServerDown() from None


def timestamp(value):
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        raise ValueError("missing message timestamp")
    if not math.isfinite(value):
        raise ValueError("invalid timestamp")
    return value / 1000 if value > 1e12 else value


def idle_snapshot(payload, idle_min):
    items = payload.get("data", payload) if isinstance(payload, dict) else payload
    items = items.get("items") if isinstance(items, dict) else items
    if not isinstance(items, list):
        raise ValueError("invalid messages")
    if not items:
        return hashlib.sha256(b"[]").hexdigest()
    latest = 0
    for item in items:
        info = item.get("info", item)
        clock = info.get("time", {})
        if not isinstance(clock, dict):
            clock = {"created": clock}
        ages = [timestamp(clock[k]) for k in
                ("created", "updated", "completed") if clock.get(k) is not None]
        if not ages:
            return None  # Unknown message age: fail closed without crashing.
        latest = max([latest, *ages])
        role = info.get("role", info.get("type"))
        status = info.get("status")
        if role == "assistant" and (status in ("running", "pending") or
                                    (not clock.get("completed") and status not in
                                     ("completed", "error", "aborted"))):
            return None
        for part in item.get("parts", item.get("content", [])):
            if part.get("state", {}).get("status") in ("running", "pending"):
                return None
    if not latest or time.time() - latest <= idle_min * 60:
        return None
    return hashlib.sha256(json.dumps(items, sort_keys=True).encode()).hexdigest()


def event(args, result, line=None):
    # No server response, exception text, credentials or prompt content is logged.
    record = {"time": time.time(), "session_id": args.sid,
              "task_line_number": line, "result": result}
    with open(args.log, "a", encoding="utf-8") as out:
        out.write(json.dumps(record) + "\n")


def tick(args, password):
    with open(str(args.cursor) + ".lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = Path(args.cursor)
        state = json.loads(path.read_text()) if path.exists() else {"consumed": 0}
        count = state["consumed"]
        if type(count) is not int or count < 0:
            raise ValueError("invalid cursor")
        lines = Path(args.inbox).read_text(encoding="utf-8").splitlines()
        if count < len(lines) and lines[count].strip() == "STOP":
            atomic_cursor(path, dict(state, consumed=count + 1, stopped=True))
            return True
        if state.get("stopped"):
            return True
        snapshot = idle_snapshot(request(args, password, "GET", "message?order=desc&limit=5"),
                                 args.idle_min)
        if snapshot is None:
            event(args, "busy", count + 1)
        elif (state.get("session_id") == args.sid and
              state.get("snapshot") == snapshot):
            event(args, "unchanged", count + 1)
        elif count >= len(lines):
            event(args, "empty", count + 1)
        else:
            # The reservation is the commit point; never roll it back on errors.
            atomic_cursor(path, {"consumed": count + 1, "session_id": args.sid,
                                 "snapshot": snapshot})
            request(args, password, "POST", "prompt",
                    {"text": "New task from the L1: " + lines[count]})
            event(args, "posted", count + 1)
    return False


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("once", "loop"))
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--password-file", required=True)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--session-id")
    source.add_argument("--state")
    for name in ("inbox", "cursor", "log"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--interval", type=float, default=30)
    parser.add_argument("--idle-min", type=float, default=5)
    args = parser.parse_args(argv)
    args.sid = ""
    try:
        if not 0 < args.port < 65536 or not math.isfinite(args.interval) or args.interval <= 0:
            raise ValueError()
        if not math.isfinite(args.idle_min) or args.idle_min < 0:
            raise ValueError()
        with open(args.password_file, encoding="utf-8") as secret:
            mode = os.fstat(secret.fileno()).st_mode
            if not stat.S_ISREG(mode) or stat.S_IMODE(mode) != 0o600:
                raise ValueError()
            password = secret.read().rstrip("\r\n")
        sid = args.session_id or json.loads(Path(args.state).read_text())["session_id"]
        if not password or not isinstance(sid, str) or not sid or password in sid:
            raise ValueError()
        args.sid = sid
        while True:
            if tick(args, password) or args.command == "once":
                return 0
            time.sleep(args.interval)
    except (ServerDown, OSError, ValueError, KeyError, TypeError, AttributeError):
        # Fixed diagnostics deliberately discard untrusted exception/body text.
        code = 3 if isinstance(sys.exc_info()[1], ServerDown) else 2
        try:
            event(args, "error")
        except OSError:
            pass
        print("wake runner: server unavailable" if code == 3 else
              "wake runner: invalid configuration or response", file=sys.stderr)
        return code
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
