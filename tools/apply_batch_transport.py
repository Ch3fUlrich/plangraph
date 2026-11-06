"""HTTP budgets and reconciliation fingerprints (not idempotency keys)."""
import hashlib
import http.client
import json
import math
import urllib.error
import urllib.request

TIMEOUT = 180
READ_TIMEOUT = 15
SNAPSHOT_TIMEOUT = 60


def timeout_seconds(value):
    try:
        seconds = float(value)
        if math.isfinite(seconds) and seconds > 0:
            return seconds
    except (TypeError, ValueError):
        pass
    raise ValueError("--timeout must be finite positive seconds")


def operation_identity(principal, verb, body, digest, submitted):
    canonical = json.dumps(dict(principal=principal, verb=verb, body=body,
                                source_sha256=digest, submitted=submitted),
                           sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def uncertain(identity, submitted, detail=""):
    return [f"error: {detail} unknown outcome; operation {identity}; {submitted}",
            "reconcile this exact operation against keeper history and current state before any retry; do not blindly rerun",
            "compare the canonical operation's expected effects and keeper history; if history cannot prove applied or not applied, leave outcome unknown and do not retry",
            "one missing read is not proof of non-application; a snapshot is not atomic execution proof"]


class Session:
    """Injected exception types preserve the entry point's public test seams."""
    def __init__(self, url, token, *, errors, parse_error):
        self.BatchError, self.ApiError = errors
        self._parse_error = parse_error
        self.mutation_timeout = TIMEOUT
        self.url = url.rstrip("/")
        self.token = token
        class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
            def http_error_302(self, req, fp, code, msg, headers):
                raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)
            http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), NoRedirectHandler())

    def _call(self, verb, body=None, timeout=READ_TIMEOUT):
        try:
            data = json.dumps(body, allow_nan=False).encode("utf-8") if body is not None else None
        except (TypeError, ValueError) as exc:
            raise self.BatchError(f"cannot encode {verb}: {exc.__class__.__name__}") from None
        request = urllib.request.Request(
            f"{self.url}/v1/{verb}", data=data,
            method="POST" if body is not None else "GET")
        request.add_header("Authorization", f"Bearer {self.token}")
        if body is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with self._opener.open(request, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            try:
                raw = exc.read()
                reply = json.loads(raw.decode("utf-8"))
                if not isinstance(reply, dict) or not reply.get("error"):
                    raise ValueError()
                code, msg = self._parse_error(raw, exc.code)
            except Exception:
                raise self.ApiError("unknown outcome" if body is not None else "unreachable", "unreadable refusal") from None
            raise self.ApiError(code, msg) from None
        except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
            raise self.ApiError("unknown outcome" if body is not None else "unreachable", exc.__class__.__name__) from None

    def post(self, verb, body):
        return self._call(verb, body, timeout=self.mutation_timeout)

    def get(self, verb, timeout=READ_TIMEOUT):
        try:
            raw = self._call(verb, timeout=timeout)
            data = json.loads(raw.decode("utf-8"))
        except self.ApiError:
            raise
        except Exception as exc:
            raise self.ApiError("unreachable", exc.__class__.__name__) from None
        if not isinstance(data, dict):
            raise self.ApiError("unreachable", "bad whoami reply")
        return data

    def snapshot(self):
        return self.get("snapshot", timeout=SNAPSHOT_TIMEOUT)
