"""Helpers to write batch files and run ``tools/apply_batch.py`` as a subprocess."""
from __future__ import annotations

import hashlib
import subprocess
import sys

import yaml

from contract_harness import ROOT, _env
from contract_world import uid

TOOL = ROOT / "tools" / "apply_batch.py"


def lane(project, ident, deps=(), **fields):
    item = {"project": project, "id": ident, "kind": "lane", "title": f"lane {ident}",
            "description": "batch lane", "done_criteria": "criteria",
            "evidence_kinds": ["decision"], "depends_on": list(deps)}
    item.update(fields)
    return item


def goal(project, ident, deps):
    return {"project": project, "id": ident, "kind": "goal", "title": f"goal {ident}",
            "description": "batch goal", "depends_on": list(deps)}


def write_batch(directory, ops, name=None, principal="router", source_text="plan source\n",
                digest=None, extra=None):
    """Write ``<name>.md`` (hashed source) and ``<name>.yaml``; return the yaml path."""
    name = name or f"batch-{uid().lower()}"
    directory.mkdir(parents=True, exist_ok=True)
    source = directory / f"{name}.md"
    source.write_text(source_text, encoding="utf-8")
    doc = {"batch": name, "principal": principal, "source": str(source),
           "source_sha256": digest or hashlib.sha256(source.read_bytes()).hexdigest(),
           "ops": ops}
    doc.update(extra or {})
    path = directory / f"{name}.yaml"
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    return path


def token_file(directory, token):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"tok-{uid().lower()}"
    path.write_text(token, encoding="utf-8")
    path.chmod(0o600)
    return path


def run_batch(kp, batch, who="router", args=(), tmp=None):
    """Run the applier; returns the CompletedProcess (stdout/stderr as text)."""
    tfile = token_file(batch.parent, kp.tokens[who])
    return subprocess.run(
        [sys.executable, str(TOOL), str(batch), "--url", kp.url, "--token-file", str(tfile),
         *args], capture_output=True, text=True, timeout=60, env=_env())
