"""``oversight-check`` (RT-OVERSIGHT-CHECKS): the local oversight pass.

Runs :func:`plangraph.keeper.oversight.run_checks` over the configured keeper
DB with the real clock, handing stall's R1/R2 the config-style ``git`` and R4 a
real ``git(args) -> str`` adapter over the first configured repo (no fetch).
Then publishes ``DIR/digest.json`` and ``DIR/digest.sha256`` through
:func:`write_digest`, whose last atomic write is ``DIR/digest.pair.json`` (the
sha and text of one run) so :func:`read_digest` never returns a mismatched
pair.  Findings raise ``projector_alarms`` rows (``--no-alarms`` skips that
half).

Exit 0 even with findings; nonzero (2) only on a config/runtime failure.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import config as config_mod, db as kdb
from .cli_verbs import EXIT_OK, EXIT_REFUSED

__all__ = ["cmd_oversight_check", "add_parser", "write_digest", "read_digest",
           "DigestPairError", "PAIR_FILE"]

#: The atomic commit record of one digest: ``{"sha256": ..., "text": ...}``.
PAIR_FILE = "digest.pair.json"


class DigestPairError(RuntimeError):
    """The on-disk digest files do not describe one payload."""


def _atomic_write(path: Path, data: bytes) -> None:
    """Write *data* to *path* through a temp file in the same directory."""
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".",
                               suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _matches(text, sha):
    """True when *text* is an ASCII payload whose sha256 is *sha*."""
    if not isinstance(text, str) or not isinstance(sha, str):
        return False
    try:
        return hashlib.sha256(text.encode("ascii")).hexdigest() == sha
    except UnicodeEncodeError:
        return False


def write_digest(out: Path, text: str, sha: str) -> None:
    """Publish one digest so readers can never observe a mismatched pair.

    The two compatibility files are written first, then ``digest.pair.json``
    holds both values and is renamed into place last.  That pair file is the
    commit record: a crash between any two writes leaves the previous complete
    pair readable, while a reader that only knows the two old files still gets
    them in the usual form.
    """
    out = Path(out)
    _atomic_write(out / "digest.json", text.encode("ascii"))
    _atomic_write(out / "digest.sha256",
                  f"{sha}  digest.json\n".encode("ascii"))
    pair = json.dumps({"sha256": sha, "text": text}, sort_keys=True,
                      separators=(",", ":"), ensure_ascii=True) + "\n"
    _atomic_write(out / PAIR_FILE, pair.encode("ascii"))


def read_digest(out: Path) -> tuple[str, str]:
    """The last committed ``(text, sha256)`` under *out*, always consistent.

    ``digest.pair.json`` wins when present because it is written last and
    atomically.  A directory without it (written by an older ``oversight-check``)
    is accepted only when ``digest.json`` and ``digest.sha256`` agree.  A torn,
    incomplete or unparsable set raises :class:`DigestPairError` rather than
    returning a pair whose sha does not describe its text.
    """
    out = Path(out)
    pair = out / PAIR_FILE
    if pair.is_file():
        try:
            data = json.loads(pair.read_text(encoding="ascii"))
            text, sha = data["text"], data["sha256"]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise DigestPairError(f"unreadable {PAIR_FILE}: {exc}") from exc
        if not _matches(text, sha):
            raise DigestPairError(f"{PAIR_FILE} does not match its payload")
        return text, sha
    try:
        text = (out / "digest.json").read_text(encoding="ascii")
        recorded = (out / "digest.sha256").read_text(encoding="ascii")
    except (OSError, ValueError) as exc:
        raise DigestPairError(f"incomplete digest files: {exc}") from exc
    match = re.search(r"[0-9a-f]{64}", recorded)
    if match is None:
        raise DigestPairError("digest.sha256 records no sha256 digest")
    sha = match.group(0)
    if not _matches(text, sha):
        raise DigestPairError("digest.json does not match digest.sha256")
    return text, sha


def _config(session):
    """The session config, or ``None`` with a one-line reason on stderr."""
    try:
        cfg = session.config
    except config_mod.ConfigError as exc:
        print(f"oversight-check config: {exc}", file=sys.stderr)
        return None
    if cfg is None:
        print("oversight-check needs --config or PG_CONFIG", file=sys.stderr)
    return cfg


def _repo_paths(cfg):
    """Working tree paths from ``cfg.repos`` in config order (str or ``{path}``)."""
    paths = []
    for entry in (getattr(cfg, "repos", None) or {}).values():
        path = entry.get("path") if isinstance(entry, dict) else entry
        if path:
            paths.append(str(path))
    return paths


def _lane_git(cfg):
    """R4's adapter: ``git(args) -> stdout`` for the first configured repo.

    Runs ``git -C <repo> <args>`` with list argv (never a shell), a 20 s
    timeout, and no fetch; a non-zero exit raises.  ``None`` when no repo is
    configured, in which case R4 skips silently.
    """
    paths = _repo_paths(cfg)
    if not paths:
        return None
    repo = paths[0]

    def git(args):
        argv = ["git", "-C", repo] + [str(arg) for arg in args]
        proc = subprocess.run(argv, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True, timeout=20,
                              check=False)
        if proc.returncode != 0:
            raise RuntimeError(
                f"{' '.join(argv)} failed ({proc.returncode}): {proc.stderr.strip()}")
        return proc.stdout

    return git


def cmd_oversight_check(args, session):
    from . import oversight     # lazy: keeps the cli module's import graph light
    cfg = _config(session)
    if cfg is None:
        return EXIT_REFUSED
    try:
        out = Path(getattr(args, "out_dir", None) or ".")
        out.mkdir(parents=True, exist_ok=True)
        conn = kdb.open_db(cfg.db, **cfg.db_options)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"oversight-check: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    try:
        now = time.time()
        # Two distinct git shapes: stall's R1/R2 understand the config object,
        # R4 needs a `git(args) -> str` callable (see oversight.run_checks).
        lane_git = _lane_git(cfg)
        findings = oversight.run_checks(
            conn, now,
            git=cfg if getattr(cfg, "repos", None) else None,
            lane_git=lane_git)
        text, sha = oversight.digest(findings)
        write_digest(out, text, sha)
        if getattr(args, "no_alarms", False):
            new = 0
        else:
            # No lane git means R4 did not run: its alarms are kept open.
            new = oversight.raise_alarms(
                conn, findings, now,
                keep_rules=() if lane_git is not None else ("R4",))
    except Exception as exc:
        print(f"oversight-check: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    finally:
        conn.close()
    print(f"oversight: {len(findings)} findings, {new} new alarms, sha256 {sha}")
    return EXIT_OK


def add_parser(sub, make):
    p = make(sub, "oversight-check",
             help="RT-OVERSIGHT-CHECKS: run the checks, write the digest, raise alarms")
    p.add_argument("--out-dir", default=".", metavar="DIR",
                   help="write DIR/digest.json, DIR/digest.sha256 and the atomic"
                        " DIR/digest.pair.json (default .)")
    p.add_argument("--no-alarms", action="store_true",
                   help="write the digest but leave projector_alarms alone")
