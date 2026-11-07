"""Scan for live references to AutoOS paths under home and repository.

Per-host command:
python3 tools/autoos_refs_check.py --home ~ --repo <plangraph repo>

Scanned: Claude settings, hooks, skills and instruction files; OpenCode
config; systemd user units; plangraph launch specs; MCP configs; ~/.local/bin;
the routing launch records (``--launch-dir``, default
``<home>/.local/state/routing/launch``, ``*.json``); the routing checkout's
``ops/*.mcp.json`` and ``bin/`` (``--routing-root``; see ``--help`` for its
default); tracked repo symlinks (``--repo``). A symlinked file
is followed to its content. Detail lines are masked by
tools/hooks/redact_common.py before they are printed.

Exit codes: 0 clean; 1 actionable AutoOS hits; 2 scan error (a --home,
--repo, --launch-dir or --routing-root that cannot be scanned); 3 only
SECRET-PATH hits (secret-named files that must not be opened: their own report
class, never AutoOS findings).
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys

from autoos_refs_core import (  # noqa: F401  (re-exported: the scanner's public names)
    Hit, check_file, has_autoos, is_secret_name, looks_secret, redact,
)
from autoos_refs_roots import (
    scan_claude_hooks, scan_claude_settings, scan_launch_specs, scan_mcp_configs,
    scan_router_bin, scan_routing_launch, scan_routing_tree, scan_skills_and_opencode,
    scan_systemd, RootScanError,
)


class RepoScanError(RuntimeError):
    """The repository leg cannot run (missing git, bad path, git failure).

    A misconfigured ``--repo`` must never read as clean.
    """


def scan_repo_symlinks(repo: Path) -> list[Hit]:
    """Report tracked symlinks pointing at AutoOS (``git ls-files``, mode 120000).

    The index is read with ``-z``: without it git quotes a non-ASCII name and
    the link would be looked up under that quoted text and reported as broken.
    A mode-120000 entry checked out as a regular file (``core.symlinks=false``)
    holds its link target as file text and is matched from that text; only an
    unresolvable, unreadable one is a 'broken-link' note (no exit-code effect).
    A leg that cannot run at all raises RepoScanError.
    """
    hits: list[Hit] = []
    if not repo.is_dir():
        raise RepoScanError(f"--repo is not an existing directory: {repo}")
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), "ls-files", "-s", "-z"],
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        raise RepoScanError(f"cannot run git in {repo}: {exc}") from exc
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        raise RepoScanError(
            f"git ls-files failed in {repo} (exit {proc.returncode}): "
            f"{err[0] if err else 'no error output'}")
    for record in proc.stdout.split(b"\0"):
        meta, _, raw_path = record.partition(b"\t")  # "<mode> <object> <stage>\t<path>"
        if not meta.startswith(b"120000 ") or not raw_path:
            continue
        p = repo / os.fsdecode(raw_path)
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


def scan_all(
    home: Path,
    repo: Path | None = None,
    launch_dir: Path | None = None,
    routing_root: Path | None = None,
) -> list[Hit]:
    """Every scan root. *launch_dir* / *routing_root* default to the standard
    places under *home*; a root that does not exist there is simply absent."""
    if not home.is_dir():
        raise ValueError(f"home is not an existing directory: {home}")
    explicit_launch, explicit_routing = launch_dir is not None, routing_root is not None
    if launch_dir is None:
        launch_dir = home / ".local" / "state" / "routing" / "launch"
    if routing_root is None:
        routing_root = home / "code" / "routing"
    hits: list[Hit] = []
    hits.extend(scan_claude_settings(home))
    hits.extend(scan_claude_hooks(home))
    hits.extend(scan_skills_and_opencode(home))
    hits.extend(scan_systemd(home))
    hits.extend(scan_launch_specs(home))
    hits.extend(scan_mcp_configs(home))
    hits.extend(scan_router_bin(home))
    hits.extend(scan_routing_launch(launch_dir, strict=explicit_launch))
    hits.extend(scan_routing_tree(routing_root, strict=explicit_routing))
    if repo is not None:
        hits.extend(scan_repo_symlinks(repo))
    return hits


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scan for live AutoOS references.")
    parser.add_argument("--home", type=Path, default=Path.home(),
                        help="Home directory to scan (default: Path.home())")
    parser.add_argument("--repo", type=Path, default=None,
                        help="Repository path to scan for tracked symlinks")
    parser.add_argument("--launch-dir", type=Path, default=None,
                        help="Routing launch records (default: "
                             "<home>/.local/state/routing/launch)")
    parser.add_argument("--routing-root", type=Path, default=None,
                        help="Routing checkout holding ops/ and bin/ "
                             "(default: <home>/code/routing)")
    args = parser.parse_args(argv)

    if not args.home.is_dir():
        print(f"autoos_refs_check: error: --home is not an existing "
              f"directory: {args.home}", file=sys.stderr)
        return 2
    for flag, given in (("--launch-dir", args.launch_dir),
                        ("--routing-root", args.routing_root)):
        if given is not None and not given.is_dir():
            print(f"autoos_refs_check: error: {flag} is not an existing "
                  f"directory: {given}", file=sys.stderr)
            return 2

    try:
        hits = scan_all(home=args.home, repo=args.repo,
                        launch_dir=args.launch_dir, routing_root=args.routing_root)
    except (RepoScanError, RootScanError) as exc:
        print(f"autoos_refs_check: error: {exc}", file=sys.stderr)
        return 2
    for kind, path, detail in hits:
        flat = path.replace("\t", " ").replace("\r", " ").replace("\n", " ")
        # A tracked name that is not valid UTF-8 prints escaped, never crashes.
        print(f"{kind}\t{flat}\t{detail}".encode("utf-8", "backslashreplace").decode("utf-8"))

    # 'broken-link' is informational (no exit effect); 'SECRET-PATH' is its
    # own report class (exit 3 alone) and never gates as an AutoOS finding;
    # every other hit is actionable (exit 1).
    actionable = [h for h in hits if h[2] not in ("broken-link", "SECRET-PATH")]
    if actionable:
        return 1
    return 3 if any(h[2] == "SECRET-PATH" for h in hits) else 0


if __name__ == "__main__":
    sys.exit(main())
