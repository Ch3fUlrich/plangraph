"""Keeper configuration: a JSON file, mode 600 enforced, fields per contract.

The config maps the store and its verifiers onto real paths:

* ``db``            — the SQLite file (relative paths resolve next to the config)
* ``snapshot``      — where ``plan.snapshot.json`` is rewritten after every write
* ``repos``         — ``{project: working tree path}`` for the git verifier
* ``runs_dir``      — where ``run-record`` jobs live
* ``decisions_file``— the DECISIONS.md the ``decision`` verifier reads
* ``ci``            — ``{project: "owner/repo" | "none"}``
* ``bind``          — ``"host:port"`` (or ``host``/``port`` keys)
* ``default_lease`` — seconds a claim is held without a heartbeat

A config file is refused unless its mode is exactly ``0600``: it may name paths
and, in deployments, tokens.  No value is ever printed — :meth:`Config.__repr__`
lists key names only.
"""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path

__all__ = ["Config", "ConfigError", "load_config", "verifier_context",
           "REQUIRED_MODE"]

REQUIRED_MODE = 0o600
_DEFAULT_SNAPSHOT = "plan.snapshot.json"
_DEFAULT_DB = "keeper.db"


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
        self.repos = dict(data.get("repos") or {})
        self.runs_dir = data.get("runs_dir")
        self.decisions_file = data.get("decisions_file")
        self.ci = dict(data.get("ci") or {})
        host, port = self._bind(data)
        self.host = host
        self.port = port
        self.default_lease = int(data.get("default_lease", 7200))
        self.review_overdue_s = int(data.get("review_overdue_s", 4 * 3600))
        self.lease_warn_s = int(data.get("lease_warn_s", 15 * 60))
        self.reap_interval = float(data.get("reap_interval", 60))

    # ------------------------------------------------------------- helpers
    def _resolve(self, value) -> Path:
        path = Path(value)
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
