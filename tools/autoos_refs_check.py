"""Scan for live references to AutoOS paths under home and repository.

Per-host command:
python3 tools/autoos_refs_check.py --home ~ --repo <plangraph repo>

Exit codes: 0 clean; 1 actionable AutoOS hits; 2 scan error (a --home or
--repo that cannot be scanned); 3 only SECRET-PATH hits (secret-named files
that must not be opened: their own report class, never AutoOS findings).
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
    {"token", "secret", "credential", "credentials", "password", "passwd",
     "apikey", "api_key", "id_rsa", "id_ed25519", "key", "authorization"}
)

SECRET_SUFFIXES = (".pem", ".key", ".p12", ".env")

# Compound keywords that contain separators; matched against the filename
# with its separators normalized to underscores.
_COMPOUND_KEYWORDS = ("id_rsa", "id_ed25519", "api_key")

_WORD_SPLIT_RE = re.compile(r"[^a-z0-9]+")

# "autoos" as a path component or word, any case: /AutoOS, AutoOS/,
# autoos-litellm.service, %h/code/AutoOS.
AUTOOS_RE = re.compile(r"(?<![A-Za-z0-9])autoos(?![A-Za-z0-9])", re.IGNORECASE)

# Redaction of co-located secret values on matched lines. Keys (quoted or
# bare) are judged by is_secret_name, so SECRET_KEYWORDS is the single source
# of truth: "apikey" / "OPENAI_API_KEY" values are redacted too.
_JSON_KV_RE = re.compile(
    r"('[^']*'|\"[^\"]*\"|[A-Za-z0-9_.\-]+)(\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|[^\s,}\]]+)"
)
_BEARER_RE = re.compile(r"(Bearer\s+)\S+", re.IGNORECASE)
_SK_RE = re.compile(r"\bsk-[A-Za-z0-9_\-]{4,}")

MAX_LINE_CHARS = 160


class RepoScanError(RuntimeError):
    """The repository leg cannot run (missing git, bad path, git failure).

    A misconfigured ``--repo`` must never read as clean.
    """


def is_secret_name(name: str) -> bool:
    """True when *name* (a file name or config key) carries a secret keyword."""
    name = name.lower()
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


def looks_secret(path: Path) -> bool:
    """True when the filename looks like a secret and must not be opened."""
    return is_secret_name(path.name)


def has_autoos(text: str) -> bool:
    """Case-insensitive match of 'autoos' as a path component or word."""
    return bool(AUTOOS_RE.search(text))


def _redact_secret_value(match: re.Match[str]) -> str:
    key, sep, _ = match.groups()
    redacted = f"{key}{sep}<redacted>"
    return redacted if is_secret_name(key.strip("'\"")) else match.group(0)


def redact(line: str) -> str:
    """Redact secret values on a matched line; flatten tabs; truncate."""
    line = _JSON_KV_RE.sub(_redact_secret_value, line)
    line = _BEARER_RE.sub(r"\1<redacted>", line)
    line = _SK_RE.sub("<redacted>", line)
    line = line.replace("\t", " ").replace("\r", " ").replace("\n", " ")
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
                return [(kind, str(path), redact(target))]
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
    hits: list[tuple[str, str, str]] = []
    for name in ("settings.json", "settings.local.json"):
        settings_file = home / ".claude" / name
        if settings_file.exists() or settings_file.is_symlink():
            hits.extend(check_file(settings_file, "claude-settings"))
    return hits


def scan_claude_hooks(home: Path) -> list[tuple[str, str, str]]:
    hits: list[tuple[str, str, str]] = []
    hooks_dir = home / ".claude" / "hooks"
    if hooks_dir.is_dir():
        for p in sorted(hooks_dir.rglob("*")):
            if p.is_file() or p.is_symlink():
                hits.extend(check_file(p, "claude-hook", is_text_script=True))
    return hits


def scan_instruction_files(directory: Path) -> list[tuple[str, str, str]]:
    """Scan *.md instruction files directly under *directory* (non-recursive)."""
    hits: list[tuple[str, str, str]] = []
    if not directory.is_dir():
        return hits
    for p in sorted(directory.iterdir()):
        if p.name.lower().endswith(".md") and (p.is_file() or p.is_symlink()):
            hits.extend(check_file(p, "instruction"))
    return hits


def _scan_skill_tree(root: Path, link_budget: int = 3) -> list[tuple[str, str, str]]:
    """Scan one skills tree, traversing symlinked directories too: each hop
    costs one *link_budget* level and a resolved dir is entered once, so a
    link cycle cannot loop forever.
    """
    hits: list[tuple[str, str, str]] = []
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


def scan_skills_and_opencode(home: Path) -> list[tuple[str, str, str]]:
    hits: list[tuple[str, str, str]] = []
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


def scan_systemd(home: Path) -> list[tuple[str, str, str]]:
    hits: list[tuple[str, str, str]] = []
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
    """Report tracked symlinks pointing at AutoOS (``git ls-files`` mode 120000).

    A mode-120000 entry checked out as a regular file (``core.symlinks=false``)
    holds its link target as file text and is matched from that text; only an
    unresolvable, unreadable one is a 'broken-link' note (no exit-code effect).
    A leg that cannot run at all raises RepoScanError.
    """
    hits: list[tuple[str, str, str]] = []
    if not repo.is_dir():
        raise RepoScanError(f"--repo is not an existing directory: {repo}")
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), "ls-files", "-s"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise RepoScanError(f"cannot run git in {repo}: {exc}") from exc
    if proc.returncode != 0:
        err = proc.stderr.strip().splitlines()
        raise RepoScanError(
            f"git ls-files failed in {repo} (exit {proc.returncode}): "
            f"{err[0] if err else 'no error output'}")
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
            # A checked-out-as-regular-file entry carries its link target as
            # file text; read and match that before calling the link broken.
            try:
                target = p.read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                hits.append(("repo-symlink", str(p), "broken-link"))
                continue
        if has_autoos(target):
            hits.append(("repo-symlink", str(p), redact(target)))
    return hits


def scan_all(home: Path, repo: Path | None = None) -> list[tuple[str, str, str]]:
    if not home.is_dir():
        raise ValueError(f"home is not an existing directory: {home}")
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
    parser = argparse.ArgumentParser(description="Scan for live AutoOS references.")
    parser.add_argument("--home", type=Path, default=Path.home(),
                        help="Home directory to scan (default: Path.home())")
    parser.add_argument("--repo", type=Path, default=None,
                        help="Repository path to scan for tracked symlinks")
    args = parser.parse_args(argv)

    if not args.home.is_dir():
        print(f"autoos_refs_check: error: --home is not an existing "
              f"directory: {args.home}", file=sys.stderr)
        return 2

    try:
        hits = scan_all(home=args.home, repo=args.repo)
    except RepoScanError as exc:
        print(f"autoos_refs_check: error: {exc}", file=sys.stderr)
        return 2
    for kind, path, detail in hits:
        flat = path.replace("\t", " ").replace("\r", " ").replace("\n", " ")
        print(f"{kind}\t{flat}\t{detail}")

    # 'broken-link' is informational (no exit effect); 'SECRET-PATH' is its
    # own report class (exit 3 alone) and never gates as an AutoOS finding;
    # every other hit is actionable (exit 1).
    actionable = [h for h in hits if h[2] not in ("broken-link", "SECRET-PATH")]
    if actionable:
        return 1
    return 3 if any(h[2] == "SECRET-PATH" for h in hits) else 0


if __name__ == "__main__":
    sys.exit(main())
