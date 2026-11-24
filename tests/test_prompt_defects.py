"""Hermetic guards for actionable prompt text and public repository pointers."""
from pathlib import Path
import subprocess
import re
import pytest

ROOT = Path(__file__).resolve().parents[1]

def test_fixer_completion_contract():
    from plangraph.routing_support.fixer import render_fixer_brief
    text = render_fixer_brief([{'text': 'repair'}])
    assert 'python3 tools/affected_tests.py --base origin/main --run' in text
    for condition in ('fixed', 'refuted by a named test', 'not done:', 'reason'):
        assert condition in text
    assert 'TEST-SPEED-AFFECTED' not in text

def test_fixer_terminal_report_gate(tmp_path):
    import asyncio
    from plangraph.routing_support.fixer import render_fixer_brief
    from plangraph.spawner.service import SpawnService
    brief = render_fixer_brief([{'text': 'repair'}])
    assert 'reported as not done: prose with the reason' in brief
    assert 'Never echo an unchecked checklist' in brief
    assert 'Do not use a Remaining gaps section for terminal not done: items' in brief
    service = SpawnService(runner=object(), state=tmp_path)
    async def probe():
        for output, reason in [('not done: repair; needs a decision', 'clean_report'),
                               ('1. [ ] repair', 'remaining_gaps'),
                               ('## Remaining gaps\nrepair', 'remaining_gaps')]:
            record = dict(run_id='probe', role='implement', exit_code=0)
            if reason == 'remaining_gaps':
                record['continuation_run_id'] = 'existing'  # Inspect gate without spawning.
            await service._continue_writer(record, output)
            assert record['continuation_reason'] == reason
    asyncio.run(probe())

def test_no_unresolvable_keeper_instruction():
    # Main documents this exact token as jargon to avoid, not an instruction.
    explanatory = {
        'skills/plangraph-planning/references/task-contract.md':
            '| Fleet jargon an agent cannot resolve (`TEST-SPEED-AFFECTED`, bare D-ids) | It cannot look them up |',
    }
    for folder in ('skills', 'docs', 'src'):
        for path in (ROOT / folder).rglob('*'):
            if path.is_file():
                text = path.read_bytes().decode('utf-8', errors='replace')
                for line in text.splitlines():
                    if 'TEST-SPEED-AFFECTED' in line:
                        assert line == explanatory.get(path.relative_to(ROOT).as_posix())


@pytest.mark.parametrize('name', ['docs/brief.md',
    'skills/plangraph-planning/references/task-contract.md'])
def test_keeper_scan_rejects_instructions(tmp_path, monkeypatch, name):
    monkeypatch.setitem(globals(), 'ROOT', tmp_path)
    path = tmp_path / name
    path.parent.mkdir(parents=True)
    path.write_text('Run affected tests only; follow TEST-SPEED-AFFECTED.\n')
    with pytest.raises(AssertionError):
        test_no_unresolvable_keeper_instruction()


@pytest.mark.parametrize('mode', ['files', 'diff'])
def test_tail_standing_question(mode):
    from plangraph.review.prompt import assemble_all, assemble_diff
    text = (ROOT / 'src/plangraph/review/tail.md').read_text()
    tails = subprocess.check_output(['git', 'ls-files', '*tail.md'], cwd=ROOT, text=True).splitlines()
    assert tails == ['src/plangraph/review/tail.md']  # load_tail reads this sole source.
    from plangraph.review.prompt import load_tail
    assert load_tail() == text
    assert 'Does the change game tests' in text
    prompts = (assemble_all(['check'], {'src/example.py': 'value = 1\n'})
               if mode == 'files' else assemble_diff(['check'],
                   'diff --git a/src/example.py b/src/example.py\n'
                   '--- a/src/example.py\n+++ b/src/example.py\n@@ -1 +1 @@\n-value = 0\n+value = 1\n'))
    assert prompts
    for prompt in prompts:
        assert prompt.rstrip().endswith(text.rstrip())
        assert prompt.index("=== ITEM CONTEXT") < prompt.index("=== FILE-PART MAP")
        assert prompt.index("value = 1") < prompt.index(text.rstrip())
        assert prompt.count('Does the change game tests') == 1

def test_claude_skill_pointers():
    assert (ROOT / 'CLAUDE.md').read_text().strip() == '@AGENTS.md'
    text = (ROOT / 'AGENTS.md').read_text()
    pointers = re.findall(r'`(skills/[^`]+/SKILL\.md)`', text)
    assert pointers
    assert all((ROOT / pointer).is_file() for pointer in pointers)
    named = {pointer.split('/')[1] for pointer in pointers}
    assert named == {path.name for path in (ROOT / 'skills').iterdir() if path.is_dir()}
    assert len(text.splitlines()) <= 200
    assert not re.search(r'(?:[\w.-]+/)+[\w.-]*DECISIONS[\w.-]*', text, re.I)

def test_no_missing_protocol_pointer():
    for folder in ('skills', 'docs'):
        for path in (ROOT / folder).rglob('*'):
            if path.is_file():
                text = path.read_bytes().decode('utf-8', errors='replace')
                assert 'docs/agent-protocol.md' not in text

def test_protocol_scan_includes_untracked_files(tmp_path, monkeypatch):
    monkeypatch.setitem(globals(), 'ROOT', tmp_path)
    (tmp_path / 'docs').mkdir()
    (tmp_path / 'docs/untracked.md').write_text('docs/agent-protocol.md')
    with pytest.raises(AssertionError):
        test_no_missing_protocol_pointer()

def test_skill_card_pointers():
    skill = ROOT / 'skills/unattended-orchestration/SKILL.md'
    pointers = re.findall(r'cards/[\w-]+\.md', skill.read_text())
    assert pointers
    assert all((skill.parent / pointer).is_file() for pointer in pointers)


def test_toolchain_provision_not_version_claim():
    text = (ROOT / 'skills/plangraph-planning/SKILL.md').read_text()
    assert 'The spawner provides the toolchain in the brief' in text
    assert '_leg_prompt' not in text


def test_l3_question_channel():
    text = (ROOT / 'skills/unattended-orchestration/SKILL.md').read_text()
    row = next(line for line in text.splitlines() if line.startswith('| **L3**'))
    assert 'never' not in row
    assert 'PG_QUESTION:' in row
    assert 'rust/worker/src/question.rs:QUESTION_PREFIX' in row
    code = (ROOT / 'rust/worker/src/question.rs').read_text()
    assert 'pub const QUESTION_PREFIX: &str = "PG_QUESTION:";' in code


def test_report_lead_in_and_retry_pointers():
    text = (ROOT / 'skills/implement/SKILL.md').read_text()
    assert 'End with these, and no claim without evidence:\n\n- `commits:`' in text
    text = (ROOT / 'docs/operations/spawner-prompt-rules.md').read_text()
    for pointer in ('ReviewMethod.max_rounds', 'ATTEMPT_BUDGET', 'plan.legs', 'no_fallthrough'):
        assert pointer in text
    assert 'no numeric\nfallthrough constant' in text
    assert all(len(line) <= 100 for line in text.splitlines() if
               'render_fixer_brief` numbers' in line or 'checklist or use' in line)


def test_spawner_size_and_prompt_rules_pointer():
    import importlib.util
    spec = importlib.util.spec_from_file_location('check_file_size', ROOT / 'tools/check_file_size.py')
    sizes = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sizes)
    # The cap and its headroom live in tools/check_file_size.py only.
    assert sizes.DOC_CAPS['docs/SPAWNER.md'] == 728 and sizes.DOC_HEADROOM >= 60
    text = (ROOT / 'docs/SPAWNER.md').read_text()
    assert len(text.splitlines()) <= sizes.DOC_CAPS['docs/SPAWNER.md'] - sizes.DOC_HEADROOM
    assert text.count('(operations/spawner-prompt-rules.md)') == 1
    rules = (ROOT / 'docs/operations/spawner-prompt-rules.md').read_text()
    assert len(rules.splitlines()) <= 400
    for contract in ('render_fixer_brief', 'refuted by a named test', '`not done:`',
                     'reason', 'Never echo an unchecked checklist', '`Remaining gaps`',
                     'python3 tools/affected_tests.py --base origin/main --run',
                     'COMMIT before finishing'):
        assert contract in rules


def test_review_seat_brief_requires_test_gaming_line():
    from plangraph.review.prompt import render_brief

    brief = render_brief("review", {"item_key": "demo:SEAT"})
    assert "TEST-GAMING:" in brief.split("=== ITEM CONTEXT", 1)[0]


@pytest.mark.parametrize("kind", ["outside", "relative", "symlink", "directory",
                                  "over-1-mib", "invalid-utf8"])
def test_review_brief_rejects_unreadable_writer_artifacts(tmp_path, monkeypatch, kind):
    from plangraph.review.prompt import render_brief

    review_dir = tmp_path / "review"
    review_dir.mkdir()
    status = review_dir / "status.txt"
    sentinel = b"ARTIFACT CONTENT MUST STAY OUT"
    if kind == "outside":
        status = tmp_path / status.name
        status.write_bytes(sentinel)
    elif kind == "relative":
        status.write_bytes(sentinel)
        monkeypatch.chdir(tmp_path)
        status = status.relative_to(tmp_path)
        review_dir = status.parent
    elif kind == "symlink":
        target = review_dir / "target.txt"
        target.write_bytes(sentinel)
        status.symlink_to(target)
    elif kind == "directory":
        status.mkdir()
        (status / "child.txt").write_bytes(sentinel)
    elif kind == "over-1-mib":
        status.write_bytes(sentinel.ljust(1024 * 1024 + 1, b"x"))
    elif kind == "invalid-utf8":
        status.write_bytes(sentinel.ljust(32 * 1024, b"x") + b"\xff")

    brief = render_brief("review", {"writer_run": {
        "review_dir": str(review_dir), "writer_status_path": str(status),
    }})
    assert "writer status: unreadable" in brief
    assert sentinel.decode("ascii") not in brief


@pytest.mark.parametrize("artifact_size", [32 * 1024, 32 * 1024 + 64])
def test_review_brief_limits_included_writer_artifact(tmp_path, artifact_size):
    from plangraph.review.prompt import render_brief

    status = tmp_path / "status.txt"
    limit = 32 * 1024
    suffix = "é\nEXCLUDED TAIL".encode("utf-8")
    if artifact_size == limit:
        content = b"x" * (limit - len(suffix)) + suffix
    else:
        # The two-byte character straddles the excerpt limit.
        content = (b"x" * (limit - 1) + suffix).ljust(artifact_size, b"x")
    status.write_bytes(content)

    brief = render_brief("review", {"writer_run": {
        "review_dir": str(tmp_path), "writer_status_path": str(status),
    }})
    evidence = brief.split("writer status: included\n", 1)[1].split("\nwriter slices:", 1)[0]
    if artifact_size == limit:
        assert evidence.endswith("é\n> EXCLUDED TAIL")
    else:
        assert evidence == "> " + "x" * (limit - 1) + "\n> [artifact truncated at 32 KiB]"


FULL_CONTEXT = {"item_key": "demo:CTX", "public_symbols": "run_seat", "callers": "seat.py:9 run_seat",
                "test_output_paths": "logs/fast.log"}


@pytest.mark.parametrize("field", ["public_symbols", "callers", "test_output_paths"])
@pytest.mark.parametrize("bad", [None, "", "  ", "unavailable (request before acceptance)", "$callers"])
def test_lane_head_review_brief_refuses_empty_item_context(field, bad):
    from plangraph.review.prompt import ItemContextError, render_brief

    context = dict(FULL_CONTEXT, **{field: bad})
    if bad is None:
        del context[field]
    with pytest.raises(ItemContextError, match=field) as raised:
        render_brief("review", context, require_item_context=True)
    assert raised.value.fields == [field]
    # The default keeps the optional-context entry points (seat, judge) working.
    assert "unavailable (request before acceptance)" in render_brief("review", {"item_key": "x"})


def test_lane_head_review_brief_accepts_verified_empty_and_renders_block():
    from plangraph.review.prompt import item_context_defects, render_brief, verified_empty

    context = dict(FULL_CONTEXT, public_symbols=verified_empty("docs-only diff"), callers=verified_empty("no public symbol"))
    brief = render_brief("review", context, require_item_context=True)
    assert "Changed public symbols: none (verified empty: docs-only diff)" in brief
    assert "Affected test output (paths): logs/fast.log" in brief
    assert item_context_defects(brief) == []
    assert item_context_defects(render_brief("review", {"item_key": "x"})) == [
        "public_symbols", "callers", "test_output_paths"]
    assert item_context_defects("no block at all") == ["public_symbols", "callers", "test_output_paths"]
