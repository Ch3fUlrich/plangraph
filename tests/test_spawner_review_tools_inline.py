"""D-4681: harnesses without repo tools receive review diffs regardless of tool_mode."""
from types import SimpleNamespace
import subprocess

import pytest

from plangraph.spawner.inline_diff import InlineDiffError, inline_diff_prompt
from plangraph.spawner.seat_spawn import SeatSpawnMixin


@pytest.fixture
def diff_ref(tmp_path, monkeypatch):
    monkeypatch.delenv('PLANGRAPH_CONTEXT_STRICT', raising=False)
    monkeypatch.setenv('GIT_CONFIG_NOSYSTEM', '1')
    monkeypatch.setenv('GIT_CONFIG_GLOBAL', '/dev/null')

    def git(*args):
        return subprocess.run(['git', '-C', str(tmp_path), *args], check=True,
                              capture_output=True, text=True).stdout.strip()

    git('init', '-q')
    path = tmp_path / 'change.txt'
    path.write_text('before\n')
    git('add', 'change.txt')
    git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
        'commit', '-qm', 'base')
    base = git('rev-parse', 'HEAD')
    path.write_text('after\n')
    git('add', 'change.txt')
    git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
        'commit', '-qm', 'head')
    return {'repo': str(tmp_path), 'base': base, 'head': git('rev-parse', 'HEAD')}


def prompt(diff_ref, *, harness='opencode', mode='tools', role='review',
           missing=False, max_tokens=100000, document=False):
    seat = SeatSpawnMixin()
    seat.runner = SimpleNamespace(default_cwd=diff_ref['repo'])
    leg = SimpleNamespace(harness=harness, tool_mode=mode, max_request_tokens=max_tokens)
    card = {} if missing else {'diff_ref': diff_ref}
    result = seat._leg_prompt('review this', card, role, leg, document_review=document)
    assert leg.tool_mode == mode
    return result


@pytest.mark.parametrize('mode', ['tools', 'inline-only'])
def test_review_diff_inlined(diff_ref, mode):
    out = prompt(diff_ref, mode=mode)
    assert out == inline_diff_prompt('review this', diff_ref, max_chars=300000)
    assert '-before\n+after' in out


def test_tools_missing_diff_ref_unchanged(diff_ref):
    assert prompt(diff_ref, missing=True) == 'review this'


def test_inline_only_missing_diff_ref_refused(diff_ref):
    with pytest.raises(InlineDiffError, match='no diff_ref'):
        prompt(diff_ref, mode='inline-only', missing=True)


@pytest.mark.parametrize('harness', ['claude', 'agy', 'qodercli'])
@pytest.mark.parametrize('missing', [False, True])
def test_real_tool_harness_unchanged(diff_ref, harness, missing):
    assert prompt(diff_ref, harness=harness, missing=missing) == 'review this'


def test_opencode_implementation_unchanged(diff_ref):
    assert prompt(diff_ref, role='implement', missing=True) == 'review this'


def test_document_review_exception_preserved(diff_ref):
    assert prompt(diff_ref, missing=True, document=True) == 'review this'


@pytest.mark.parametrize('mode', ['tools', 'inline-only'])
def test_same_diff_cap(diff_ref, mode):
    from plangraph.spawner.context_pack import ContextError, PromptBoundError
    assert issubclass(PromptBoundError, ContextError)
    assert issubclass(PromptBoundError, InlineDiffError)
    with pytest.raises(PromptBoundError, match='prompt_too_large') as caught:
        prompt(diff_ref, mode=mode, max_tokens=1)
    assert isinstance(caught.value, InlineDiffError)
    assert isinstance(caught.value, ContextError)
    assert isinstance(caught.value, PromptBoundError)
    with pytest.raises(InlineDiffError, match='prompt_too_large: diff too large') as direct:
        inline_diff_prompt('review this', diff_ref, max_chars=3)
    assert direct.value.chars > direct.value.max_chars == 3


def test_tools_invalid_diff_ref_refused(diff_ref):
    with pytest.raises(InlineDiffError):
        prompt(dict(diff_ref, base='nonexistent-revision'))
