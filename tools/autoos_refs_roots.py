"""The scan roots of tools/autoos_refs_check.py: one function per place a live
reader (Claude Code, OpenCode, systemd, the routing fleet) can still load
AutoOS from. Every function returns ``(kind, path, detail)`` hits and never
opens a secret-named file; symlinked files are followed by ``check_file``.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

from autoos_refs_core import Hit, check_file, looks_secret


def scan_claude_settings(home: Path) -> list[Hit]:
    hits: list[Hit] = []
    for name in ("settings.json", "settings.local.json"):
        settings_file = home / ".claude" / name
        if settings_file.exists() or settings_file.is_symlink():
            hits.extend(check_file(settings_file, "claude-settings"))
    return hits


def scan_claude_hooks(home: Path) -> list[Hit]:
    hits: list[Hit] = []
    hooks_dir = home / ".claude" / "hooks"
    if hooks_dir.is_dir():
        for p in sorted(hooks_dir.rglob("*")):
            if p.is_file() or p.is_symlink():
                hits.extend(check_file(p, "claude-hook", is_text_script=True))
    return hits


def scan_instruction_files(directory: Path) -> list[Hit]:
    """Scan *.md instruction files directly under *directory* (non-recursive)."""
    hits: list[Hit] = []
    if not directory.is_dir():
        return hits
    for p in sorted(directory.iterdir()):
        if p.name.lower().endswith(".md") and (p.is_file() or p.is_symlink()):
            hits.extend(check_file(p, "instruction"))
    return hits


def _scan_skill_tree(root: Path, link_budget: int = 3) -> list[Hit]:
    """Scan one skills tree, traversing symlinked directories too: each hop
    costs one *link_budget* level and a resolved dir is entered once, so a
    link cycle cannot loop forever.
    """
    hits: list[Hit] = []
    seen: set[str] = set()

    def walk(directory: Path, budget: int) -> None:
        for p in sorted(directory.iterdir()):
            hits.extend(check_file(p, "skill"))
            if not p.is_dir():
                continue
            if not p.is_symlink():
                walk(p, budget)
            elif budget > 0:
                try:
                    key = str(p.resolve())
                except OSError:
                    continue
                if key not in seen:
                    seen.add(key)
                    walk(p, budget - 1)

    try:
        walk(root, link_budget)
    except OSError:
        pass
    return hits


def scan_skills_and_opencode(home: Path) -> list[Hit]:
    hits: list[Hit] = []
    hits.extend(scan_instruction_files(home / ".claude"))

    claude_skills = home / ".claude" / "skills"
    if claude_skills.is_symlink():
        hits.extend(check_file(claude_skills, "skill"))
    if claude_skills.is_dir():
        hits.extend(_scan_skill_tree(claude_skills))

    opencode_dir = home / ".config" / "opencode"
    if opencode_dir.is_dir():
        hits.extend(scan_instruction_files(opencode_dir))
        for cfg_name in ("opencode.json", "opencode.jsonc"):
            cfg = opencode_dir / cfg_name
            if cfg.exists() or cfg.is_symlink():
                hits.extend(check_file(cfg, "opencode"))

        op_skills = opencode_dir / "skills"
        if op_skills.is_symlink():
            hits.extend(check_file(op_skills, "skill"))
        if op_skills.is_dir():
            hits.extend(_scan_skill_tree(op_skills))

        for p in sorted(opencode_dir.iterdir()):
            if p.is_symlink() and p.name not in ("opencode.json", "opencode.jsonc", "skills"):
                hits.extend(check_file(p, "opencode"))

    alt_cfg = home / ".config" / "opencode.json"
    if alt_cfg.exists() or alt_cfg.is_symlink():
        hits.extend(check_file(alt_cfg, "opencode"))

    return hits


def scan_systemd(home: Path) -> list[Hit]:
    hits: list[Hit] = []
    user_dir = home / ".config" / "systemd" / "user"
    if user_dir.is_dir():
        for p in sorted(user_dir.iterdir()):
            if p.name.endswith(".service") or p.name.endswith(".timer"):
                hits.extend(check_file(p, "systemd"))
            elif p.is_dir() and p.name.endswith(".d"):
                for dropin in sorted(p.iterdir()):  # autoos.service.d/*.conf
                    if dropin.name.endswith(".conf"):
                        hits.extend(check_file(dropin, "systemd"))
            elif p.is_dir() and p.name.endswith(".wants"):
                for link in sorted(p.iterdir()):  # *.wants/ enable links
                    hits.extend(check_file(link, "systemd"))
    return hits


def scan_launch_specs(home: Path) -> list[Hit]:
    hits: list[Hit] = []
    base = home / ".local" / "share" / "plangraph"
    seen: set[str] = set()

    if base.is_dir():
        for p in sorted(base.rglob("*")):
            if "backups" in p.relative_to(base).parts:
                continue  # dated snapshots, not live readers
            if p.is_file() or p.is_symlink():
                if p.name.endswith(".json") or looks_secret(p):
                    seen.add(str(p))
                    hits.extend(check_file(p, "launch-spec"))

    for candidate in (base / "keeper.json", home / "keeper.json"):
        if (candidate.exists() or candidate.is_symlink()) and str(candidate) not in seen:
            seen.add(str(candidate))
            hits.extend(check_file(candidate, "launch-spec"))

    return hits


def scan_mcp_configs(home: Path) -> list[Hit]:
    hits: list[Hit] = []
    claude_json = home / ".claude.json"
    if claude_json.exists() or claude_json.is_symlink():
        hits.extend(check_file(claude_json, "mcp"))

    claude_dir = home / ".claude"
    if claude_dir.is_dir():
        found: set[Path] = set()
        for pat in ("**/.mcp.json", "**/*.mcp.json", "**/mcp.json"):
            for p in claude_dir.glob(pat):
                found.add(p)
        for p in sorted(found):
            if "jobs" in p.relative_to(claude_dir).parts:
                continue  # per-job scratch, not live config
            hits.extend(check_file(p, "mcp"))

    return hits


def scan_bin_dir(directory: Path, kind: str) -> list[Hit]:
    """Scripts directly under *directory*: text only, symlinks followed."""
    hits: list[Hit] = []
    if directory.is_dir():
        for p in sorted(directory.iterdir()):
            if p.is_file() or p.is_symlink():
                hits.extend(check_file(p, kind, is_text_script=True))
    return hits


def scan_router_bin(home: Path) -> list[Hit]:
    return scan_bin_dir(home / ".local" / "bin", "router-bin")


class RootScanError(RuntimeError):
    """An explicitly supplied scan root cannot be read: a scan error, not a clean root."""


def _listing(directory: Path, strict: bool) -> list[Path]:
    """Sorted entries of *directory*. *strict* (an explicit root): a read failure
    raises RootScanError; otherwise it is an absent root."""
    try:
        return sorted(directory / name for name in os.listdir(directory))
    except OSError as exc:
        if strict:
            raise RootScanError(f"cannot read {directory}: {type(exc).__name__}") from exc
        return []


def scan_routing_launch(launch_dir: Path, strict: bool = False) -> list[Hit]:
    """Routing launch records (``<launch_dir>/*.json``): the specs the fleet
    router replays when it relaunches a session."""
    hits: list[Hit] = []
    if strict or launch_dir.is_dir():
        for p in _listing(launch_dir, strict):
            if p.name.endswith(".json"):
                hits.extend(check_file(p, "routing-launch"))
    return hits


def scan_routing_tree(routing_root: Path, strict: bool = False) -> list[Hit]:
    try:
        return _scan_routing_tree(routing_root, strict)
    except OSError as exc:
        if strict:
            raise RootScanError(f"cannot read {routing_root}: {type(exc).__name__}") from exc
        raise


def _scan_routing_tree(routing_root: Path, strict: bool) -> list[Hit]:
    """The routing checkout: ``ops/*.mcp.json`` (the MCP sets it launches
    sessions with) and the scripts in ``bin/``."""
    if strict:   # an explicit root that is missing, unreadable or no directory is no clean scan; keep the stat error
        try:
            mode = os.stat(routing_root).st_mode
        except OSError as exc:
            raise RootScanError(f"cannot read {routing_root}: {type(exc).__name__}") from exc
        if not stat.S_ISDIR(mode):
            raise RootScanError(f"cannot read {routing_root}: not a directory")
        _listing(routing_root, True)
    hits: list[Hit] = []
    ops = routing_root / "ops"
    try:
        ops_mode = os.stat(ops).st_mode if strict else 0
    except FileNotFoundError:
        ops_mode = 0
    except OSError as exc:
        raise RootScanError(f"cannot read {ops}: {type(exc).__name__}") from exc
    if stat.S_ISDIR(ops_mode) if strict else ops.is_dir():
        for p in _listing(ops, strict):
            if p.name.endswith(".mcp.json"):
                hits.extend(check_file(p, "routing-mcp"))
    elif strict and ops_mode:
        raise RootScanError(f"cannot read {ops}: not a directory")
    bin_dir = routing_root / "bin"
    if strict:
        try:
            bin_mode = os.stat(bin_dir).st_mode
        except FileNotFoundError:
            bin_mode = 0
        except OSError as exc:
            raise RootScanError(f"cannot read {bin_dir}: {type(exc).__name__}") from exc
        if stat.S_ISDIR(bin_mode):
            _listing(bin_dir, True)  # an unreadable bin/ must not look clean
    hits.extend(scan_bin_dir(bin_dir, "routing-bin"))
    return hits
