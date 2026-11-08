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
        if not getattr(args, "audit", False):
            out.mkdir(parents=True, exist_ok=True)
        conn = kdb.open_db(cfg.db, **cfg.db_options)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"oversight-check: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    try:
        if getattr(args, "audit", False):
            audit_once(args.out_dir, args.router_log, args.cursor,
                       {row[0] for row in conn.execute("SELECT key FROM items")})
            return EXIT_OK
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
    audit = make(sub, "oversight-audit", help="one-shot digest and router audit")
    audit.set_defaults(command="oversight-check", audit=True)
    audit.add_argument("--out-dir", default=".", metavar="DIR")
    audit.add_argument("--cursor", required=True, type=Path)
    audit.add_argument("--router-log", required=True, type=Path)
    p = make(sub, "oversight-check",
             help="RT-OVERSIGHT-CHECKS: run the checks, write the digest, raise alarms")
    p.add_argument("--out-dir", default=".", metavar="DIR",
                   help="write DIR/digest.json, DIR/digest.sha256 and the atomic"
                        " DIR/digest.pair.json (default .)")
    p.add_argument("--no-alarms", action="store_true",
                    help="write the digest but leave projector_alarms alone")


def audit_once(out_dir, router_log, cursor, known_keys, *, emit=None):
    """Read fixtures or local files; flush output before atomically advancing.

    The cursor records both digest identity and the highest numeric D-row ID.
    A changed digest replays its findings, not previously audited router rows.
    ``known_keys`` is a supplied iterable (or zero-argument snapshot function).
    No keeper mutation or network operation is performed here.
    """
    text, sha = read_digest(Path(out_dir))
    cursor = Path(cursor)
    previous = json.loads(cursor.read_text()) if cursor.exists() else {}
    after = int(previous.get("last_row", 0))
    last = after
    findings = []
    if previous.get("sha256") != sha:
        for finding in json.loads(text):
            findings.append(f"{finding['rule']} {finding['key']}: "
                            f"{finding['detail']}")
    keys = set(known_keys() if callable(known_keys) else known_keys)
    # Router decisions are Markdown table rows, identified by the first cell.
    for line in Path(router_log).read_text().splitlines():
        match = re.match(r"\s*\|\s*\**(D-(\d+))\b", line)
        if not match:
            continue
        row_id, number = match.group(1), int(match.group(2))
        last = max(last, number)
        if number <= after:
            continue
        cells = line.strip().strip("|").split("|")
        decision = cells[2] if len(cells) >= 3 else ""
        if re.search(r"(?<![\w-])GO(?![\w-])", decision) and not re.search(
                r"\bGO-HEAD\s*[:=]?\s*`?[0-9a-fA-F]{40}(?![0-9a-fA-F])", decision):
            findings.append(f"{row_id}: GO without 40-hex GO-HEAD")
        # A directive's colon has no key suffix; keeper keys do (repo:item).
        closes = re.search(
            r"\bCloses\b\s*:?\s*(.*?)(?=\||\b\w+:(?![\w.-])|$)",
            decision, re.I)
        if closes:
            for key in re.findall(r"[\w.-]+:[\w.-]+", closes.group(1)):
                key = key.rstrip(".,;)]")
                if key not in keys:
                    findings.append(f"{row_id}: Closes unknown keeper key {key}")
    label = f"sha256 {sha} last D-{last}"
    lines = [f"oversight-audit {label}: {len(findings)} findings"
             if findings else f"oversight-audit quiet {label}"]
    lines.extend(" ".join(f.splitlines()) for f in findings)
    if len(lines) > 10:
        lines = lines[:9] + [f"dropped {len(findings) - 8} findings"]
    if emit is None:
        emit = lambda line: print(line, flush=True)
    for line in lines:
        emit(line)
    _atomic_write(cursor, (json.dumps({"sha256": sha, "last_row": last})
                           + "\n").encode("ascii"))
