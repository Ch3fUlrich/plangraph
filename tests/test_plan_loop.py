"""Hermetic rule tests through the plan-loop command boundary."""
import json
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace

import pytest
from tools import plan_loop as cli
from tools import plan_loop_seats as selection
from plangraph.planner.legs import DEFAULT_LEGS


def plan(tmp_path, capsys, tier='high'):
    directory = tmp_path / 'material'
    assert cli.main(['init', '--dir', str(directory), '--slug', 'sample',
                     '--tier', tier]) == 0
    (directory / 'plan.md').write_text('A small plan.\n')
    assert cli.main(['freeze', '--dir', str(directory)]) == 0
    capsys.readouterr()
    return directory


def accept(argv, **kwargs):
    leg = next(l for l in DEFAULT_LEGS if l.id == argv[argv.index('--leg') + 1])
    return SimpleNamespace(returncode=0, stdout=json.dumps({
        'status': 'completed', 'run_id': leg.id, 'served_family': leg.family,
        'served_model': leg.model, 'witness_kind': 'response',
        'attempts': [{'stdout_tail': '```plan-review\n```\nVERDICT: ACCEPT\n'}]}))


def test_discovery_parallel_blind(tmp_path, capsys):
    directory = plan(tmp_path, capsys)
    barrier = Barrier(2)
    prompts = []

    def spawn(argv, **kwargs):
        prompts.append((Path(kwargs['cwd']) / argv[argv.index('--prompt-file') + 1]).read_text())
        barrier.wait(timeout=2)
        return accept(argv, **kwargs)

    assert cli.main(['round', '--dir', str(directory), '--kind', 'discovery'], spawn=spawn) == 0
    rows = cli.load(directory)[3]
    completed = [r for r in rows if r['status'] == 'completed']
    assert len(completed) == 2
    assert len({r['family'] for r in completed}) == 2
    assert all('R1.s1.' not in p for p in prompts)


def command(directory, capsys, verb, *args, spawn=accept):
    code = cli.main([verb, '--dir', str(directory), *args], spawn=spawn)
    return code, capsys.readouterr().out.strip()


def ready(directory):
    policy, manifest, rows, yields, _ = cli.load(directory)
    manifest.update(plan_chars_r1=100, report={'sha': manifest['text_sha'], 'ok': True,
                    'claims': ['C1']}, claims={'C1': {'state': 'verified'}})
    cli.save(directory, manifest, rows, yields)


def chat(directory, capsys, family, decision=True):
    reply = directory / 'reply.md'
    reply.write_text('```plan-review\n```\nVERDICT: ACCEPT\n')
    sha = cli.read(directory / 'manifest.json')['text_sha']
    return command(directory, capsys, 'add-review', '--family', family, '--sha', sha,
                   '--file', str(reply), '--source', 'operator-chat',
                   *(['--decision', 'D-123'] if decision else []))


def clean_plan(tmp_path, capsys):
    directory = plan(tmp_path, capsys, 'standard')
    ready(directory)
    command(directory, capsys, 'round', '--kind', 'scope-cut')
    for family in ('deepseek', 'qwen', 'glm', 'gpt'):
        chat(directory, capsys, family)
    assert command(directory, capsys, 'status')[0] == 0
    return directory


def test_b8_four_families_clean(tmp_path, capsys):
    directory = plan(tmp_path, capsys, 'standard')
    ready(directory)
    command(directory, capsys, 'round', '--kind', 'scope-cut',
            spawn=lambda *a, **k: SimpleNamespace(returncode=0, stdout=json.dumps({
                'status': 'completed', 'attempts': [{'stdout_tail': '```plan-review\n```\nVERDICT: ACCEPT\n'}]})))
    for family in ('gpt', 'openai', 'qwen', 'glm'):
        assert chat(directory, capsys, family)[1].startswith('OPEN ')
    assert 'accepted=3/4' in command(directory, capsys, 'status')[1]
    assert chat(directory, capsys, 'deepseek')[1].startswith('CLEAN ')


def test_unwitnessed_accept_never_counts(tmp_path, capsys):
    directory = plan(tmp_path, capsys, 'standard')
    for family in ('gpt', 'qwen', 'glm'):
        chat(directory, capsys, family)
    assert 'unwitnessed_accepts=deepseek' in chat(directory, capsys, 'deepseek', False)[1]
    path = directory / 'plan-loop.toml'
    path.write_text('allow_unwitnessed = true\n' + path.read_text())
    assert command(directory, capsys, 'status')[0] == 1


def test_b10_operator_chat_needs_decision(tmp_path, capsys):
    directory = plan(tmp_path, capsys, 'standard')
    assert 'accepted=0/4' in chat(directory, capsys, 'qwen', False)[1]
    line = chat(directory, capsys, 'qwen')[1]
    assert 'accepted=1/4' in line and 'operator_chat=alibaba' in line


def test_b8_private_default_file_reaches_four(tmp_path, capsys):
    directory = plan(tmp_path, capsys, 'standard')
    policy = selection.load_policy(directory / 'plan-loop.toml')
    for level in (1, 2, 3):
        state = selection.State(level=level)
        families = set()
        for _ in range(5):
            leg = selection.pick('confirm', state, policy)[0]
            families.add(cli.spawning._leg_family(leg.id))
            state.use(leg.family)
        assert {'deepseek', 'qwen', 'glm', 'gpt'} <= families
    assert selection.pick('stacked', selection.State(level=3), policy)[0].id == 'cx-gpt-6.1-sol'
    with pytest.raises(selection.SelectionRefused, match='ladder-exhausted'):
        selection.pick('stacked', selection.State(level=4), policy)


def test_b8_material_change_lapses_accepts(tmp_path, capsys):
    directory = clean_plan(tmp_path, capsys)
    (directory / 'plan.md').write_text('A changed plan.\n')
    assert 'accepted=0/4' in command(directory, capsys, 'freeze')[1]


def test_minor_freeze_carries_only_on_witnessed_diff_accept(tmp_path, capsys):
    directory = clean_plan(tmp_path, capsys)
    _, manifest, rows, yields, _ = cli.load(directory)
    cli.ledger.ingest(rows, cli.ledger.parse_reply('```plan-review\nF1-1 | minor | state | plan | defect | proof | repair\n```\nVERDICT: REQUEST_CHANGES\n'),
                      round=1, seat_id='s1', family='qwen', lens='', sha=manifest['text_sha'])
    cli.save(directory, manifest, rows, yields)
    (directory / 'plan.md').write_text('A small plan!\n')
    assert 'next=diff' in command(directory, capsys, 'freeze', '--minor', '--fixes', 'R1.s1.1')[1]

    def unwitnessed(argv, **kwargs):
        result = json.loads(accept(argv, **kwargs).stdout)
        result.pop('served_family')
        return SimpleNamespace(returncode=0, stdout=json.dumps(result))

    assert 'next=diff' in command(directory, capsys, 'round', '--kind', 'diff', spawn=unwitnessed)[1]
    assert command(directory, capsys, 'round', '--kind', 'diff')[1].startswith('CLEAN ')


def test_minor_diff_changes_lapse_accepts(tmp_path, capsys):
    directory = clean_plan(tmp_path, capsys)
    _, manifest, rows, yields, _ = cli.load(directory)
    cli.ledger.ingest(rows, cli.ledger.parse_reply('```plan-review\nF1-1 | minor | state | p | defect | proof | fix\n```\nVERDICT: REQUEST_CHANGES'),
                      round=1, seat_id='s', family='qwen', lens='', sha=manifest['text_sha'])
    cli.save(directory, manifest, rows, yields)
    (directory / 'plan.md').write_text('A small plan!\n')
    command(directory, capsys, 'freeze', '--minor', '--fixes', 'R1.s.1')
    def changes(argv, **kwargs):
        result = json.loads(accept(argv, **kwargs).stdout)
        result['attempts'][-1]['stdout_tail'] = '```plan-review\n```\nVERDICT: REQUEST_CHANGES'
        return SimpleNamespace(returncode=0, stdout=json.dumps(result))
    line = command(directory, capsys, 'round', '--kind', 'diff', spawn=changes)[1]
    assert line.startswith('OPEN ') and 'accepted=0/4' in line


@pytest.mark.parametrize('condition', range(1, 6))
def test_clean_needs_each_condition(tmp_path, capsys, condition):
    directory = clean_plan(tmp_path, capsys)
    _, manifest, rows, yields, _ = cli.load(directory)
    if condition == 1:
        rows.append({'severity': 'major', 'disposition': 'open'})
    elif condition == 2:
        yields[-1]['new_classes'] = ['new']
    elif condition == 3:
        for row in yields:
            row['kind'] = 'confirm'
    elif condition == 4:
        manifest['report']['ok'] = False
    else:
        manifest['plan_chars_r1'] = 1
    cli.save(directory, manifest, rows, yields)
    assert command(directory, capsys, 'status')[0] == 1


def cap(directory):
    _, manifest, rows, yields, _ = cli.load(directory)
    sha = manifest['text_sha']
    yields[:] = [dict(round=n, kind='stacked', status='completed', family='qwen',
                     sha=sha, witnessed=True, verdict='ACCEPT', counts={}, new_classes=[])
                 for n in range(1, 7)]
    cli.save(directory, manifest, rows, yields)


def test_b8_round_cap_blocks_never_passes(tmp_path, capsys):
    directory = plan(tmp_path, capsys, 'standard')
    cap(directory)
    assert command(directory, capsys, 'status') == (1,
        'BLOCKED plan:sample | review-loop-capped | options: raise cap by D-row, *replan')
    assert command(directory, capsys, 'round')[0] == 2
    assert command(directory, capsys, 'waive', '--decision', 'D-123')[0] == 2
    assert chat(directory, capsys, 'gpt')[1].startswith('BLOCKED ')


def test_extend_returns_open_keeps_counter(tmp_path, capsys):
    directory = plan(tmp_path, capsys, 'standard')
    cap(directory)
    command(directory, capsys, 'status')
    line = command(directory, capsys, 'extend', '--decision', 'D-123', '--rounds', '2')[1]
    assert line.startswith('OPEN ') and 'round=6' in line


@pytest.mark.parametrize('text', ['', 'No claims.'])
def test_r0_failed_report_never_clean(tmp_path, capsys, text):
    directory = plan(tmp_path, capsys, 'standard')
    report = directory / 'report.txt'
    report.write_text(text)
    assert command(directory, capsys, 'r0', '--report', str(report))[0] == 2
    assert command(directory, capsys, 'status')[0] == 1


def reply_test(tmp_path, capsys, text, *, earlier=None):
    directory = plan(tmp_path, capsys, 'standard')
    def spawn(argv, **kwargs):
        record = json.loads(accept(argv, **kwargs).stdout)
        record['attempts'] = ([{'stdout_tail': earlier}] if earlier else []) + [{'stdout_tail': text}]
        return SimpleNamespace(returncode=0, stdout=json.dumps(record))
    command(directory, capsys, 'round', '--kind', 'confirm', spawn=spawn)
    return cli.load(directory)


def test_accept_with_major_is_changes(tmp_path, capsys):
    result = reply_test(tmp_path, capsys, '```plan-review\nF1-1 | major | state | plan | defect | proof | repair\n```\nVERDICT: ACCEPT\n')
    assert result[2][0]['severity'] == 'major'
    assert result[3][-1]['verdict'] == 'REQUEST_CHANGES'


def test_missing_block_failed_seat(tmp_path, capsys):
    assert reply_test(tmp_path, capsys, 'VERDICT: ACCEPT\n')[3][-1]['status'] == 'failed'


def test_two_blocks_failed_seat(tmp_path, capsys):
    assert reply_test(tmp_path, capsys, '```plan-review\n```\n```plan-review\n```\nVERDICT: ACCEPT\n')[3][-1]['status'] == 'failed'


def test_verdict_inside_fence_failed_seat(tmp_path, capsys):
    assert reply_test(tmp_path, capsys, '```plan-review\nVERDICT: ACCEPT\n```\n')[3][-1]['status'] == 'failed'


def test_truncated_reply_failed_seat(tmp_path, capsys):
    assert reply_test(tmp_path, capsys, '```plan-review\n```\n[... output truncated ...]\nVERDICT: ACCEPT\n')[3][-1]['reason'] == 'output_truncated'


def test_earlier_attempt_verdict_ignored(tmp_path, capsys):
    assert reply_test(tmp_path, capsys, '', earlier='```plan-review\n```\nVERDICT: ACCEPT\n')[3][-1]['reason'] == 'empty_reply'


def test_b11_no_degraded_mode(tmp_path, capsys):
    directory = plan(tmp_path, capsys)
    path = directory / 'plan-loop.toml'
    text = path.read_text()
    lines = [line for line in text.splitlines() if '{id =' not in line or 'cx-gpt-6.1-sol' in line]
    text = '\n'.join(lines)
    start = text.index('fact_check_leg =')
    end = text.index('\n', start)
    text = text[:start] + 'fact_check_leg = ["cx-gpt-6.1-sol"]' + text[end:]
    path.write_text(text)
    calls = []
    line = command(directory, capsys, 'round', '--kind', 'discovery', spawn=lambda *a, **k: calls.append(a))[1]
    assert not calls
    assert line == 'BLOCKED plan:sample | review-loop-stalled | options: wait for a family, *waive by D-row'
    assert command(directory, capsys, 'waive', '--decision', 'D-123')[1] == 'WAIVED plan:sample D-123'


def test_c2_ladder_rises_never_falls(tmp_path, capsys):
    directory = plan(tmp_path, capsys, 'standard')
    state = selection.State()
    state.end_round(True)
    state.end_round(True)
    assert state.level == 2
    state.end_round(False)
    assert state.level == 2
    _, manifest, rows, yields, _ = cli.load(directory)
    manifest['selection'] = {'level': state.level}
    cli.save(directory, manifest, rows, yields)
    command(directory, capsys, 'round', '--kind', 'stacked')
    assert cli.load(directory)[1]['selection']['level'] == 2


def test_c2_ladder_end_blocks(tmp_path, capsys):
    directory = plan(tmp_path, capsys, 'standard')
    _, manifest, rows, yields, _ = cli.load(directory)
    manifest['selection'] = {'level': 4}
    cli.save(directory, manifest, rows, yields)
    assert command(directory, capsys, 'round', '--kind', 'stacked')[1] == (
        'BLOCKED plan:sample | ladder-exhausted | options: add a leg, *replan')


def training_round(tmp_path, capsys, material, words=None):
    directory = plan(tmp_path, capsys, 'standard')
    path = directory / 'plan-loop.toml'
    text = path.read_text().replace('material = "private"', f'material = "{material}"')
    text = '\n'.join(line for line in text.splitlines()
                     if '{id =' not in line or 'muse-spark' in line)
    start = text.index('fact_check_leg =')
    end = text.index('\n', start)
    text = text[:start] + 'fact_check_leg = ["opencode-go-muse-spark"]' + text[end:]
    if words is not None:
        wordlist = tmp_path / 'words.txt'
        wordlist.write_text(words)
        text = text.replace('site_words_file = ""', 'site_words_file = ' + json.dumps(str(wordlist)))
    path.write_text(text)
    calls = []
    def spawn(argv, **kwargs):
        calls.append(argv)
        return accept(argv, **kwargs)
    command(directory, capsys, 'round', '--kind', 'confirm', spawn=spawn)
    return directory, calls


def test_muse_private_skipped(tmp_path, capsys):
    assert not training_round(tmp_path, capsys, 'private')[1]


def test_muse_public_served(tmp_path, capsys):
    assert len(training_round(tmp_path, capsys, 'public')[1]) == 1


@pytest.mark.parametrize('words,served', [('absent-token-xyz', True), ('small', False)])
def test_muse_scrubbed_needs_zero_hits(tmp_path, capsys, words, served):
    assert bool(training_round(tmp_path, capsys, 'scrubbed', words)[1]) == served


def test_scrub_writes_no_word(tmp_path, capsys):
    directory, calls = training_round(tmp_path, capsys, 'scrubbed', 'small')
    assert not calls
    records = list(directory.glob('r*/*.scrub.json'))
    assert records and all('small' not in p.read_text() for p in records)


def test_muse_missing_wordlist_skipped(tmp_path, capsys):
    assert not training_round(tmp_path, capsys, 'scrubbed')[1]


def test_unverified_major_blocks_high_dispose(tmp_path, capsys):
    directory = plan(tmp_path, capsys)
    _, manifest, rows, yields, _ = cli.load(directory)
    parsed = cli.ledger.parse_reply('```plan-review\nF1-1 | major | state | plan | defect | UNVERIFIED | repair\n```\nVERDICT: REQUEST_CHANGES\n')
    cli.ledger.ingest(rows, parsed, round=1, seat_id='s1', family='qwen', lens='', sha=manifest['text_sha'])
    cli.save(directory, manifest, rows, yields)
    assert 'unverified=1' in command(directory, capsys, 'status')[1]
    assert command(directory, capsys, 'dispose', 'R1.s1.1', 'rejected', '--disposition-evidence', 'proof')[0] == 2


def test_b12_refused_route_falls_through(tmp_path, capsys):
    directory = tmp_path / 'material'
    assert cli.main(['init', '--dir', str(directory), '--slug', 'sample']) == 0
    capsys.readouterr()
    (directory / 'plan.md').write_text('A small plan.\n', encoding='utf-8')
    assert cli.main(['freeze', '--dir', str(directory)]) == 0
    capsys.readouterr()
    reasons = iter(['refused', 'limited', 'opencode_tools_not_read_only', 'over_budget'])
    calls = []
    legs = {leg.id: leg for leg in DEFAULT_LEGS}

    def spawn(argv, **kwargs):
        calls.append(argv)
        reason = next(reasons, None)
        if reason:
            record = {'status': 'refused' if reason == 'refused' else 'failed',
                      'reason': reason, 'events': [
                          {'event': 'render_refused', 'reason': reason}]}
        else:
            leg = legs[argv[argv.index('--leg') + 1]]
            record = {'status': 'completed', 'run_id': 'review-1',
                      'served_family': leg.family, 'served_model': leg.model,
                      'witness_kind': 'response', 'attempts': [
                          {'stdout_tail': '```plan-review\n```\nVERDICT: ACCEPT\n'}]}
        return SimpleNamespace(returncode=0, stdout=json.dumps(record))

    assert cli.main(['round', '--dir', str(directory), '--kind', 'confirm'],
                    spawn=spawn) == 0
    line = capsys.readouterr().out.strip()
    rows = cli.load(directory)[3]
    launched = [row for row in rows if row.get('attempt_used')]
    assert len(calls) == 5
    assert len(launched) == 1
    assert launched[0]['status'] == 'completed'
    assert line.startswith('OPEN plan:sample ')
    assert cli.main(['status', '--dir', str(directory)]) == 1
    assert capsys.readouterr().out.startswith('OPEN plan:sample ')
