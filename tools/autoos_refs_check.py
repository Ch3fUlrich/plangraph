"""Scan for live references to AutoOS paths under home and repository.

Per-host command:
python3 tools/autoos_refs_check.py --home ~ --repo <plangraph repo>
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import subprocess
import sys

# Word keywords are matched on token boundaries (the filename split on
# non-alphanumerics), so "monkey" / "keyboard" do not match "key".
SECRET_KEYWORDS = frozenset(
    {
        "token",
        "secret",
        "credential",
        "credentials",
        "password",
        "passwd",
        "apikey",
        "api_key",
        "id_rsa",
        "id_ed25519",
        "key",
    }
)

SECRET_SUFFIXES = (".pem", ".key", ".p12", ".env")

# Compound keywords that contain separators; matched against the filename
# with its separators normalized to underscores.
_COMPOUND_KEYWORDS = ("id_rsa", "id_ed25519", "api_key")

_WORD_SPLIT_RE = re.compile(r"[^a-z0-9]+")

# "autoos" as a path component or word, any case: /AutoOS, AutoOS/,
# autoos-litellm.service, %h/code/AutoOS.
AUTOOS_RE = re.compile(r"(?<![A-Za-z0-9])autoos(?![A-Za-z0-9])", re.IGNORECASE)

# Redaction of co-located secret values on matched lines.
_JSON_SECRET_KEY_RE = re.compile(
    r'("(?:token|key|password|secret|authorization)"\s*:\s*)'
    r"(\"[^\"]*\"|'[^']*'|[^\s,}\]]+)",
    re.IGNORECASE,
)
_BEARER_RE = re.compile(r"(Bearer\s+)\S+", re.IGNORECASE)
_SK_RE = re.compile(r"\bsk-[A-Za-z0-9_\-]{4,}")

MAX_LINE_CHARS = 160


def looks_secret(path: Path) -> bool:
    """True when the filename looks like a secret and must not be opened."""
    name = path.name.lower()
    if name.endswith(SECRET_SUFFIXES):
        return True
    tokens = [w for w in _WORD_SPLIT_RE.split(name) if w]
    if set(tokens) & (SECRET_KEYWORDS - set(_COMPOUND_KEYWORDS)):
        return True
    for kw in _COMPOUND_KEYWORDS:
        parts = kw.split("_")
        width = len(parts)
        if any(tokens[i : i + width] == parts for i in range(len(tokens) - width + 1)):
            return True
    return False


def has_autoos(text: str) -> bool:
    """Case-insensitive match of 'autoos' as a path component or word."""
    return bool(AUTOOS_RE.search(text))


def redact(line: str) -> str:
    """Redact secret values on a matched line and truncate it."""
    line = _JSON_SECRET_KEY_RE.sub(r"\1<redacted>", line)
    line = _BEARER_RE.sub(r"\1<redacted>", line)
    line = _SK_RE.sub("<redacted>", line)
    return line[:MAX_LINE_CHARS]


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
                        detail = redact(line.rstrip("\r\n").strip())
                        hits.append((kind, str(path), detail))
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


def scan_instruction_files(directory: Path) -> list[tuple[str, str, str]]:
    """Scan instruction files (*.md, incl. AGENTS.md, CLAUDE.md) directly
    under *directory*. Non-recursive: nothing inside skills/ is walked here.
    """
    hits: list[tuple[str, str, str]] = []
    if not directory.is_dir():
        return hits
    for p in sorted(directory.iterdir()):
        if p.name.lower().endswith(".md") and (p.is_file() or p.is_symlink()):
            hits.extend(check_file(p, "instruction"))
    return hits


def scan_skills_and_opencode(home: Path) -> list[tuple[str, str, str]]:
    hits: list[tuple[str, str, str]] = []
    hits.extend(scan_instruction_files(home / ".claude"))

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
        hits.extend(scan_instruction_files(opencode_dir))
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
            if "jobs" in p.relative_to(claude_dir).parts:
                continue  # per-job scratch, not live config
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
    """Report tracked symlinks pointing at AutoOS.

    Symlinks are enumerated with ``git ls-files -s`` (mode 120000) so only
    repository-tracked links are inspected, never untracked scratch. A
    symlink whose readlink raises OSError is reported as a 'broken-link'
    note; notes do not change the exit code.
    """
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
            except OSError:
                hits.append(("repo-symlink", str(p), "broken-link"))
                continue
            if has_autoos(target):
                hits.append(("repo-symlink", str(p), target))
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

    # 'broken-link' entries are informational notes and do not affect the
    # exit code; every other hit does.
    actionable = [h for h in hits if h[2] != "broken-link"]
    return 1 if actionable else 0


if __name__ == "__main__":
    sys.exit(main())
