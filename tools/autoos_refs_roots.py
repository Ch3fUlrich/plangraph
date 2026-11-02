"""The scan roots of tools/autoos_refs_check.py: one function per place a live
reader (Claude Code, OpenCode, systemd, the routing fleet) can still load
AutoOS from. Every function returns ``(kind, path, detail)`` hits and never
opens a secret-named file; symlinked files are followed by ``check_file``.
"""

from __future__ import annotations

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


def scan_routing_launch(launch_dir: Path) -> list[Hit]:
    """Routing launch records (``<launch_dir>/*.json``): the specs the fleet
    router replays when it relaunches a session."""
    hits: list[Hit] = []
    if launch_dir.is_dir():
        for p in sorted(launch_dir.glob("*.json")):
            hits.extend(check_file(p, "routing-launch"))
    return hits


def scan_routing_tree(routing_root: Path) -> list[Hit]:
    """The routing checkout: ``ops/*.mcp.json`` (the MCP sets it launches
    sessions with) and the scripts in ``bin/``."""
    hits: list[Hit] = []
    ops = routing_root / "ops"
    if ops.is_dir():
        for p in sorted(ops.glob("*.mcp.json")):
            hits.extend(check_file(p, "routing-mcp"))
    hits.extend(scan_bin_dir(routing_root / "bin", "routing-bin"))
    return hits
