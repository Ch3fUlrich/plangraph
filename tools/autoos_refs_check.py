"""Scan for live references to AutoOS paths under home and repository.

Per-host command:
python3 tools/autoos_refs_check.py --home ~ --repo <plangraph repo>
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys

SECRET_KEYWORDS = (
    "token",
    "secret",
    "credential",
    ".env",
    "id_rsa",
    ".pem",
    "password",
    "key",
)


def looks_secret(path: Path) -> bool:
    name = path.name.lower()
    return any(kw in name for kw in SECRET_KEYWORDS)


def has_autoos(text: str) -> bool:
    return "/AutoOS" in text or "AutoOS/" in text


def check_file(
    path: Path, kind: str, is_text_script: bool = False
) -> list[tuple[str, str, str]]:
    if looks_secret(path):
        return [(kind, str(path), "SECRET-PATH")]

    if path.is_symlink():
        try:
            target = os.readlink(path)
            if has_autoos(target):
                return [(kind, str(path), target)]
        except OSError:
            pass
        return []

    if path.is_file():
        hits: list[tuple[str, str, str]] = []
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                if is_text_script:
                    sample = f.read(4096)
                    if "\0" in sample:
                        return []
                    f.seek(0)
                for line in f:
                    if has_autoos(line):
                        hits.append((kind, str(path), line.rstrip("\r\n").strip()))
        except OSError:
            pass
        return hits

    return []


def scan_claude_settings(home: Path) -> list[tuple[str, str, str]]:
    settings_file = home / ".claude" / "settings.json"
    if settings_file.exists() or settings_file.is_symlink():
        return check_file(settings_file, "claude-settings")
    return []


def scan_claude_hooks(home: Path) -> list[tuple[str, str, str]]:
    hits: list[tuple[str, str, str]] = []
    hooks_dir = home / ".claude" / "hooks"
    if hooks_dir.is_dir():
        for p in sorted(hooks_dir.rglob("*")):
            if p.is_file() or p.is_symlink():
                hits.extend(check_file(p, "claude-hook", is_text_script=True))
    return hits


def scan_skills_and_opencode(home: Path) -> list[tuple[str, str, str]]:
    hits: list[tuple[str, str, str]] = []

    claude_skills = home / ".claude" / "skills"
    if claude_skills.is_symlink():
        hits.extend(check_file(claude_skills, "skill"))
    elif claude_skills.is_dir():
        for p in sorted(claude_skills.iterdir()):
            hits.extend(check_file(p, "skill"))
            if p.is_dir() and not p.is_symlink():
                for sub in sorted(p.rglob("*")):
                    if sub.is_file() or sub.is_symlink():
                        hits.extend(check_file(sub, "skill"))

    opencode_dir = home / ".config" / "opencode"
    if opencode_dir.is_dir():
        for cfg_name in ("opencode.json", "opencode.jsonc"):
            cfg = opencode_dir / cfg_name
            if cfg.exists() or cfg.is_symlink():
                hits.extend(check_file(cfg, "opencode"))

        op_skills = opencode_dir / "skills"
        if op_skills.is_symlink():
            hits.extend(check_file(op_skills, "skill"))
        elif op_skills.is_dir():
            for p in sorted(op_skills.iterdir()):
                hits.extend(check_file(p, "skill"))
                if p.is_dir() and not p.is_symlink():
                    for sub in sorted(p.rglob("*")):
                        if sub.is_file() or sub.is_symlink():
                            hits.extend(check_file(sub, "skill"))

        for p in sorted(opencode_dir.iterdir()):
            if p.is_symlink() and p.name not in ("opencode.json", "opencode.jsonc", "skills"):
                hits.extend(check_file(p, "opencode"))

    alt_cfg = home / ".config" / "opencode.json"
    if alt_cfg.exists() or alt_cfg.is_symlink():
        hits.extend(check_file(alt_cfg, "opencode"))

    return hits


def scan_systemd(home: Path) -> list[tuple[str, str, str]]:
    hits: list[tuple[str, str, str]] = []
    user_dir = home / ".config" / "systemd" / "user"
    if user_dir.is_dir():
        for p in sorted(user_dir.iterdir()):
            if p.name.endswith(".service") or p.name.endswith(".timer"):
                hits.extend(check_file(p, "systemd"))
    return hits


def scan_launch_specs(home: Path) -> list[tuple[str, str, str]]:
    hits: list[tuple[str, str, str]] = []
    base = home / ".local" / "share" / "plangraph"
    seen: set[str] = set()

    if base.is_dir():
        for p in sorted(base.rglob("*")):
            if p.is_file() or p.is_symlink():
                if p.name.endswith(".json") or looks_secret(p):
                    seen.add(str(p))
                    hits.extend(check_file(p, "launch-spec"))

    for candidate in (base / "keeper.json", home / "keeper.json"):
        if (candidate.exists() or candidate.is_symlink()) and str(candidate) not in seen:
            seen.add(str(candidate))
            hits.extend(check_file(candidate, "launch-spec"))

    return hits


def scan_mcp_configs(home: Path) -> list[tuple[str, str, str]]:
    hits: list[tuple[str, str, str]] = []
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
            hits.extend(check_file(p, "mcp"))

    return hits


def scan_router_bin(home: Path) -> list[tuple[str, str, str]]:
    hits: list[tuple[str, str, str]] = []
    bin_dir = home / ".local" / "bin"
    if bin_dir.is_dir():
        for p in sorted(bin_dir.iterdir()):
            if p.is_file() or p.is_symlink():
                hits.extend(check_file(p, "router-bin", is_text_script=True))
    return hits


def scan_repo_symlinks(repo: Path) -> list[tuple[str, str, str]]:
    hits: list[tuple[str, str, str]] = []
    if not repo.is_dir():
        return hits
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), "ls-files", "-s"],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            return hits
        for line in proc.stdout.splitlines():
            if not line.startswith("120000 "):
                continue
            parts = line.split("\t", 1)
            if len(parts) != 2:
                continue
            relpath = parts[1]
            p = repo / relpath
            if looks_secret(p):
                hits.append(("repo-symlink", str(p), "SECRET-PATH"))
                continue
            try:
                target = os.readlink(p)
                if has_autoos(target):
                    hits.append(("repo-symlink", str(p), target))
            except OSError:
                pass
    except OSError:
        pass
    return hits


def scan_all(home: Path, repo: Path | None = None) -> list[tuple[str, str, str]]:
    hits: list[tuple[str, str, str]] = []
    hits.extend(scan_claude_settings(home))
    hits.extend(scan_claude_hooks(home))
    hits.extend(scan_skills_and_opencode(home))
    hits.extend(scan_systemd(home))
    hits.extend(scan_launch_specs(home))
    hits.extend(scan_mcp_configs(home))
    hits.extend(scan_router_bin(home))
    if repo is not None:
        hits.extend(scan_repo_symlinks(repo))
    return hits


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Scan for live references to AutoOS paths."
    )
    parser.add_argument(
        "--home",
        type=Path,
        default=Path.home(),
        help="Home directory to scan (default: Path.home())",
    )
    parser.add_argument(
        "--repo",
        type=Path,
        default=None,
        help="Repository path to scan for tracked symlinks",
    )
    args = parser.parse_args(argv)

    hits = scan_all(home=args.home, repo=args.repo)
    for kind, path, detail in hits:
        print(f"{kind}\t{path}\t{detail}")

    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
