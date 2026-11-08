"""Served-model witnesses: header/JSON output, claude stream-json, opencode session store.

Moved verbatim out of runner.py (file-size baseline, D-3643); runner re-exports the names.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3

from plangraph.spawner.resolver import detect_model_family

_SELECTED_MODEL_RE = re.compile(r"(?i)X-OmniRoute-Selected-Model:\s*([^\r\n]+)")
_SELECTED_FAMILY_RE = re.compile(r"(?i)X-OmniRoute-Selected-Family:\s*([^\r\n]+)")


def extract_served_telemetry(output: str, fallback_model: str = "") -> tuple[str, str]:
    """Extract served model and family from OmniRoute headers or JSON output."""
    served_model = ""
    served_family = ""

    if output:
        # Check header patterns
        m_mod = _SELECTED_MODEL_RE.search(output)
        if m_mod:
            served_model = m_mod.group(1).strip()

        m_fam = _SELECTED_FAMILY_RE.search(output)
        if m_fam:
            served_family = m_fam.group(1).strip()

        # Check JSON payload if present
        if not served_model or not served_family:
            for line in output.splitlines():
                line = line.strip()
                if line.startswith("{") and line.endswith("}"):
                    try:
                        data = json.loads(line)
                        if isinstance(data, dict):
                            if not served_model and "served_model" in data:
                                served_model = str(data["served_model"])
                            if not served_family and "served_family" in data:
                                served_family = str(data["served_family"])
                    except (ValueError, TypeError):
                        continue
                    except Exception as exc:
                        logging.getLogger(__name__).warning('telemetry_failed: %s', type(exc).__name__)
                        continue

    if not served_family and served_model:
        served_family = detect_model_family(served_model)

    return served_model, served_family


def _claude_stream_model(text) -> str | None:
    """Request-side stream witness, preserving the D-3605 contract."""
    for line in str(text or '').splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get('type') == 'system' and isinstance(event.get('model'), str):
            return event['model']
    return None


def _opencode_served_model(db_path, title, *, all_models=False):
    """Served model witnessed in opencode's session store for one titled session (D-2898).

    Only a unanimous assistant model id counts; anything else stays unwitnessed.
    """
    try:
        from urllib.parse import quote
        # 2s timeout: a locked/busy store must never block the monitor
        # (D-2921 F2); it just stays unwitnessed.
        conn = sqlite3.connect('file:' + quote(str(db_path)) + '?mode=ro', uri=True, timeout=2.0)
        try:
            rows = conn.execute('SELECT id FROM session_v2 WHERE title = ?', (title,)).fetchall()
            models = set()
            for (data,) in conn.execute(
                    "SELECT data FROM session_message WHERE session_id = ? AND type = 'assistant'",
                    (rows[0][0],) if len(rows) == 1 else (None,)):
                model = json.loads(data)['model']
                models.add(model['providerID'] + '/' + model['id'])
        finally:
            conn.close()
    except (sqlite3.Error, OSError, ValueError, KeyError, TypeError):
        return set() if all_models else None
    return models if all_models else next(iter(models)) if len(models) == 1 else None


def native_models(harness, text):
    """Read only structured harness metadata, never nested tool/answer text.

    Codex exec JSONL (codex-rs/exec/src/exec_events.rs:38-56,135-139)
    has thread_id, usage and agent_message.text, but no served model field.
    routing_support/clients.py:346-347 currently launches plain exec, not JSONL.
    ACP agy output has no verified served-model field either.
    Claude init model is recorded in
    rust/worker-core/tests/fixtures/claude/claude-text-run.jsonl:1.
    """
    models = set()
    for line in str(text or '').splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        model = None
        if harness == 'claude':
            if event.get('type') == 'system' and event.get('subtype') == 'init':
                model = event.get('model')
        # Recorded OpenCode --format json has no model metadata:
        # rust/worker/tests/fixtures/opencode/real-ok-text.jsonl:1-2.
        # Do not invent an info.model schema or consult its child-writable DB.
        if isinstance(model, str) and model.strip():
            models.add(model)
    return models


def record_native(job, harness, models, requested):
    """Record native provenance without promoting it to a gateway witness."""
    if len(models) != 1:
        reason = ('non-unanimous native models' if models else
                  'emits no verified structured served-model telemetry')
        job.served_witness_reason = f'{harness}: {reason}'
        return
    model = next(iter(models))
    job.served_model, job.served_family = model, detect_model_family(model)
    if job.witness_kind != 'request-side-claude-stream':
        job.witness_kind = 'native-' + harness
    job.served_witness_reason = f'{harness} structured native model metadata'
    if model != requested:
        job.events.append(dict(event='served_model_mismatch', requested_model=requested, served_model=model))


async def witness_native(runner, job_id, job, text, requested):
    """Native telemetry for writers and reviews; response-side gateway wins."""
    from pathlib import Path
    if job.exit_code != 0 or job.served_model not in (None, '', 'unwitnessed'):
        return
    harness = Path(job.command[0]).name if job.command else ''
    models = native_models(harness, text)
    record_native(job, harness, models, requested)


def mismatch_flag(record):
    """Shared status projection (Job and persisted run records)."""
    record['served_model_mismatch'] = any(
        e.get('event') == 'served_model_mismatch' for e in record.get('events', []))
    return record
