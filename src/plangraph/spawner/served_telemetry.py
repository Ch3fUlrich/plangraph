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
    """Model id from the stream-json ``system``/init event of a claude -p run (D-3605)."""
    for line in str(text or '').splitlines():
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if isinstance(data, dict) and data.get('type') == 'system' and isinstance(data.get('model'), str):
            return data['model']
    return None


def _opencode_served_model(db_path, title) -> str | None:
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
        return None
    return next(iter(models)) if len(models) == 1 else None
