"""privacy=sensitive admission (PG-EFFICIENCY-GATES gap 3; operator D-2683/D-2684, D-2738, D-2760).

A ``privacy: sensitive`` card may run only when ALL of these hold, else the
spawner refuses it naming D-2683:

* the leg is a marked model (:data:`SENSITIVE_OK_MODELS`: agy gemini-3.8-flash,
  mimo-v2.6-flash/-pro) that does not train on prompts; every other leg is
  dropped at route time (:func:`sensitive_leg_ok`, the route-level marker);
* the repo is private per the REPO registry (:func:`repo_private`): a file kept
  by the registry owner (``$PLANGRAPH_REPO_REGISTRY`` or
  ``~/.config/plangraph/repos.json``, ``{"repos": {"<repo>": {"private": true}}}``).
  An absent file, an unknown repo or a non-``true`` flag fails closed; a
  card-level flag is never enough (D-2738);
* no secret is in scope: no ``secrets`` risk tag, the card declares its scope
  as a list of paths, and none matches :func:`secret_path`;
* the real run tree holds no secret-shaped untracked or ignored file
  (:func:`run_tree_secrets`, checked right before the model starts; plangraph
  runs in place, so the run tree is the worktree).  Tracked files are reviewed
  code; real secrets live untracked (``.env``, ``api-keys.yml``, ``vault/``).
"""
from __future__ import annotations

import fnmatch
import json
import os
import subprocess
from pathlib import Path, PurePosixPath

from plangraph.routing_errors import SpawnRefused

__all__ = ["SENSITIVE_OK_MODELS", "SECRET_NAME_PATTERNS", "SECRET_DIRS", "REGISTRY_ENV",
           "is_sensitive", "secret_path", "sensitive_leg_ok", "repo_of", "repo_private",
           "admit_card", "run_tree_secrets", "admit_run_tree"]

#: The only models a sensitive card may reach (D-2683; gemini only over agy, D-255).
SENSITIVE_OK_MODELS = frozenset({("agy", "gemini-3.8-flash"), ("opencode", "mimo/mimo-v2.6-flash"),
                                 ("opencode", "mimo/mimo-v2.6-pro")})
#: One matcher for secret-shaped file names (lower-cased basename).  The old
#: shapes plus the wider set: a name that merely says api-key / secret / token /
#: credential is treated as a secret (over-matching refuses, which is the safe side).
SECRET_NAME_PATTERNS = (
    "*.key", "*.pem", "*.p12", "*.pfx", "*.kdbx", "*.p8", "*.jks",
    ".env", ".env.*", "credentials*.json", "api_keys.*", "api-keys.*", "id_rsa*", "id_ed25519*",
    "*api-key*", "*api_key*", "*apikey*", "*secret*", "*token*", "*credential*",
    "service-account*.json",
)
#: Directory names whose whole content counts as secret.
SECRET_DIRS = frozenset({"secrets", "secret", "vault", ".ssh", ".gnupg"})
REGISTRY_ENV = "PLANGRAPH_REPO_REGISTRY"
REGISTRY_FILE = "~/.config/plangraph/repos.json"
_D = "D-2683"


def is_sensitive(card):
    return isinstance(card, dict) and card.get("privacy") == "sensitive"


def secret_path(path):
    """True when *path* (any separator) names a secret-shaped file or sits in a secret dir."""
    parts = [p for p in PurePosixPath(str(path).replace("\\", "/").lower()).parts if p not in ("/", ".")]
    # D-2965: this documentation page is not token-handling code.
    if parts == ["docs", "tokens.md"]:
        return False
    if not parts:
        return False
    if any(p.rstrip("/") in SECRET_DIRS for p in parts):
        return True
    name = parts[-1]
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in SECRET_NAME_PATTERNS)


def sensitive_leg_ok(leg):
    """The route-level marker: a marked model that does not train on prompts."""
    return (getattr(leg, "harness", None), getattr(leg, "model", None)) in SENSITIVE_OK_MODELS \
        and not getattr(leg, "trains_on_data", True)


def repo_of(card):
    """``card["repo"]``, else the project part of ``card["key"]`` (``plangraph:X`` -> ``plangraph``)."""
    repo = card.get("repo")
    if isinstance(repo, str) and repo:
        return repo
    key = card.get("key")
    if isinstance(key, str) and ":" in key:
        return key.split(":", 1)[0] or None
    return None


def repo_private(repo, env=None):
    """True only when the registry marks *repo* ``private: true``; None/False fail closed."""
    if not repo:
        return None
    path = Path(os.path.expanduser((env or os.environ).get(REGISTRY_ENV) or REGISTRY_FILE))
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return None
    entry = (data.get("repos") or {}).get(repo) if isinstance(data, dict) else None
    if not isinstance(entry, dict):
        return None
    return entry.get("private") is True


def admit_card(card, env=None):
    """Raise ``SpawnRefused`` (naming D-2683) unless a sensitive *card* may run at all."""
    if not is_sensitive(card):
        return
    if "secrets" in (card.get("risk_tags") or ()):
        raise SpawnRefused(f"privacy=sensitive with secrets in scope ({_D}): risk tag secrets")
    paths = card.get("paths", card.get("write_set"))
    if not isinstance(paths, (list, tuple)) or not paths or not all(isinstance(p, str) for p in paths):
        raise SpawnRefused(f"privacy=sensitive needs its scope as a list of paths ({_D}): unknown scope")
    hits = [p for p in paths if secret_path(p)]
    if hits:
        raise SpawnRefused(f"privacy=sensitive with a secret path in scope ({_D}): {hits[:5]}")
    repo = repo_of(card)
    if repo_private(repo, env) is not True:
        raise SpawnRefused(f"privacy=sensitive needs a repo the registry marks private ({_D}): "
                           f"{repo or 'unknown repo'}")


def run_tree_secrets(cwd):
    """Secret-shaped untracked or ignored entries of the git tree at *cwd*; None when unknown."""
    try:
        out = subprocess.run(["git", "-C", str(cwd), "ls-files", "-z", "--others", "--directory"],
                             capture_output=True, timeout=60, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    entries = [e for e in out.stdout.decode("utf-8", "replace").split("\0") if e]
    return [e for e in entries if secret_path(e)]


def admit_run_tree(card, cwd):
    """Raise ``SpawnRefused`` when a sensitive *card*'s real run tree holds a secret (or is unknown)."""
    if not is_sensitive(card):
        return
    hits = run_tree_secrets(cwd)
    if hits is None:
        raise SpawnRefused(f"privacy=sensitive: run tree {cwd} is not a readable git tree ({_D})")
    if hits:
        raise SpawnRefused(f"privacy=sensitive: secret-shaped files in the run tree ({_D}): {hits[:5]}")
