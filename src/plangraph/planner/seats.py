"""Review seats per writer part and the ``diff_ref`` card check (split from routing.py)."""
from __future__ import annotations

import os
import re
from collections.abc import Mapping

from plangraph.routing_errors import SpawnRefused

#: A git revision the inline-diff path accepts: no option-looking or spaced refs.
_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/~^@{}-]{0,199}$")


def _valid_diff_path(path):
    """Repo-relative diff pathspec with no option injection or traversal."""
    if not isinstance(path, str) or not path or any(ord(c) < 32 or 127 <= ord(c) < 160 for c in path):
        return False
    if path[0] in ("-", "/", "\\", ":") or (len(path) >= 2 and path[1] == ":" and path[0].isalpha()):
        return False
    if os.path.isabs(path):
        return False
    parts = re.split(r"[/\\]", path)
    return ".." not in parts


def _check_diff_ref(ref):
    """``card["diff_ref"]`` = ``{"repo": <absolute path>, "base": <rev>, "head": <rev>, ["paths": [...]]}``."""
    if (not isinstance(ref, Mapping)
            or not {"repo", "base", "head"} <= set(ref)
            or not set(ref) <= {"repo", "base", "head", "paths"}
            or not all(isinstance(ref[k], str) for k in ("repo", "base", "head"))
            or not os.path.isabs(ref["repo"])
            or not _REF_RE.match(ref["base"]) or not _REF_RE.match(ref["head"])):
        raise SpawnRefused('diff_ref must be {"repo": absolute path, "base": rev, "head": rev}')
    if "paths" in ref:
        paths = ref["paths"]
        if (not isinstance(paths, (list, tuple)) or not paths
                or not all(_valid_diff_path(p) for p in paths)):
            raise SpawnRefused("diff_ref paths must be a non-empty list of repo-relative paths")


def seat_family(family):
    """Keeper family of a leg or writer family (review/family.py: claude=anthropic, gpt=openai, qwen=alibaba)."""
    from plangraph.review.family import _family_key
    return _family_key(family)


def claude_writer_fence(role, writer_set):
    """D-3605: a claude/anthropic writer part fences every claude-family review leg (the Haiku leg)."""
    # D-2511: a seat family must differ from every writer family, so all claude-family legs are fenced.
    return {"claude"} if role == "review" and "anthropic" in {seat_family(w) for w in writer_set} else set()


def writer_parts(writer_families):
    """Keeper families of the writer parts; an unknown writer refuses (``writer_key``, fail closed)."""
    from plangraph.review.family import FamilyError, writer_key
    try:
        return {writer_key(w) for w in writer_families if str(w or "").strip()}
    except FamilyError as exc:
        raise SpawnRefused(str(exc)) from None


def family_spellings(families, legs):
    """``families`` plus every leg family that is one of them for the keeper (claude/anthropic, gpt/openai)."""
    keys = {seat_family(f) for f in families}
    return set(families) | keys | {leg.family for leg in legs if seat_family(leg.family) in keys}


def pick_seats(ordered, writer_families, *, high):
    """Seats covering every writer part (DECISIONS 3.2, keeper ``coverage.uncovered``).

    A part (writer family) is covered by ``need`` distinct seat families other
    than its own (1 standard, 2 high); a high part also needs one non-Claude
    family other than its own.  A seat may share a family with ANOTHER writer
    (D-3084).  Seats are taken in route order, one per family, only while they
    help cover an open part; at least two families are always picked.  Returns
    ``(seats, coverage, open_parts)``.
    """
    from plangraph.keeper.coverage import uncovered
    writers = sorted({seat_family(w) for w in writer_families if w})
    need = 2 if high else 1
    seats, families = [], []
    for leg in ordered:
        fam = seat_family(leg.family)
        if fam in families or (writers and all(fam == w for w in writers)):
            continue
        open_parts = uncovered(writers, families, need=need, sensitive=high)
        if len(families) >= 2 and not any(fam != w for w in open_parts):
            continue
        seats.append(leg)
        families.append(fam)
        if len(families) >= 2 and not uncovered(writers, families, need=need, sensitive=high):
            break
    coverage = tuple({"leg": leg.id, "covers": [w for w in writers if w != seat_family(leg.family)]}
                     for leg in seats)
    return seats, coverage, uncovered(writers, families, need=need, sensitive=high)


def head_writer_families(card):
    """Families named by the ``Writer`` trailer(s) of the head commit of ``card["diff_ref"]``.

    Empty without a ``diff_ref`` or a trailer. A trailer naming an unknown writer refuses (fail
    closed, like ``writer_parts``): the head's writer must never review its own work.
    """
    ref = card.get("diff_ref") if isinstance(card, Mapping) else None
    if ref is None:
        return []
    if isinstance(ref, Mapping) and "repo_id" in ref and "repo" not in ref:
        # A registered-repository ref ({repo_id, base_sha, head_sha}) is resolved only by the Rust server, which adds the trailer families to writer_families (launch::with_head_writers).
        return []
    _check_diff_ref(ref)
    import subprocess

    def git(*args):
        try:
            return subprocess.run(["git", "-C", ref["repo"], *args], stdin=subprocess.DEVNULL, capture_output=True,
                                  text=True, encoding="utf-8", errors="replace", timeout=30, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SpawnRefused(f"cannot read the Writer trailer of {ref['head'][:12]}: {type(exc).__name__}") from None
    # One commit: a path, a range (a..b), a negation or a missing revision is not a head to read.
    sha = git("rev-parse", "--verify", "--quiet", "--end-of-options", ref["head"] + "^{commit}")
    resolved = sha.stdout.strip()
    if sha.returncode != 0 or len(resolved) not in (40, 64) or not all(c in "0123456789abcdef" for c in resolved):
        # An unreadable head is not an absent trailer: the head's writer could not be excluded.
        raise SpawnRefused(f"cannot read the Writer trailer of {ref['head'][:12]}: not a single commit")
    proc = git("log", "-1", "--no-color", "--format=%(trailers:key=Writer,valueonly,unfold)", resolved, "--")
    if proc.returncode != 0:
        raise SpawnRefused(f"cannot read the Writer trailer of {ref['head'][:12]}: git exit {proc.returncode}")
    out = proc.stdout
    # "Writer: <model> <session> (<note>)": the model is the first word.
    models = [line.split()[0] for line in out.splitlines() if line.strip()]
    from plangraph.review.family import FamilyError, writer_key
    families = []
    for model in models:
        try:
            writer_key(model)
        except FamilyError:
            raise SpawnRefused(f"unknown writer in Writer trailer of {ref['head'][:12]}: {model[:64]}") from None
        families.append(model)
    return families


def with_head_writers(card, role, families):
    """``families`` plus the head ``Writer`` trailer families of a review card (each once): the one
    set used for planning, launch headers and the run record."""
    families = list(families)
    if role == "review":
        families += [f for f in head_writer_families(card) if f not in families]
    return families
