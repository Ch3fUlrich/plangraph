"""``GitAdapter.writer_trailers`` against a REAL temporary git repository.

Items 15 and 16 of the SEAT-COVERAGE-KEEPER ADDENDUM: a merge (or PHASE merge)
evidence commit carries only ``Plan-Item``, so the writer trailers come from
the branch commits in ``merge-base(parent1, parent2)..parent2`` -- never from
the merge commit itself, from main-side commits, or from inner PR merges.

Hermetic: everything lives in ``tmp_path``, the repo sets its own
``user.name``/``user.email``, the host's global/system git config is ignored,
and nothing is pushed or fetched (no network).  The implementation under test,
``GitAdapter.writer_trailers``, does not exist yet, so these tests are red.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from plangraph.keeper.evidence_git import GitAdapter
from plangraph.keeper.errors import Refused

pytestmark = pytest.mark.skipif(shutil.which("git") is None,
                                reason="git not available")


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, check=True)
    return proc.stdout.strip()


def _commit(repo: Path, subject: str, trailers: dict[str, str],
            *, email: str | None = None) -> str:
    """One ``--allow-empty`` commit; returns its sha."""
    if email:
        _git(repo, "config", "user.email", email)
    body = "".join(f"{key}: {value}\n" for key, value in trailers.items())
    _git(repo, "commit", "--allow-empty", "-q", "-m", f"{subject}\n\n{body}")
    return _git(repo, "rev-parse", "HEAD")


def _merge(repo: Path, branch: str, plan_item: str) -> str:
    """A ``--no-ff`` merge carrying only ``Plan-Item``; returns its sha."""
    _git(repo, "merge", "--no-ff", "-q", "-m",
         f"Merge {branch}\n\nPlan-Item: {plan_item}", branch)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture(autouse=True)
def hermetic_git(tmp_path, monkeypatch):
    """Ignore the host's git config; keep git inside ``tmp_path``."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")


@pytest.fixture
def repo(tmp_path: Path, hermetic_git) -> Path:
    """A fresh repo with local identity and signing disabled."""
    path = tmp_path / "r"
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True,
                   capture_output=True)
    _git(path, "config", "user.name", "Test Writer")
    _git(path, "config", "user.email", "writer@example.invalid")
    _git(path, "config", "commit.gpgsign", "false")
    return path


@pytest.fixture
def adapter() -> GitAdapter:
    return GitAdapter()


# --------------------------------------------------------------------------
# 15. a plain commit, then a --no-ff merge of a two-commit branch
# --------------------------------------------------------------------------

@pytest.mark.parametrize("subject", ["Writer: deepseek-r1 test-run-2", "**Writer:** deepseek-r1 test-run-2"])
def test_writer_subject_refuses_partial_family_list(repo, adapter, subject):
    sha = _commit(repo, subject, {"Plan-Item": "demo:A", "Writer": "mimo-v2 test-run-1"})
    assert adapter.trailers(str(repo), sha)["Writer"] == "mimo-v2 test-run-1"
    assert adapter.writer_trailers(str(repo), sha) == []
    with pytest.raises(Refused) as exc:
        _families(repo, sha)
    assert exc.value.code == "writer_unknown"


def test_whitespace_paragraph_separator_is_not_writer_continuation(repo, adapter):
    _git(repo, "commit", "--allow-empty", "--cleanup=verbatim", "-q", "-m",
         "squash\n\nWriter: mimo-v2 test-run-1\n \t \nPlan-Item: demo:A\n")
    sha = _git(repo, "rev-parse", "HEAD")
    assert adapter.writer_trailers(str(repo), sha) == [
        {"Writer": "mimo-v2 test-run-1", "Plan-Item": "demo:A"}]


@pytest.mark.parametrize("value", ["mimo-v2 deepseek-v4 test-run-1", "mimo-v2, deepseek-v4",
                                        "mimo-v2 [deepseek-v4]", "mimo-v2 (deepseek-v4)"])
def test_multiple_models_in_one_writer_value_refuse(repo, value):
    sha = _commit(repo, "squash", {"Plan-Item": "demo:A", "Writer": value})
    with pytest.raises(Refused) as exc:
        _families(repo, sha)
    assert exc.value.code == "writer_mixed_family"


@pytest.mark.parametrize("seat_family", ["xiaomi", "google"])
def test_real_squash_closing_verdict_covers_both_writers(repo, seat_family):
    from evidence_fakes import make_ctx
    from keeper_util import World, seed
    from plangraph.keeper import auth

    _git(repo, "commit", "--allow-empty", "-q", "-m",
         "squash\n\nPlan-Item: demo:A\nWriter: mimo-v2 test-run-1\nWriter: deepseek-r1 test-run-2\n")
    sha = _git(repo, "rev-parse", "HEAD")
    world = World(repo.parent / "keeper")
    try:
        token = auth.add_principal(world.conn, "squashReviewer", "reviewer", ["demo"],
                                   family=seat_family)
        reviewer = auth.authenticate(world.conn, token)
        (key,) = seed(world, [("A", [])], evidence_kinds=("commit+ci",))
        world.store.ctx = make_ctx(git=GitAdapter({"demo": str(repo)}))
        epoch = world.store.claim(world.p("workerA"), key, lease_s=7200)["epoch"]
        assert world.store.add_evidence(world.p("workerA"), key, epoch,
                                        "commit+ci", {"commit": sha})["verified"]
        world.store.submit(world.p("workerA"), key, epoch)
        if seat_family == "xiaomi":
            with pytest.raises(Refused) as exc:
                world.store.verdict(reviewer, key, True, "reviewed")
            assert exc.value.code == "coverage"
            assert world.store._item(key)["state"] == "in_review"
        else:
            world.store.verdict(reviewer, key, True, "reviewed")
            assert world.store._item(key)["state"] == "done"
    finally:
        world.close()


def test_non_merge_commit_returns_its_own_trailers(repo, adapter):
    sha = _commit(repo, "one", {"Plan-Item": "demo:A", "Writer": "mimo-v2 test-run-1"})

    assert adapter.writer_trailers(str(repo), sha) == [
        {"Plan-Item": "demo:A", "Writer": "mimo-v2 test-run-1"}]


def test_merge_returns_the_branch_commits_only(repo, adapter):
    _commit(repo, "base", {"Plan-Item": "demo:BASE", "Writer": "qwen-max test-run-0"})
    _git(repo, "switch", "-q", "-c", "feat")
    f1 = _commit(repo, "f1", {"Plan-Item": "demo:A", "Writer": "mimo-v2 test-run-1"})
    f2 = _commit(repo, "f2", {"Plan-Item": "demo:A", "Writer": "deepseek-r1 test-run-2"})
    _git(repo, "switch", "-q", "main")
    # Main-side commit made AFTER the branch forked: outside the range.
    _commit(repo, "side", {"Plan-Item": "demo:SIDE", "Writer": "qwen-max test-run-9"})
    merge = _merge(repo, "feat", "demo:MERGE")

    got = adapter.writer_trailers(str(repo), merge)
    assert len(got) == 2
    assert {d.get("Writer") for d in got} == {"mimo-v2 test-run-1", "deepseek-r1 test-run-2"}
    # Neither the merge commit (demo:MERGE) nor base/side commits leak in.
    assert {d.get("Plan-Item") for d in got} == {"demo:A"}
    assert adapter.writer_trailers(str(repo), f1) == [
        {"Plan-Item": "demo:A", "Writer": "mimo-v2 test-run-1"}]
    assert adapter.writer_trailers(str(repo), f2) == [
        {"Plan-Item": "demo:A", "Writer": "deepseek-r1 test-run-2"}]


# --------------------------------------------------------------------------
# 16. (D-2308) a PHASE merge over inner PR merges: leaf commits only
# --------------------------------------------------------------------------

def test_phase_merge_returns_leaf_trailers_and_skips_inner_merges(repo, adapter):
    _commit(repo, "base", {"Plan-Item": "demo:BASE"})
    _git(repo, "switch", "-q", "-c", "phase")
    # PR 1 branched off the phase branch, merged back with --no-ff.
    _git(repo, "switch", "-q", "-c", "featA")
    _commit(repo, "a1", {"Plan-Item": "demo:LEAF", "Writer": "mimo-v2 test-run-1"})
    _commit(repo, "a2", {"Plan-Item": "demo:LEAF", "Writer": "deepseek-r1 test-run-2"})
    _git(repo, "switch", "-q", "phase")
    _merge(repo, "featA", "demo:PR-A")
    # PR 2, then the phase branch itself merges into main.
    _git(repo, "switch", "-q", "-c", "featB")
    _commit(repo, "b1", {"Plan-Item": "demo:LEAF", "Writer": "claude-opus test-run-1"})
    _git(repo, "switch", "-q", "phase")
    _merge(repo, "featB", "demo:PR-B")
    _git(repo, "switch", "-q", "main")
    phase = _merge(repo, "phase", "demo:PHASE")

    got = adapter.writer_trailers(str(repo), phase)
    assert len(got) == 3
    assert {d.get("Writer") for d in got} == {
        "mimo-v2 test-run-1", "deepseek-r1 test-run-2", "claude-opus test-run-1"}
    # Inner PR merges (demo:PR-A/demo:PR-B) and the PHASE merge are dropped.
    assert {d.get("Plan-Item") for d in got} == {"demo:LEAF"}


# --------------------------------------------------------------------------
# 17. an OCTOPUS merge (3+ parents) is out of scope: fail closed, never
#     ignore the commits behind parents[2:]
# --------------------------------------------------------------------------

def test_octopus_merge_returns_no_writer_trailers(repo, adapter):
    _commit(repo, "base", {"Plan-Item": "demo:BASE"})
    _git(repo, "branch", "b1")
    _git(repo, "branch", "b2")
    _git(repo, "switch", "-q", "b1")
    _commit(repo, "w1", {"Plan-Item": "demo:A", "Writer": "mimo-v2 test-run-1"})
    _git(repo, "switch", "-q", "b2")
    _commit(repo, "w2", {"Plan-Item": "demo:A", "Writer": "deepseek-r1 test-run-2"})
    _git(repo, "switch", "-q", "main")
    _commit(repo, "side", {"Plan-Item": "demo:SIDE", "Writer": "qwen-max test-run-9"})
    # One `git merge` of two branches at once: three parents.
    _git(repo, "merge", "--no-ff", "-q", "-m",
         "Octopus\n\nPlan-Item: demo:OCTOPUS", "b1", "b2")
    octopus = _git(repo, "rev-parse", "HEAD")
    assert len(_git(repo, "rev-list", "--parents", "-n", "1",
                    octopus).split()) == 4

    # A writer behind parents[2:] must never be silently skipped: the adapter
    # cannot tell who wrote an octopus, so the keeper gets [] and fails closed.
    assert adapter.writer_trailers(str(repo), octopus) == []


# --------------------------------------------------------------------------
# 18. a Writer BLOCK followed by a SEPARATE trailer paragraph: the keeper must
#     still read the writer (git's ``%(trailers)`` reports only the LAST
#     paragraph of the message)
# --------------------------------------------------------------------------

def test_writer_block_before_a_separate_trailer_paragraph_is_still_read(repo, adapter):
    """A blank line after the ``Writer:`` line used to hide it from the keeper.

    ``git show -s --format=%(trailers)`` returns only the message's last
    paragraph, so for this message it yields the ``Co-Authored-By`` line and
    nothing else -- the ``Writer:`` line (and with it the ``Plan-Item:`` line)
    is dropped, and the seat-coverage check can never see that writer.  The
    adapter reads the whole message body instead, so both paragraphs survive.
    """
    _git(repo, "commit", "--allow-empty", "-q", "-m",
         "squash the branch\n\n"
         "Plan-Item: demo:A\n"
         "Writer: mimo-v2 test-run-1\n"
         "\n"
         "Co-Authored-By: Tester <t@example.invalid>")
    sha = _git(repo, "rev-parse", "HEAD")

    git_trailers = _git(repo, "show", "-s", "--format=%(trailers)", sha)
    assert "Co-Authored-By:" in git_trailers
    assert "Writer:" not in git_trailers      # git's own view of the message

    want = {"Plan-Item": "demo:A",
            "Writer": "mimo-v2 test-run-1",
            "Co-Authored-By": "Tester <t@example.invalid>"}
    assert adapter.trailers(str(repo), sha) == want
    assert adapter.writer_trailers(str(repo), sha) == [want]


# --------------------------------------------------------------------------
# 19. a SQUASHED commit: several ``Writer:`` lines on ONE leaf commit, every
#     value kept (the seat-coverage check needs every writer family)
# --------------------------------------------------------------------------

def test_two_writer_lines_on_one_commit_keep_every_value(repo, adapter):
    _git(repo, "commit", "--allow-empty", "-q", "-m",
         "squash the branch\n\n"
         "Plan-Item: demo:A\n"
         "Writer: mimo-v2 test-run-1\n"
         "Writer: deepseek-r1 test-run-2\n")
    sha = _git(repo, "rev-parse", "HEAD")

    assert adapter.writer_trailers(str(repo), sha) == [{
        "Plan-Item": "demo:A",
        "Writer": ["mimo-v2 test-run-1", "deepseek-r1 test-run-2"],
    }]


# --------------------------------------------------------------------------
# 20. (FIX r1) prose paragraphs are NOT trailer blocks: a ``Hand-Edit:`` /
#     ``Writer:`` line quoted inside prose is prose, and never reaches the
#     seat-coverage writer families
# --------------------------------------------------------------------------

def _families(repo, sha):
    """``coverage.writer_families`` for one verified ``commit+ci`` row at *sha*."""
    from types import SimpleNamespace

    from plangraph.keeper.coverage import writer_families

    rows = [{"epoch": 1, "verified": True, "kind": "commit+ci",
             "ref": {"commit": sha}}]
    store = SimpleNamespace(_evidence=lambda key: rows)
    ctx = SimpleNamespace(git=GitAdapter({"demo": str(repo)}))
    return writer_families(store, {"key": "demo:X", "epoch": 1,
                                   "project": "demo"}, ctx)


@pytest.mark.parametrize("body,expected", [
    ("Writer: ", None),
    ("Writer: deepseek-v4\nWriter: ", None),
    ("Hand-Edit: manual\nWriter: ", None),
    ("Writer: deepseek-v4\nHand-Edit: ", ["deepseek"]),
    ("Hand-Edit: ", None),
], ids=["blank-writer", "blank-writer-after-writer", "blank-writer-after-hand",
        "blank-hand-with-writer", "blank-hand-only"])
def test_fix10_real_parser_blank_attribution(repo, adapter, body, expected):
    _git(repo, "commit", "--allow-empty", "--cleanup=verbatim", "-q", "-m",
         f"blank attribution\n\nPlan-Item: demo:A\n{body}\n")
    sha = _git(repo, "rev-parse", "HEAD")
    blank_key = body.splitlines()[-1].split(":")[0]
    parsed = adapter.writer_trailers(str(repo), sha)[0][blank_key]
    assert (parsed[-1] if isinstance(parsed, list) else parsed) == ""
    if expected is None:
        with pytest.raises(Refused) as exc:
            _families(repo, sha)
        assert exc.value.code == "writer_unknown"
    else:
        assert _families(repo, sha) == expected


@pytest.mark.parametrize("blank_first", [True, False], ids=["blank-first", "blank-last"])
@pytest.mark.parametrize("blank_key", ["Writer", "Hand-Edit"])
def test_fix11_real_parser_blank_order(repo, adapter, blank_first, blank_key):
    lines = [f"{blank_key}: ", "Writer: deepseek-v4"]
    if not blank_first:
        lines.reverse()
    _git(repo, "commit", "--allow-empty", "--cleanup=verbatim", "-q", "-m",
         "blank order\n\nPlan-Item: demo:A\n" + "\n".join(lines) + "\n")
    sha = _git(repo, "rev-parse", "HEAD")
    expected = {"Plan-Item": "demo:A"}
    if blank_key == "Writer":
        expected["Writer"] = ["", "deepseek-v4"] if blank_first else ["deepseek-v4", ""]
    else:
        expected.update({"Writer": "deepseek-v4", "Hand-Edit": ""})
    assert adapter.writer_trailers(str(repo), sha) == [expected]
    if blank_key == "Writer":
        with pytest.raises(Refused, match="blank Writer") as exc:
            _families(repo, sha)
        assert exc.value.code == "writer_unknown"
    else:
        assert _families(repo, sha) == ["deepseek"]


def test_fix11_merge_leaf_without_attribution_refuses(repo, adapter):
    _commit(repo, "base", {"Writer": "deepseek-v4"})
    _git(repo, "switch", "-q", "-c", "missing-writer")
    _commit(repo, "unattributed leaf", {"Plan-Item": "demo:A"})
    _git(repo, "switch", "-q", "main")
    sha = _merge(repo, "missing-writer", "demo:A")
    assert adapter.writer_trailers(str(repo), sha) == [{"Plan-Item": "demo:A"}]
    with pytest.raises(Refused) as exc:
        _families(repo, sha)
    assert exc.value.code == "writer_unknown"


def test_prose_hand_edit_and_writer_lines_are_not_trailers(repo):
    """A prose paragraph QUOTING ``Hand-Edit:``/``Writer:`` lines is not a
    trailer block, so it can neither be returned as a trailer nor change the
    writer families the keeper needs seats for.

    FIX2 r2: the prose ``Writer:`` line still must not be read as a trailer,
    but it now makes the writer set UNKNOWABLE rather than letting the keeper
    count only the surviving trailer's family.  A partial family list could
    close the item with a seat that never covered the dropped writer.
    """
    _git(repo, "commit", "--allow-empty", "-q", "-m",
         "explain the run\n\n"
         "This paragraph is prose about the change.\n"
         "Hand-Edit: touched docs by hand\n"
         "Writer: claude run-prose\n"
         "\n"
         "Plan-Item: demo:A\n"
         "Writer: mimo-v2.6-flash run-real\n")
    sha = _git(repo, "rev-parse", "HEAD")

    adapter = GitAdapter({"demo": str(repo)})
    got = adapter.trailers(str(repo), sha)
    # The prose Hand-Edit/Writer lines are dropped, the real ones kept.
    assert "Hand-Edit" not in got
    assert got.get("Writer") == "mimo-v2.6-flash run-real"
    assert adapter.writer_trailers(str(repo), sha) == []
    with pytest.raises(Refused) as exc:
        _families(repo, sha)
    assert exc.value.code == "writer_unknown"


def test_prose_plan_item_loses_to_the_real_trailer(repo, adapter):
    """A prose ``Plan-Item: plangraph:OTHER`` never attaches the commit to
    another item: only the real trailer block's key is returned."""
    _git(repo, "commit", "--allow-empty", "-q", "-m",
         "quote the old message\n\n"
         "Plan-Item: plangraph:OTHER\n"
         "the line above is quoted prose, not a trailer\n\n"
         "Plan-Item: plangraph:K\n")
    sha = _git(repo, "rev-parse", "HEAD")

    assert adapter.trailers(str(repo), sha) == {"Plan-Item": "plangraph:K"}


# --------------------------------------------------------------------------
# 21. (FIX2 r2) a Writer line OUTSIDE the trailing trailer block makes the
#     writer set unknowable: fail closed, never a partial family list
# --------------------------------------------------------------------------

def _ambiguous_commit(repo: Path) -> str:
    """``Plan-Item``/``Writer`` block, prose, then a final ``Writer`` block."""
    _git(repo, "commit", "--allow-empty", "-q", "-m",
         "split the trailers\n\n"
         "Plan-Item: demo:A\n"
         "Writer: mimo-v2 test-run-1\n"
         "\n"
         "This prose separates the two trailer-shaped paragraphs.\n"
         "\n"
         "Writer: deepseek-r1 test-run-2\n")
    return _git(repo, "rev-parse", "HEAD")


def test_writer_line_outside_the_trailing_block_refuses_writer_unknown(repo):
    """Only the trailing block is read, but a dropped ``Writer:`` cannot be
    silently ignored: ``writer_trailers`` refuses (``[]``) instead of
    returning the partial ``deepseek`` family alone."""
    sha = _ambiguous_commit(repo)
    adapter = GitAdapter({"demo": str(repo)})

    # Plan-Item/Writer reading itself is unchanged: the prose paragraph ends
    # the block, so only the final paragraph is parsed.
    assert adapter.trailers(str(repo), sha) == {"Writer": "deepseek-r1 test-run-2"}
    assert adapter.writer_trailers(str(repo), sha) == []
    with pytest.raises(Refused) as exc:
        _families(repo, sha)
    assert exc.value.code == "writer_unknown"


def test_writer_line_outside_the_trailing_block_refuses_the_closing_verdict(repo):
    """The production entry (``store.verdict``) refuses the closing verdict.

    A standard item would otherwise close on one seat once the partial family
    list named only ``deepseek``; the earlier ``mimo-v2`` writer would have
    no covering seat at all.
    """
    from evidence_fakes import make_ctx
    from keeper_util import World, seed

    sha = _ambiguous_commit(repo)
    world = World(repo.parent / "keeper")
    try:
        (key,) = seed(world, [("A", [])], evidence_kinds=("commit+ci",))
        world.store.ctx = make_ctx(git=GitAdapter({"demo": str(repo)}))
        epoch = world.store.claim(world.p("workerA"), key, lease_s=7200)["epoch"]
        out = world.store.add_evidence(world.p("workerA"), key, epoch,
                                       "commit+ci", {"commit": sha})
        assert out["verified"] is True
        world.store.submit(world.p("workerA"), key, epoch)

        with pytest.raises(Refused) as exc:
            world.store.verdict(world.p("reviewer"), key, True, "would close")
        assert exc.value.code == "writer_unknown"
        assert world.store._item(key)["state"] == "in_review"
    finally:
        world.close()


# --------------------------------------------------------------------------
# 22. (FIX3) a Writer-shaped line OUTSIDE the trailing block is not only the
#     plain "Writer: x" form: indenting it, quoting it, nesting the quote,
#     bullet-ing it, or spacing the key away from the colon all hide the same
#     dropped family from ``parse_trailers``, so the keeper must fail closed
#     on every one of those shapes too.
# --------------------------------------------------------------------------

#: One (shape id, line) pair per way a stray Writer line is written in a
#: commit body.  Each line is a paragraph of its own, OUTSIDE the trailing
#: trailer block that follows the blank line.
STRAY_WRITER_SHAPES = [
    ("indented", "    Writer: mimo-v2 test-run-1"),
    ("quoted", "> Writer: mimo-v2 test-run-1"),
    ("nested-quote", ">> Writer: mimo-v2 test-run-1"),
    ("bullet", "- Writer: mimo-v2 test-run-1"),
    ("star-bullet", "* Writer: mimo-v2 test-run-1"),
    ("space-before-colon", "Writer : mimo-v2 test-run-1"),
]


def _stray_writer_commit(repo: Path, stray: str) -> str:
    """A commit whose prose paragraph carries *stray*, then a clean block."""
    _git(repo, "commit", "--allow-empty", "-q", "-m",
         "quote a writer line\n\n"
         f"{stray}\n"
         "\n"
         "Plan-Item: demo:A\n"
         "Writer: deepseek-r1 test-run-2\n")
    return _git(repo, "rev-parse", "HEAD")


@pytest.mark.parametrize("shape,stray", STRAY_WRITER_SHAPES,
                         ids=[shape for shape, _ in STRAY_WRITER_SHAPES])
def test_stray_writer_shape_outside_the_trailing_block_refuses(repo, shape, stray):
    """Every stray shape names a writer ``parse_trailers`` drops.

    Only the trailing block is parsed, so the ``mimo-v2`` family would be
    silently lost and the keeper could close the item on a seat that never
    covered it; the writer set must be ``[]`` (``writer_unknown``) instead.
    """
    sha = _stray_writer_commit(repo, stray)
    adapter = GitAdapter({"demo": str(repo)})

    # The trailing block itself is read normally: the stray line is prose.
    assert adapter.trailers(str(repo), sha) == {
        "Plan-Item": "demo:A", "Writer": "deepseek-r1 test-run-2"}
    assert adapter.writer_trailers(str(repo), sha) == []
    with pytest.raises(Refused) as exc:
        _families(repo, sha)
    assert exc.value.code == "writer_unknown"


def test_trailing_block_alone_still_yields_its_writer_values(repo, adapter):
    """The widened stray-line detection does not fault a clean trailer block."""
    _git(repo, "commit", "--allow-empty", "-q", "-m",
         "clean trailers\n\n"
         "Plan-Item: demo:A\n"
         "Writer: mimo-v2 test-run-1\n"
         "Writer: deepseek-r1 test-run-2\n")
    sha = _git(repo, "rev-parse", "HEAD")

    assert adapter.writer_trailers(str(repo), sha) == [{
        "Plan-Item": "demo:A",
        "Writer": ["mimo-v2 test-run-1", "deepseek-r1 test-run-2"],
    }]


def test_prose_mentioning_writer_without_a_colon_is_not_flagged(repo, adapter):
    """Prose that only uses the WORD "writer" must not make writers unknowable."""
    _git(repo, "commit", "--allow-empty", "-q", "-m",
         "mention the writer\n\n"
         "The writer of this change ran the full suite first.\n"
         "Every writer line below names one model family.\n"
         "\n"
         "Plan-Item: demo:A\n"
         "Writer: mimo-v2 test-run-1\n")
    sha = _git(repo, "rev-parse", "HEAD")

    assert adapter.writer_trailers(str(repo), sha) == [
        {"Plan-Item": "demo:A", "Writer": "mimo-v2 test-run-1"}]


# --------------------------------------------------------------------------
# 23. (FIX4) fail-closed BY CONSTRUCTION: writer_trailers never yields a
#     PARTIAL writer family list.  Instead of enumerating spellings, ANY line
#     that looks like a Writer key is either read as one (a plain `Writer:`
#     trailer of the trailing block) or refuses the whole family set.
#     V2: a loose Writer-shaped line OUTSIDE the trailing block is stray.
#     V1: a Writer-shaped CONTINUATION inside the block is refused, never
#         folded into the previous value.
#     V3: a Writer value extended by ANY folded continuation is refused.
# --------------------------------------------------------------------------

#: One (shape id, template) pair per way a Writer-shaped line is written in a
#: commit body.  ``{value}`` is substituted, and each line is a paragraph of
#: its own BEFORE the clean trailing block, which names the ``mimo`` writer.
#: The pattern the shapes must hit is loose — ``writer`` + optional
#: non-alphanumeric decoration + ``:`` + a non-space value — so it covers
#: every fix3 spelling (indent, quote, nested quote, bullet, star bullet,
#: space before the colon) plus the ones an anchored regex missed: markdown
#: bold, numbered list, backticks, table pipe and a marker glued to the key.
WRITER_SHAPE_TEMPLATES = [
    ("indented", "    Writer: {value}"),
    ("quoted", "> Writer: {value}"),
    ("nested-quote", ">> Writer: {value}"),
    ("bullet", "- Writer: {value}"),
    ("star-bullet", "* Writer: {value}"),
    ("space-before-colon", "Writer : {value}"),
    ("markdown-bold", "**Writer:** {value}"),
    ("numbered", "1. Writer: {value}"),
    ("backticked", "`Writer: {value}`"),
    ("table-pipe", "| Writer: {value} |"),
    ("marker-joined", "-Writer: {value}"),
]


def _shape_then_clean_block_commit(repo: Path, shape_line: str) -> str:
    """A commit carrying *shape_line* first, then a clean trailing block.

    The clean block is the usual ``Plan-Item``/``Writer: mimo-v2 test-run-1``
    paragraph, so the ONLY thing that can make the writers unknowable is the
    shape line above it.
    """
    _git(repo, "commit", "--allow-empty", "-q", "-m",
         "quote a writer line\n\n"
         f"{shape_line}\n"
         "\n"
         "Plan-Item: demo:A\n"
         "Writer: mimo-v2 test-run-1\n")
    return _git(repo, "rev-parse", "HEAD")


@pytest.mark.parametrize("shape,template", WRITER_SHAPE_TEMPLATES,
                         ids=[shape for shape, _ in WRITER_SHAPE_TEMPLATES])
def test_writer_shape_before_the_clean_block_refuses_the_family_list(
        repo, shape, template):
    """V2: one commit per shape -> ``writer_trailers == []``, never one family.

    The dropped line names ``deepseek-r1``; the keeper must not close on a
    seat that never covered it, so ``coverage.writer_families`` refuses
    ``writer_unknown`` instead of counting the clean block's ``mimo`` alone.
    """
    sha = _shape_then_clean_block_commit(
        repo, template.format(value="deepseek-r1 test-run-2"))
    adapter = GitAdapter({"demo": str(repo)})

    assert adapter.writer_trailers(str(repo), sha) == []
    with pytest.raises(Refused) as exc:
        _families(repo, sha)
    assert exc.value.code == "writer_unknown"


@pytest.mark.parametrize("after", ["Writer", "Plan-Item"],
                         ids=["after-writer", "after-plan-item"])
def test_writer_shaped_continuation_inside_the_block_refuses(repo, after):
    """V1: an indented Writer-shaped line is refused, never folded in.

    Folding it into the value before it would keep ONE family of the two the
    body names (the review's exact partial-list failure).
    """
    if after == "Writer":
        body = ("Plan-Item: demo:A\n"
                "Writer: mimo-v2 test-run-1\n"
                "  Writer: deepseek-r1 test-run-2\n")
    else:
        body = ("Plan-Item: demo:A\n"
                "  Writer: deepseek-r1 test-run-2\n"
                "Writer: mimo-v2 test-run-1\n")
    _git(repo, "commit", "--allow-empty", "-q", "-m",
         f"fold a second writer\n\n{body}")
    sha = _git(repo, "rev-parse", "HEAD")
    adapter = GitAdapter({"demo": str(repo)})

    assert adapter.writer_trailers(str(repo), sha) == []
    with pytest.raises(Refused) as exc:
        _families(repo, sha)
    assert exc.value.code == "writer_unknown"


def test_writer_value_with_a_folded_continuation_refuses(repo):
    """V3: a continuation joined onto a ``Writer`` key is refused (``[]``).

    ``parse_trailers`` keeps git's continuation rule (the value below stays
    folded), but one value naming two models must never reach the family
    rule, which reads only its first token.
    """
    _git(repo, "commit", "--allow-empty", "-q", "-m",
         "fold a model into the writer value\n\n"
         "Plan-Item: demo:A\n"
         "Writer: mimo-v2 test-run-1\n"
         "  deepseek-r1 test-run-2\n")
    sha = _git(repo, "rev-parse", "HEAD")
    adapter = GitAdapter({"demo": str(repo)})

    # The generic trailer read is unchanged: git's own continuation rule.
    assert adapter.trailers(str(repo), sha) == {
        "Plan-Item": "demo:A",
        "Writer": "mimo-v2 test-run-1 deepseek-r1 test-run-2",
    }
    assert adapter.writer_trailers(str(repo), sha) == []
    with pytest.raises(Refused) as exc:
        _families(repo, sha)
    assert exc.value.code == "writer_unknown"


def test_plan_item_continuation_without_a_writer_shape_is_unchanged(repo, adapter):
    """Negative: a non-Writer-shaped continuation still joins its own key.

    Refusing here would fail closed on a body that names its writer plainly
    and completely, so the continuation rules must be Writer-specific.
    """
    _git(repo, "commit", "--allow-empty", "-q", "-m",
         "point at the tracker\n\n"
         "Plan-Item: demo:A\n"
         "  see the issue tracker for the details\n"
         "Writer: mimo-v2 test-run-1\n")
    sha = _git(repo, "rev-parse", "HEAD")

    want = {"Plan-Item": "demo:A see the issue tracker for the details",
            "Writer": "mimo-v2 test-run-1"}
    assert adapter.trailers(str(repo), sha) == want
    assert adapter.writer_trailers(str(repo), sha) == [want]
    assert _families(repo, sha) == ["xiaomi"]


def test_clean_two_writer_block_still_yields_both_families(repo, adapter):
    """Negative: a clean block is unaffected — BOTH writers reach coverage."""
    _git(repo, "commit", "--allow-empty", "-q", "-m",
         "two writers, no ambiguity\n\n"
         "Plan-Item: demo:A\n"
         "Writer: mimo-v2 test-run-1\n"
         "Writer: deepseek-r1 test-run-2\n")
    sha = _git(repo, "rev-parse", "HEAD")

    assert adapter.writer_trailers(str(repo), sha) == [{
        "Plan-Item": "demo:A",
        "Writer": ["mimo-v2 test-run-1", "deepseek-r1 test-run-2"],
    }]
    assert sorted(_families(repo, sha)) == ["deepseek", "xiaomi"]


@pytest.mark.parametrize("value", ["mimo-v2/deepseek-r1", "deepseek-r1/mimo-v2",
                                        "cx/gpt-6.1-sol/mimo-v2"])
def test_fix6_slash_model_list_refused(repo, value):
    sha = _commit(repo, "models", {"Writer": value})
    with pytest.raises(Refused, match="writer_mixed_family"):
        _families(repo, sha)


@pytest.mark.parametrize("run_id", ["run-claude"])
def test_second_token_naming_another_family_is_refused(repo, run_id):
    sha = _commit(repo, "run", {"Writer": f"mimo-v2 {run_id}"})
    with pytest.raises(Refused) as exc:
        _families(repo, sha)
    # FIX13 (G4): ``run-claude`` names anthropic, but ``run`` is no model the
    # table knows, so the value fails closed with the EXACT reason code.
    assert exc.value.code == "writer_unknown_family"


def test_fix13_unknown_model_head_with_codex_fails_closed(repo):
    """FIX13 (G4): ``codex`` resolves openai, but ``kimi-k2`` names no model
    the table knows -- an unresolvable model head fails closed as
    ``writer_unknown_family``, never accepted as openai.
    """
    sha = _commit(repo, "fix13 head", {"Writer": "kimi-k2 codex"})
    with pytest.raises(Refused) as exc:
        _families(repo, sha)
    assert exc.value.code == "writer_unknown_family"


def test_fix13_text_after_the_run_id_is_not_a_second_writer(repo):
    """FIX13 (G2): trailing text after the date-stamped run id is slug text."""
    sha = _commit(repo, "fix13 slug", {
        "Writer": "mimo-v2.6-flash 20261024-072324-x codex"})
    assert _families(repo, sha) == ["xiaomi"]


@pytest.mark.parametrize("run_id", [
    "20261023-184701-seat-free-opencode-write-13e736",  # spawner stamp
    "ses_abc12XYZ",                                     # session id (V2)
    "keeper-trailer-multi-fix5",                        # >=3 segments (V2/V7)
    "kimi-k2", "run1", "two-words", "8f4c7d",
])
def test_fix6_valid_second_token_accepted_as_run_id(repo, run_id):
    """V2: a recognised run id keeps exactly the model token's family."""
    sha = _commit(repo, "run-ok", {"Writer": f"mimo-v2 {run_id}"})
    assert _families(repo, sha) == ["xiaomi"]


# Fixed compatibility corpus: this sandbox has no origin/main history. These
# are the concrete shapes supplied by build-L1, not live-git expectations.
FIX6_WRITER_SHAPES = [
    ("deepseek-flash 20261022-142814-task-1dc4da [tool-less]", "deepseek"),
    ("qwen3.8-flash qodercli aabb-1234-5678", "alibaba"),
    ("mimo-v2.6-flash (xiaomi, omniroute) run [spawner]", "xiaomi"),
    ("omniroute/cx-gpt-6.1-sol title run", "openai"),
    ("qodercli Qwen3.8-Flash qt-w1-67abcd", "alibaba"),
    ("openai/gpt-4o", "openai"),
    ("deepseek/deepseek-r1", "deepseek"),
    ("mimo-v2/mimo-v3", "xiaomi"),
    ("L2", "anthropic"),
    ("omniroute/cx-gpt-6.1-sol keeper-trailer-multi-fix5", "openai"),
    ("omniroute/cx-gpt-6.1-sol exec-ownership-sq8-sol2-a3a264", "openai"),
    ("omniroute/cx-gpt-6.1-sol ui-browser-fix3-r2-382049", "openai"),
    ("cx-gpt-6.1-sol 20261023-184701-seat-free-opencode-write-13e736", "openai"),
    ("xiaomi-mimo/mimo-v2.6-flash 20261023-134745-trailer-multi-writer-470492", "xiaomi"),
    ("cx/gpt-6.1-sol trailer-multi/20261023-064042-trailer-multi-writer-0e3d4e", "openai"),
    ("mimo/mimo-v2.6-flash", "xiaomi"),
    ("cx/gpt-6.1-sol", "openai"),
    ("mimo-v2 20261023-123456-task", "xiaomi"),
]


@pytest.mark.parametrize("value,family", FIX6_WRITER_SHAPES)
def test_fix6_fixed_writer_shapes_resolve_once(repo, value, family):
    sha = _commit(repo, "compatibility", {"Writer": value})
    assert _families(repo, sha) == [family]


@pytest.mark.parametrize("value", ["n/a merge commit", "opencode run1", "unknown [tool-less]"])
def test_fix7_zero_families_refused(repo, value):
    sha = _commit(repo, "unknown", {"Writer": value})
    with pytest.raises(Refused, match="writer_unknown"):
        _families(repo, sha)


@pytest.mark.parametrize("value", [
    "deepseek-r1 (fix by L2:)",
    "qwen3.8-flash, L2.",
    "gpt-6.1-sol;claude-sonnet",
])
def test_fix8_glued_punctuation_exposes_second_family(repo, value):
    sha = _commit(repo, "punctuation", {"Writer": value})
    with pytest.raises(Refused) as exc:
        _families(repo, sha)
    assert exc.value.code == "writer_mixed_family"


def test_fix8_punctuated_run_id_preserves_model(repo):
    sha = _commit(repo, "run", {
        "Writer": "cx/gpt-6.1-sol 20261023-235309-x-800284."})
    assert _families(repo, sha) == ["openai"]


def test_corpus_value_naming_mimo_and_l2_refuses_mixed_writers(repo):
    value, _ = next(line for line in
                (Path(__file__).parent / "fixtures" / "main_writer_trailers.txt"
                 ).read_text(encoding="utf-8").splitlines()
                if not line.startswith("#") and "fixes by L2:" in line).split("\t")
    assert "fixes by L2:" in value
    sha = _commit(repo, "mixed corpus writer", {"Writer": value})
    with pytest.raises(Refused) as exc:
        _families(repo, sha)
    assert exc.value.code == "writer_mixed_family"


@pytest.mark.parametrize("value,expected", [
    line.split("\t") for line in
    (Path(__file__).parent / "fixtures" / "main_writer_trailers.txt").read_text(
        encoding="utf-8").splitlines() if not line.startswith("#")
])
def test_fix7_main_writer_corpus(repo, value, expected):
    """FIX13: every row pins its EXACT outcome -- one family, or the exact
    refusal code the grammar produces (the fix12 catch-all
    ``code in (...)`` is gone: a ``writer_unknown*`` label IS the code)."""
    sha = _commit(repo, "corpus", {"Writer": value})
    if expected.startswith("writer_"):
        with pytest.raises(Refused) as exc:
            _families(repo, sha)
        assert exc.value.code == expected
    else:
        assert _families(repo, sha) == [expected]


@pytest.mark.parametrize("colon", ["\uff1a", "\ua789", "\u2236", "\ufe55", "\u205d", " :"])
@pytest.mark.parametrize("position", ["prose", "block"])
def test_fix6_colon_variant_refuses(repo, adapter, colon, position):
    line = f"Writer{colon} deepseek-r1"
    body = (f"Prose about attribution.\n{line}\n\n" if position == "prose"
            else "")
    body += "Plan-Item: demo:A\nWriter: mimo-v2\n"
    if position == "block":
        body += line + "\n"
    _git(repo, "commit", "--allow-empty", "-q", "-m", f"colon\n\n{body}")
    sha = _git(repo, "rev-parse", "HEAD")
    assert adapter.writer_trailers(str(repo), sha) == []
    with pytest.raises(Refused, match="writer_unknown"):
        _families(repo, sha)


def test_fix7_every_unicode_named_colon_is_writer_shaped():
    import sys
    import unicodedata
    from plangraph.keeper.evidence_git import _WRITER_SHAPED_RE

    for code in range(sys.maxunicode + 1):
        char = chr(code)
        if "COLON" in unicodedata.name(char, ""):
            assert _WRITER_SHAPED_RE.search(f"Writer{char} deepseek-r1"), hex(code)


def test_fix9_pinned_colons_equal_unicode_name_scan():
    import sys
    import unicodedata
    from plangraph.keeper.evidence_git import _WRITER_COLONS

    assert set(_WRITER_COLONS) == {"\u2236"} | {
        chr(code) for code in range(sys.maxunicode + 1)
        if "COLON" in unicodedata.name(chr(code), "")}


@pytest.mark.parametrize("first", ["demo:OTHER", "demo:A"])
def test_fix6_plan_item_binding_requires_unique_value(repo, adapter, first):
    from evidence_fakes import make_ctx
    from plangraph.keeper.evidence import CommitCiVerifier

    _git(repo, "commit", "--allow-empty", "-q", "-m",
         f"binding\n\nPlan-Item: {first}\nplan-item: demo:A\nWriter: mimo-v2\n")
    sha = _git(repo, "rev-parse", "HEAD")
    # Use real parsing/ancestry; only PR provenance is supplied by the fixture.
    adapter.repo_path = lambda project: str(repo)
    adapter.merge_commit_of = lambda *args: (sha, "merge@example.invalid")
    ctx = make_ctx(git=adapter)
    ctx.ci.mode = lambda project: "none"
    result = CommitCiVerifier().verify({"key": "demo:A", "project": "demo"},
                                      {"commit": sha}, ctx)
    assert result.ok is (first == "demo:A")
