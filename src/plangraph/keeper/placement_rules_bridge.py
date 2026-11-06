"""C4 bounded transport to the server-installed, policy-owning Rust helper."""
import json
import os
import subprocess
import threading
import time

from .errors import Refused

_MAX = 1024 * 1024
_TIMEOUT = 5


def _bin_env():
    return os.environ


def call_rules(request: dict) -> dict:
    """Send data only on stdin; never inherit credentials or invoke a shell."""
    unavailable = Refused("rules_unavailable", "placement rules helper unavailable")
    binary = _bin_env().get("PG_PLACEMENT_RULES_BIN", "/usr/local/bin/pg-placement-rules")
    if not os.path.isabs(binary):
        raise unavailable
    try:
        body = json.dumps(request, allow_nan=False, separators=(",", ":")).encode()
    except (ValueError, TypeError, RecursionError):
        raise Refused("rules_invalid", "invalid placement rules request") from None
    if len(body) + 1 > _MAX:
        raise Refused("rules_invalid", "invalid placement rules request")
    deadline = time.monotonic() + _TIMEOUT
    try:
        child = subprocess.Popen([binary], shell=False, stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                 env={})
    except OSError:
        raise unavailable from None
    output = bytearray()
    failed = threading.Event()

    def read():
        try:
            while True:
                chunk = child.stdout.read(65536)
                if not chunk:
                    break
                if len(output) + len(chunk) > _MAX:
                    failed.set()
                    child.kill()
                    break
                output.extend(chunk)
        except OSError:
            failed.set()

    def write():
        try:
            child.stdin.write(body + b"\n")
            child.stdin.close()
        except (OSError, ValueError):
            failed.set()

    reader = threading.Thread(target=read)
    writer = threading.Thread(target=write)
    reader.start()
    writer.start()
    try:
        child.wait(timeout=max(0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        failed.set()
        child.kill()  # Only our pid: the helper is single-process, not a process group.
    finally:
        child.wait()
        writer.join()
        reader.join()
        child.stdin.close()
        child.stdout.close()
    if failed.is_set():
        raise unavailable
    try:
        result = json.loads(output)
    except (ValueError, UnicodeError, RecursionError):
        raise unavailable from None
    if result == {"error": "rules_invalid"} and child.returncode == 2:
        raise Refused("rules_invalid", "invalid placement rules request")
    if (child.returncode != 0 or not isinstance(result, dict)
            or set(result) != {"ok"} or not isinstance(result["ok"], dict)):
        raise unavailable
    return result["ok"]
