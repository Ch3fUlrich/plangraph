"""Signal a waiting Temporal workflow when a question is answered.

Stdlib only (``urllib``).  Configuration is read from the environment at call
time: ``PLANGRAPH_TEMPORAL_HTTP`` (base URL of the Temporal HTTP API; unset or
empty disables signalling, with no network) and ``PLANGRAPH_TEMPORAL_NAMESPACE``
(default ``default``).  :func:`send_answer` never raises and never returns or
logs the URL or a response body; there are no retries, threads or timers (the
question deadline timer lives in the workflow, not in the keeper).
"""
from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.parse
import urllib.request

__all__ = ["send_answer", "workflow_id_for", "SIGNAL_NAME", "TIMEOUT_S"]

SIGNAL_NAME = "answer"
TIMEOUT_S = 2.0
_ENV_BASE = "PLANGRAPH_TEMPORAL_HTTP"
_ENV_NAMESPACE = "PLANGRAPH_TEMPORAL_NAMESPACE"
#: ``json/plain`` in base64, the Temporal payload encoding metadata.
_ENCODING = "anNvbi9wbGFpbg=="


def _base_url() -> str | None:
    """The configured http(s) base URL without a trailing slash, or ``None``."""
    raw = os.environ.get(_ENV_BASE, "").strip()
    parts = urllib.parse.urlsplit(raw)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    return raw.rstrip("/")


def workflow_id_for(conn, key: str) -> str | None:
    """``workflow_id`` of the newest attempt of *key* that has one, else ``None``."""
    row = conn.execute(
        "SELECT workflow_id FROM attempts WHERE key=? AND workflow_id IS NOT NULL"
        " ORDER BY n DESC LIMIT 1", (key,)).fetchone()
    return row[0] if row is not None else None


def _payload(question: dict) -> dict:
    """The signal request body: one ``json/plain`` payload, compact UTF-8 JSON."""
    data = json.dumps({"question_id": question["question_id"], "key": question["key"],
                       "text": question["text"], "source": question["source"]},
                      separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return {"input": {"payloads": [{"metadata": {"encoding": _ENCODING},
                                    "data": base64.b64encode(data).decode("ascii")}]},
            "identity": "plangraph-keeper",
            "requestId": f"plangraph-q{question['question_id']}-{question['source']}"}


def _post(base: str, workflow_id: str, question: dict) -> str:
    """POST the signal; return ``sent``, ``not_found`` or ``failed``."""
    namespace = os.environ.get(_ENV_NAMESPACE, "").strip() or "default"
    url = (f"{base}/api/v1/namespaces/{urllib.parse.quote(namespace, safe='')}"
           f"/workflows/{urllib.parse.quote(workflow_id, safe='')}/signal/{SIGNAL_NAME}")
    request = urllib.request.Request(
        url, data=json.dumps(_payload(question)).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
            return "sent" if 200 <= response.status < 300 else "failed"
    except urllib.error.HTTPError as exc:
        return "not_found" if exc.code == 404 else "failed"
    except Exception:  # noqa: BLE001 - URLError, timeout, bad URL: never raise
        return "failed"


def send_answer(question: dict, conn=None) -> dict:
    """Signal the workflow waiting on *question*; return ``{status, workflow_id}``.

    *question* carries ``question_id``, ``key``, ``text``, ``source`` and
    ``answered_at``.  The workflow id comes from ``question["workflow_id"]`` if
    given, else from the newest attempt of the key found through *conn*.
    Statuses: ``sent``, ``not_found``, ``failed``, ``disabled``, ``no_workflow``,
    ``skipped_timeout``.
    """
    base = _base_url()
    if base is None:
        return {"status": "disabled", "workflow_id": None}
    try:
        workflow_id = question.get("workflow_id")
        if workflow_id is None and conn is not None:
            workflow_id = workflow_id_for(conn, question["key"])
    except Exception:  # noqa: BLE001 - a lookup failure never fails the answer
        return {"status": "failed", "workflow_id": None}
    if not workflow_id:
        return {"status": "no_workflow", "workflow_id": None}
    if question.get("source") == "question_timeout":
        return {"status": "skipped_timeout", "workflow_id": workflow_id}
    try:
        status = _post(base, workflow_id, question)
    except Exception:  # noqa: BLE001
        status = "failed"
    return {"status": status, "workflow_id": workflow_id}
