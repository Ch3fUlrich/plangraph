"""Keeper configuration: a JSON file, mode 600 enforced, fields per contract.

The config maps the store and its verifiers onto real paths:

* ``db``            — the SQLite file (relative paths resolve next to the config)
* ``snapshot``      — where ``plan.snapshot.json`` is rewritten after every write
* ``repos``         — ``{project: working tree path}`` for the git verifier
* ``runs_dir``      — where ``run-record`` jobs live
* ``decisions_file``— the DECISIONS.md the ``decision`` verifier reads
* ``ci``            — ``{project: "owner/repo" | "none"}``
* ``batch_roots``   — directories ``close_on_decision`` may read batch files from
* ``operator_users``— the Authelia names the operator hop accepts (default none)
* ``operator_origin`` — the exact ``Origin`` an operator write must present
* ``operator_csrf_key_file`` — HMAC key for the ``X-PG-CSRF`` token
* ``bind``          — ``"host:port"`` (or ``host``/``port`` keys)
* ``default_lease`` — seconds a claim is held without a heartbeat

A config file is refused unless its mode is exactly ``0600``: it may name paths
and, in deployments, tokens.  No value is ever printed — :meth:`Config.__repr__`
lists key names only.
"""
from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path

from .evidence_git import valid_ref

__all__ = ["Config", "ConfigError", "load_config", "verifier_context",
           "REQUIRED_MODE"]

REQUIRED_MODE = 0o600
_DEFAULT_SNAPSHOT = "plan.snapshot.json"
_DEFAULT_DB = "keeper.db"
#: An ``Origin`` is exactly a scheme and a host with a port: a path, a user or
#: any other scheme can never match the header the browser sends.
_ORIGIN = re.compile(r"https?://[A-Za-z0-9._-]+(?::\d{1,5})?")


class ConfigError(Exception):
    """The config file is missing, unreadable, malformed or world-readable."""


class Config:
    """A validated config file with attribute access and a redacting repr."""

    def __init__(self, data: dict, base: Path):
        self._data = dict(data)
        self._base = Path(base)
        self.path = self._base / "keeper.json"
        self.db = self._resolve(data.get("db") or _DEFAULT_DB)
        self.snapshot = self._resolve(
            data.get("snapshot") or _DEFAULT_SNAPSHOT)
        repos = data.get("repos") or {}
        if not isinstance(repos, dict):
            raise ConfigError("repos must be a dictionary")
        self.repos = {}
        for proj, entry in repos.items():
            if isinstance(entry, str):
                self.repos[proj] = entry
            elif isinstance(entry, dict):
                path = entry.get("path")
                if not isinstance(path, str) or not path:
                    raise ConfigError(f"repo {proj!r} must contain a 'path' string")
                ref = entry.get("ref", "main")
                if not isinstance(ref, str) or not valid_ref(ref):
                    raise ConfigError(f"repo {proj!r} ref {ref!r} is invalid")
                self.repos[proj] = {"path": path, "ref": ref}
            else:
                raise ConfigError(f"repo {proj!r} entry must be a path string or dict")
        self.runs_dir = data.get("runs_dir")
        self.decisions_file = data.get("decisions_file")
        # M2B-SIGNED-SNAPSHOT: the keeper signs with signing_key; readers verify with snapshot_pubkey.
        self.signing_key = (str(self._resolve(data["signing_key"]))
                            if data.get("signing_key") else None)
        self.snapshot_pubkey = (str(self._resolve(data["snapshot_pubkey"]))
                                if data.get("snapshot_pubkey") else None)
        self.ci = dict(data.get("ci") or {})
        # X7b: the directories ``close_on_decision`` may read a batch file from.
        # Empty means the verb always refuses, so an unconfigured deployment
        # cannot close an item on a batch the operator never sanctioned.
        roots = data.get("batch_roots") or []
        if not isinstance(roots, (list, tuple)):
            raise ConfigError("batch_roots must be a list of paths")
        for root in roots:
            if not isinstance(root, str):
                raise ConfigError("batch_roots entries must be strings")
        # A blank entry would ``_resolve`` to the config dir and silently
        # authorise it; drop blanks before resolving.
        self.batch_roots = [str(self._resolve(p)) for p in roots if p.strip()]
        # PG-ANSWER-UI: the operator hop.  The allow-list is the only thing
        # naming an Authelia user, so a blank or absent entry must close every
        # door rather than match one.
        users = data.get("operator_users") or []
        if not isinstance(users, (list, tuple)):
            raise ConfigError("operator_users must be a list of names")
        for user in users:
            if not isinstance(user, str):
                raise ConfigError("operator_users entries must be strings")
            if not user.strip():
                raise ConfigError("operator_users entries must not be blank")
        self.operator_users = list(users)
        origin = data.get("operator_origin")
        if origin is not None and not _ORIGIN.fullmatch(str(origin)):
            raise ConfigError(f"operator_origin {origin!r} must be "
                              "scheme://host[:port] with no path")
        self.operator_origin = str(origin) if origin else None
        self.operator_csrf_key_file = (
            str(self._resolve(data["operator_csrf_key_file"]))
            if data.get("operator_csrf_key_file") else None)
        host, port = self._bind(data)
        self.host = host
        self.port = port
        self.default_lease = int(data.get("default_lease", 7200))
        self.review_overdue_s = int(data.get("review_overdue_s", 4 * 3600))
        self.lease_warn_s = int(data.get("lease_warn_s", 15 * 60))
        self.reap_interval = float(data.get("reap_interval", 60))

    # ------------------------------------------------------------- helpers
    def _resolve(self, value) -> Path:
        path = Path(value).expanduser()   # "~/..." in keeper.json / reader.json (D-818 steps)
        return path if path.is_absolute() else (self._base / path)

    @staticmethod
    def _bind(data) -> tuple[str, int]:
        bind = data.get("bind")
        if isinstance(bind, dict):
            return str(bind.get("host", "127.0.0.1")), int(bind.get("port", 8765))
        if isinstance(bind, str) and bind:
            host, _, port = bind.rpartition(":")
            return (host or "127.0.0.1"), int(port or 8765)
        return str(data.get("host", "127.0.0.1")), int(data.get("port", 8765))

    def get(self, key, default=None):
        return self._data.get(key, default)

    def __getitem__(self, key):
        return self._data[key]

    def keys(self):
        return list(self._data)

    def __repr__(self) -> str:
        # Key names only: a value may be a token or a share path.
        return f"Config({', '.join(sorted(self._data))})"


def _check_mode(path: Path) -> None:
    try:
        mode = stat.S_IMODE(os.stat(path).st_mode)
    except FileNotFoundError as exc:
        raise ConfigError(f"config not found: {path}") from exc
    except OSError as exc:
        raise ConfigError(f"config unreadable: {path}: {exc}") from exc
    if mode != REQUIRED_MODE:
        raise ConfigError(
            f"config {path} has mode {mode:04o}; must be 600 (chmod 600 {path})")


def load_config(path) -> Config:
    """Load and validate a JSON config; refuse any mode other than 600."""
    path = Path(path)
    _check_mode(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigError(f"config {path} is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"config {path} must be a JSON object")
    cfg = Config(raw, path.resolve().parent)
    cfg.path = path
    return cfg


def verifier_context(cfg: Config):
    """The real verifier adapters built from *cfg* (no I/O until called)."""
    from . import evidence
    return evidence.default_context(cfg)
