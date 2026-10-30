"""Session-spawn config parsing and fail-closed argument checks.

SESSION-SPAWN-MCP slice 1/4 (the 400-line file gate, D-3209/D-3215):
``_ConfigMixin`` holds everything that turns the operator config and request
arguments into validated values - moved unchanged out of ``sessions.py`` so
each file keeps one responsibility. ``Sessions`` subclasses the mixin, so
``self`` is the ``Sessions`` instance and the bodies are untouched (D-3193
fail-closed checks: refuse, never guess).
"""
from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path

from plangraph.routing_errors import ParameterRefused

SESSION_CONFIG_KEYS = frozenset({
    "cwd_roots", "models", "efforts", "permission_modes", "mcp_configs",
    "style_text", "compact_window", "ctx_cap_tokens",
})
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")


def _refuse(message):
    """Refusal with scrubbed text: error messages reach callers and logs."""
    return ParameterRefused(_scrub(str(message)))

class _ConfigMixin:
    """Config loading and request-argument checks for ``Sessions``.

    Stateless: reads only ``self._config_path`` / ``self._platform`` set by
    ``Sessions.__init__``. Every check refuses via ``ParameterRefused``."""

    # ---- config: parsed, trusted, fail-closed (R-1, R-3) ---------------

    def _config_file(self):
        """The configured path, else $PLANGRAPH_SESSIONS_CONFIG, else the
        default ~/.config/plangraph/sessions.json (R-24)."""
        return Path(self._config_path) if self._config_path else (
            Path.home() / ".config" / "plangraph" / "sessions.json")

    def _read_config_bytes(self, path):
        """Read the config through the one descriptor that passed the checks
        (R-24). POSIX: refuse when the platform lacks O_NOFOLLOW, open the path
        once with it (ELOOP and friends refuse), fstat that fd - regular file,
        owned by the current user, mode 0600 or stricter - and read the bytes
        from the same fd, never re-opened by name. win32: plain read."""
        if self._platform == "win32":
            try:
                return Path(path).read_bytes()
            except OSError:
                raise _refuse(f"sessions config is not a readable regular non-symlink file: {path}")
        if not hasattr(os, "O_NOFOLLOW"):
            raise _refuse(f"no O_NOFOLLOW on this platform: cannot safely read {path}")
        try:
            fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW)
        except OSError:
            raise _refuse(f"sessions config is not a readable regular non-symlink file: {path}")
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise _refuse(f"sessions config is not a regular file: {path}")
            if st.st_uid != os.getuid():
                raise _refuse(f"sessions config is not owned by the current user: {path}")
            if st.st_mode & 0o077:
                raise _refuse(
                    f"sessions config mode must be 0600 or stricter (group/world-writable or readable): {path}")
            chunks = []
            try:
                while True:
                    chunk = os.read(fd, 65536)
                    if not chunk:
                        break
                    chunks.append(chunk)
            except OSError:
                raise _refuse(f"sessions config is not a readable regular non-symlink file: {path}")
            return b"".join(chunks)
        finally:
            os.close(fd)

    def _load_config(self):
        path = self._config_file()
        raw = self._read_config_bytes(path)
        # The parse guard must not swallow the read's ParameterRefused: it is a
        # ValueError and would otherwise surface as "not valid JSON".
        try:
            cfg = json.loads(raw)
        except ValueError:
            raise _refuse(f"sessions config is not valid JSON: {path}")
        if not isinstance(cfg, dict):
            raise _refuse(f"sessions config must be a JSON object: {path}")
        self._check_config(cfg)
        return cfg

    def _check_config(self, cfg):
        unknown = sorted(set(cfg) - SESSION_CONFIG_KEYS)
        if unknown:
            raise _refuse(f"config has unknown keys: {unknown}")
        roots = self._root_list(cfg, "cwd_roots")
        if not roots:
            raise _refuse("config cwd_roots must be a non-empty list of directories")
        for key in ("models", "efforts", "permission_modes"):
            values = cfg.get(key)
            if not isinstance(values, list) or not values or not all(isinstance(v, str) and v for v in values):
                raise _refuse(f"config {key} must be a non-empty list of strings")
        entries = cfg.get("mcp_configs")
        if not isinstance(entries, list):
            raise _refuse("config mcp_configs must be a list of file paths")
        for raw in entries:
            if not isinstance(raw, str) or not Path(raw).expanduser().resolve().is_file():
                raise _refuse(f"config mcp_configs entry is not a regular file: {raw}")
        # Scalars: a present key must carry its type - a null is not a value.
        if "style_text" in cfg:
            style_text = cfg["style_text"]
            if not isinstance(style_text, str) or not style_text:
                raise _refuse("config style_text must be a non-empty string")
        if "compact_window" in cfg:
            window = cfg["compact_window"]
            if not isinstance(window, str) or not window.isdigit():
                raise _refuse("config compact_window must be a digit string")
        if "ctx_cap_tokens" in cfg:
            cap = cfg["ctx_cap_tokens"]
            if not isinstance(cap, int) or isinstance(cap, bool) or cap <= 0:
                raise _refuse("config ctx_cap_tokens must be a positive int")

    def _root_list(self, cfg, key):
        raw_list = cfg.get(key)
        if raw_list is None:
            return []
        if not isinstance(raw_list, list):
            raise _refuse(f"config {key} must be a list of directories")
        roots = []
        for raw in raw_list:
            if not isinstance(raw, str) or not raw or not Path(raw).expanduser().is_absolute():
                raise _refuse(f"config {key} entries must be absolute directory paths: {raw}")
            path = Path(raw).expanduser().resolve()
            # A filesystem root would allow any path; refuse it outright.
            if len(path.parts) <= 1 or path.parent == path:
                raise _refuse(f"config {key} must not be a filesystem root: {raw}")
            if not path.is_dir():
                raise _refuse(f"config {key} entry is not an existing directory: {raw}")
            roots.append(path)
        return roots

    def _inside(self, path, roots):
        return any(path == root or root in path.parents for root in roots)

    def _file_arg(self, value, cfg, what):
        """prompt_file / handoff: inside cwd_roots, a readable regular file."""
        if not isinstance(value, str) or not value:
            raise _refuse(f"{what} must be a non-empty path string")
        path = Path(value).expanduser().resolve()
        if not self._inside(path, self._root_list(cfg, "cwd_roots")):
            raise _refuse(f"{what} is outside the configured roots: {value}")
        if not path.is_file():
            raise _refuse(f"{what} is not a regular file: {value}")
        if not os.access(path, os.R_OK):
            raise _refuse(f"{what} is not readable: {value}")
        return path

    def _check_name(self, name):
        if not isinstance(name, str) or not NAME_RE.fullmatch(name):
            raise _refuse(f"name must match [A-Za-z0-9][A-Za-z0-9._-]{{0,63}}: {name!r}")
        if name.lower().endswith(".ready"):
            # <name>.ready.json is the ready-marker file name: a session name
            # with that suffix would collide with its own marker.
            raise _refuse("name must not end with .ready")
        return name

    def _check_cwd(self, cwd, cfg):
        if not isinstance(cwd, str) or not cwd:
            raise _refuse("cwd must be a non-empty path string")
        path = Path(cwd).expanduser().resolve()
        if not self._inside(path, self._root_list(cfg, "cwd_roots")):
            raise _refuse(f"cwd outside cwd_roots: {cwd}")
        if not path.is_dir():
            raise _refuse(f"cwd does not exist or is not a directory: {cwd}")
        return path

    def _check_choice(self, value, allowed, what):
        if not isinstance(value, str) or value not in (allowed or ()):
            raise _refuse(f"{what} not allowed by config: {value!r}")
        return value

    def _check_mcp(self, mcp_config, cfg):
        if not isinstance(mcp_config, str) or not mcp_config:
            raise _refuse("mcp_config is required")
        path = Path(mcp_config).expanduser().resolve()
        listed = {str(Path(e).expanduser().resolve()) for e in cfg.get("mcp_configs") or ()}
        if str(path) not in listed:
            raise _refuse(f"mcp_config not listed in config mcp_configs: {mcp_config}")
        if not path.is_file():
            raise _refuse(f"mcp_config is not a regular file: {mcp_config}")
        return path

    def _prompt_text(self, prompt, prompt_file, cfg):
        if (prompt is None) == (prompt_file is None):
            raise _refuse("exactly one of prompt or prompt_file is required")
        if prompt is not None:
            if not isinstance(prompt, str):
                raise _refuse("prompt must be a string")
            text = prompt
        else:
            try:
                text = self._file_arg(prompt_file, cfg, "prompt_file").read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                raise _refuse(f"prompt_file not readable: {prompt_file}")
        if not text.strip():
            raise _refuse("prompt must not be blank")
        # Dash guard runs on the raw prompt text, before any handoff prefix.
        if text.startswith("-"):
            raise _refuse("prompt must not start with '-'")
        return text

    def _style_text(self, cfg):
        style = cfg.get("style_text")
        if isinstance(style, str) and style.startswith("-"):
            # argv style: a leading dash would read as a CLI flag, never text.
            raise _refuse("config style_text must not start with '-'")
        return style

    def _handoff_arg(self, handoff, cfg):
        if not isinstance(handoff, str) or not handoff:
            raise _refuse("handoff must be a non-empty path string")
        return self._file_arg(handoff, cfg, "handoff")

    def _relaunch_handoff(self, rec, cfg, name, handoff):
        """Effective relaunch handoff: the argument wins, else the ready marker.
        With neither, the relaunch is refused ("ready marker or handoff
        required"). Freshness is measured against the last successful launch
        (launched_ts, else ts): the handoff file must be strictly newer than
        that launch time - an equal mtime is stale (seat finding). Existence
        and containment always apply - the record's old handoff is never
        reused."""
        if handoff is not None:
            path = self._handoff_arg(handoff, cfg)
        else:
            marker_path = self._marker_path(name)
            if not marker_path.is_file():
                raise _refuse("ready marker or handoff required")
            try:
                marker = json.loads(marker_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                marker = {}
            raw = marker.get("handoff")
            if not isinstance(raw, str) or not raw:
                raise _refuse("ready marker handoff must be a path string")
            path = self._handoff_arg(raw, cfg)
        launched = float(rec.get("launched_ts") or rec.get("ts") or 0)
        if path.stat().st_mtime <= launched:
            raise _refuse(f"handoff is not newer than the session record: {path}")
        return path



# Imported last on purpose: _refuse scrubs through sessions._scrub while
# sessions.py imports _refuse (and the two constants) from this module.
# Both modules define their names before reaching the cross-import, so either
# import order resolves; moving this import to the top would break one of them.
from plangraph.spawner.sessions import _scrub
