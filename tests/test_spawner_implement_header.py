"""Rule 3.57: spawned writers and reviewers start with their own harness header and role skill."""
import asyncio
from pathlib import Path

import pytest

from plangraph.spawner.prompt_input import compose, header, read_skill
from plangraph.spawner.runner import SpawnerRunner

ROOT = Path(__file__).resolve().parents[1]
BATCH_TAIL = '\n# Items\nitem {n}\n\n# Run values\nNAME: x\n'


def _prompt(role, prompt='do the brief', client=None, model='mimo/mimo-v2.6-flash'):
    job = asyncio.run(SpawnerRunner().spawn(prompt, model_or_combo=model, role=role,
                                            dry_run=True, client=client))
    return job.task_prompt


def _skill(name):
    return read_skill(name, str(ROOT))


@pytest.mark.parametrize('role', ['implement', 'review'])
@pytest.mark.parametrize('harness,leg_id,words,forbidden', [
    ('agy', 'agy-x', 'agy', 'opencode'),
    ('opencode', 'cx-gpt-6.1-sol', 'Codex', 'opencode'),
    ('opencode', 'opencode-x', 'opencode', 'Codex'),
])
def test_header_names_own_harness(role, harness, leg_id, words, forbidden):
    text = header(harness, role, leg_id)
    assert text and words in text
    assert forbidden not in text
    assert 'AutoOS' not in text and 'build agent' not in text
    assert ('read-only' in text) == (role == 'review')
    assert 'never push' in text or role == 'review'
    full = compose('brief body', role, harness, str(ROOT), leg_id)
    assert full.startswith(text) and _skill('review' if role == 'review' else 'implement') in full
    # the whole built prompt, skill text included, carries none of the forbidden words
    assert 'AutoOS' not in full and 'opencode build agent' not in full
    assert forbidden not in full.split('\n---\n')[0]


def test_batch_prompt_has_no_forbidden_words():
    for harness, leg in (('agy', 'agy-x'), ('opencode', 'cx-gpt-6.1-sol'), ('opencode', 'opencode-x')):
        full = compose('b' + BATCH_TAIL.format(n=1), 'implement', harness, str(ROOT), leg)
        assert 'AutoOS' not in full and 'opencode build agent' not in full


def test_spawned_prompts_have_header_and_skill():
    impl = _prompt('implement', client='agy', model='agy-gemini-4-argon-low')
    assert impl.startswith(header('agy', 'implement'))
    assert _skill('implement') in impl and impl.endswith('do the brief')
    review = _prompt('review', client='agy', model='agy-gemini-4-argon-low')
    assert review.startswith(header('agy', 'review'))
    assert _skill('review') in review
    assert 'AutoOS' not in impl.split('name: implement')[0]


def test_batch_briefs_share_prefix():
    a = _prompt('implement', 'brief A' + BATCH_TAIL.format(n=1))
    b = _prompt('implement', 'brief B' + BATCH_TAIL.format(n=2))
    prefix = header('opencode', 'implement') + '\n\n' + _skill('batch-writer')
    assert a.startswith(prefix) and b.startswith(prefix)


def test_skill_in_brief_is_not_doubled():
    skill = _skill('batch-writer')
    brief = skill + '\n' + BATCH_TAIL.format(n=1)
    out = _prompt('implement', brief)
    assert out.count(skill) == 1
    assert out == header('opencode', 'implement') + '\n\n' + brief


def test_implement_prompt_starts_with_header():
    assert _prompt('implement').startswith(header('opencode', 'implement'))


def test_review_prompt_has_no_header():
    # the implement header (commit allowed) never reaches a review prompt
    assert 'commit is allowed' not in _prompt('review')


def test_header_not_doubled_on_continuation():
    text = header('opencode', 'implement')
    assert _prompt('implement', text + '\n\nnext round').count(text) == 1


def test_continuation_is_idempotent():
    once = _prompt('implement', 'next round')
    assert _prompt('implement', once) == once


def test_prompt_built_without_skill_fails_the_presence_check(monkeypatch):
    monkeypatch.setattr('plangraph.spawner.prompt_input.read_skill', lambda *a: None)
    out = _prompt('review')
    with pytest.raises(AssertionError):
        assert _skill('review') in out


def test_skill_falls_back_to_working_tree_without_origin_main(tmp_path, monkeypatch):
    monkeypatch.setattr('plangraph.spawner.prompt_input._REPO_ROOT', tmp_path / 'none')
    (tmp_path / 'skills' / 'review').mkdir(parents=True)
    (tmp_path / 'skills' / 'review' / 'SKILL.md').write_text('---\nname: review\n---\nlocal\n')
    assert read_skill('review', str(tmp_path)) == '---\nname: review\n---\nlocal\n'
    assert compose('x', 'review', 'agy', str(tmp_path), 'agy-x').endswith('local\n\nx')


def test_gemini_md_links_agents_md():
    # A repository scrub forbids symlinks, so GEMINI.md imports AGENTS.md the way CLAUDE.md does.
    assert (ROOT / 'GEMINI.md').read_text() == '@AGENTS.md\n' == (ROOT / 'CLAUDE.md').read_text()
    assert (ROOT / 'AGENTS.md').is_file()


def test_native_writer_review_composes_the_entire_brief_for_inline_leg():
    from plangraph.review.prompt import render_brief

    head = "a" * 40
    native_brief = render_brief("writer", {"item_key": "demo:INLINE"},
                                rules="Open src/example.rs and run cargo test.")
    versioned = render_brief("review", {"item_key": "demo:INLINE", "head_sha": head},
                              rules=_skill("review"))
    rules, item = versioned.split("\n=== ITEM CONTEXT\n", 1)
    brief = (f"{rules}\n\nWriter brief (item keys and Done-when lines):\n{native_brief}\n"
             "Open callers, tests and docs. Run read-only commands.\n"
             "<!-- PLANGRAPH NATIVE REVIEW TOOLS END -->\n"
             f"=== ITEM CONTEXT\n{item}\nName a finding with file:line evidence.\n"
             f"HEAD: {head}\nVERDICT: ACCEPT or REQUEST_CHANGES\n"
             "\n=== INLINE DIFF ===\n+fn evidence() {}\n")
    task_prompt = _prompt("review", brief, model="mimo/mimo-v2.6-pro")
    assert "no tools" in task_prompt.lower()
    for instruction in ("Read the named files", "Open callers", "Run read-only commands",
                        "Open src/example.rs", "cargo test", "Writer brief", "Skills"):
        assert instruction not in task_prompt
    for evidence in ("demo:INLINE", head, "VERDICT: ACCEPT", "+fn evidence() {}"):
        assert evidence in task_prompt

    tools = _prompt("review", brief, model="agy-gemini-4-argon-low", client="agy")
    assert "Open src/example.rs and run cargo test." in tools
    assert "Open callers, tests and docs." in tools
