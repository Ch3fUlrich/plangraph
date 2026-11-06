import json
from types import SimpleNamespace
from pathlib import Path
import subprocess
import sys

from tools import plan_loop as cli


def invoke(capsys, *args):
    code = cli.main(list(args))
    return code, capsys.readouterr().out.strip()


def setup_plan(tmp_path, capsys):
    directory = tmp_path / 'material'
    assert invoke(capsys, 'init', '--dir', str(directory), '--slug', 'sample')[0] == 0
    (directory / 'plan.md').write_text('A small plan.\n')
    assert invoke(capsys, 'freeze', '--dir', str(directory))[0] == 0
    return directory


def test_directory_refusal(capsys):
    code, line = invoke(capsys, 'init', '--dir', str(Path(__file__).parents[1]), '--slug', 'sample')
    assert code == 2
    assert 'outside the checkout' in line


def test_killed_round_rerunnable(tmp_path, capsys):
    directory = setup_plan(tmp_path, capsys)
    script = '''
import json, os, signal, sys
from types import SimpleNamespace
from tools import plan_loop as cli
calls = 0
def spawn(argv, **kwargs):
    global calls
    calls += 1
    if calls == 2:
        os.kill(os.getpid(), signal.SIGKILL)
    return SimpleNamespace(returncode=0, stdout=json.dumps(dict(status='completed',
        attempts=[dict(stdout_tail='```plan-review\\n```\\nVERDICT: ACCEPT\\n')])))
cli.main(['round', '--dir', sys.argv[1], '--kind', 'confirm'], spawn=spawn)
cli.main(['round', '--dir', sys.argv[1], '--kind', 'stacked'], spawn=spawn)
'''
    result = subprocess.run([sys.executable, '-c', script, str(directory)], capture_output=True)
    assert result.returncode == -9
    assert sum(r['status'] == 'completed' for r in cli.load(directory)[3]) == 1
    def spawn(*args, **kwargs):
        return SimpleNamespace(returncode=0, stdout=json.dumps(dict(status='completed',
            attempts=[dict(stdout_tail='```plan-review\n```\nVERDICT: ACCEPT\n')])))
    assert cli.main(['round', '--dir', str(directory), '--kind', 'stacked'], spawn=spawn) == 0
    assert sum(r['status'] == 'completed' for r in cli.load(directory)[3]) == 2


def test_init_freeze_and_status(tmp_path, capsys):
    directory = setup_plan(tmp_path, capsys)
    code, line = invoke(capsys, 'status', '--dir', str(directory))
    assert code == 1 and line.startswith('OPEN plan:sample ')
    assert len(line.splitlines()) == 1


def test_cap_is_sticky_until_extend(tmp_path, capsys):
    directory = setup_plan(tmp_path, capsys)
    path = directory / 'manifest.json'
    manifest = json.loads(path.read_text())
    manifest['capped'] = True
    path.write_text(json.dumps(manifest))
    assert invoke(capsys, 'round', '--dir', str(directory))[0] == 2
    assert invoke(capsys, 'waive', '--dir', str(directory), '--decision', 'D-123')[0] == 2
    assert invoke(capsys, 'extend', '--dir', str(directory), '--decision', 'D-123', '--rounds', '2')[0] == 0
    assert not json.loads(path.read_text())['capped']


def test_operator_review_and_drift(tmp_path, capsys):
    directory = setup_plan(tmp_path, capsys)
    sha = json.loads((directory / 'manifest.json').read_text())['text_sha']
    reply = tmp_path / 'reply.md'
    reply.write_text('```plan-review\n```\nVERDICT: ACCEPT\n')
    args = ('add-review', '--dir', str(directory), '--family', 'qwen', '--sha', sha,
            '--file', str(reply), '--source', 'operator-chat')
    assert invoke(capsys, *args)[0] == 0
    row = cli.load(directory)[3][0]
    assert not row['witnessed'] and row['counts']['major'] == 0
    (directory / 'plan.md').write_text('Changed')
    assert invoke(capsys, *args)[0] == 2


def test_round_records_yield_and_discovery_baseline(tmp_path, capsys):
    directory = setup_plan(tmp_path, capsys)
    calls = []

    def spawn(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout=json.dumps({
            'status': 'completed', 'run_id': 'run-1',
            'attempts': [{'stdout_tail': '```plan-review\n```\nVERDICT: ACCEPT\n'}]}))

    assert cli.main(['round', '--dir', str(directory), '--kind', 'discovery'], spawn=spawn) == 0
    capsys.readouterr()
    manifest = json.loads((directory / 'manifest.json').read_text())
    assert manifest['plan_chars_r1'] == len('A small plan.\n')
    records = cli.load(directory)[3]
    completed = [r for r in records if r['status'] == 'completed']
    assert len(completed) == 1
    assert completed[0]['kind'] == 'discovery'
    assert completed[0]['counts'] == {'blocker': 0, 'major': 0, 'minor': 0}
    assert '--prompt-file' in calls[0][0]
    assert json.loads((directory / 'ledger.json').read_text()) == []
    exported = [json.loads(line) for line in (directory / 'yield.jsonl').read_text().splitlines()]
    assert exported == records
    assert set(('round kind lens family leg run_id sha from_sha witnessed witness_kind '
                'verdict status reason counts new_classes seconds served_model served_family').split()) <= completed[0].keys()


def test_kill_after_journal_repairs_exports(tmp_path, capsys):
    directory = setup_plan(tmp_path, capsys)
    script = '''
import os, signal, sys
from tools import plan_loop as cli
directory = cli.Path(sys.argv[1])
_, manifest, rows, yields, _ = cli.load(directory)
parsed = cli.ledger.parse_reply('```plan-review\\nF1-1 | major | state | plan | defect | proof | repair\\n```\\nVERDICT: REQUEST_CHANGES')
cli.ledger.ingest(rows, parsed, round=1, seat_id='s1', family='qwen', lens='state', sha=manifest['text_sha'])
yields.append(dict(round=1, kind='confirm', lens='state', family='qwen', leg='example',
    run_id='run-1', sha=manifest['text_sha'], from_sha=None, witnessed=True,
    witness_kind='response', verdict='REQUEST_CHANGES', status='completed', reason='',
    counts=dict(blocker=0, major=1, minor=0), new_classes=['state'], seconds=0))
original = cli.ledger.atomic_json
def write(path, value):
    original(path, value)
    if path.name == 'manifest.json':
        os.kill(os.getpid(), signal.SIGKILL)
cli.ledger.atomic_json = write
cli.save(directory, manifest, rows, yields)
'''
    result = subprocess.run([sys.executable, '-c', script, str(directory)], capture_output=True)
    assert result.returncode == -9
    journal = json.loads((directory / 'manifest.json').read_text())['driver_projection']
    assert json.loads((directory / 'ledger.json').read_text()) != journal['ledger']
    assert invoke(capsys, 'status', '--dir', str(directory))[0] == 1
    assert json.loads((directory / 'ledger.json').read_text()) == journal['ledger']
    assert [json.loads(s) for s in (directory / 'yield.jsonl').read_text().splitlines()] == journal['yields']
    assert 'open=0/1' in invoke(capsys, 'status', '--dir', str(directory))[1]
    code, line = invoke(capsys, 'check-batch', '--dir', str(directory), 'missing.yaml')
    assert code == 1 and 'open=0/1' in line
    fields = ('id seat_id round family lens severity class claim location evidence fix scope_plus '
              'disposition disposition_evidence trigger sha_seen sha_fixed closed_by unverified sightings dup_of')
    assert set(fields.split()) <= journal['ledger'][0].keys()


def test_accepted_unknown_sha_refused(tmp_path, capsys):
    directory = setup_plan(tmp_path, capsys)
    manifest = json.loads((directory / 'manifest.json').read_text())
    parsed = cli.ledger.parse_reply('```plan-review\nF1-1 | major | state | plan | defect | proof | repair\n```\nVERDICT: REQUEST_CHANGES\n')
    rows = []
    cli.ledger.ingest(rows, parsed, round=1, seat_id='s1', family='qwen', lens='', sha=manifest['text_sha'])
    cli.save(directory, manifest, rows, [])
    assert invoke(capsys, 'dispose', '--dir', str(directory), 'R1.s1.1', 'accepted', '--sha-fixed', 'unknown')[0] == 2


def test_blocked_ladder_recovers_after_policy_edit(tmp_path, capsys):
    directory = setup_plan(tmp_path, capsys)
    _, manifest, rows, yields, _ = cli.load(directory)
    manifest['selection'] = {'level': 4}
    cli.save(directory, manifest, rows, yields)
    assert invoke(capsys, 'round', '--dir', str(directory), '--kind', 'stacked')[1].startswith('BLOCKED ')
    path = directory / 'plan-loop.toml'
    path.write_text(path.read_text().replace('rung = 3', 'rung = 4'))
    def spawn(*args, **kwargs):
        return SimpleNamespace(returncode=0, stdout=json.dumps(dict(status='completed',
            attempts=[dict(stdout_tail='```plan-review\n```\nVERDICT: ACCEPT\n')])))
    assert cli.main(['round', '--dir', str(directory), '--kind', 'stacked'], spawn=spawn) == 0
    assert not cli.load(directory)[1].get('ladder_exhausted')


def test_discovery_shared_failure_limit(tmp_path, capsys):
    directory = setup_plan(tmp_path, capsys)
    path = directory / 'manifest.json'
    manifest = json.loads(path.read_text())
    manifest['tier'] = 'high'
    cli.ledger.atomic_json(path, manifest)
    calls = []
    def spawn(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout=json.dumps({
            'status': 'failed', 'reason': 'no_verdict'}))
    assert cli.main(['round', '--dir', str(directory), '--kind', 'discovery'], spawn=spawn) == 0
    assert len(calls) == 3
    assert capsys.readouterr().out.strip() == (
        'BLOCKED plan:sample | review-loop-stalled | options: wait for a family, *waive by D-row')


def test_auth_waiver_refused(tmp_path, capsys):
    directory = setup_plan(tmp_path, capsys)
    _, manifest, rows, yields, _ = cli.load(directory)
    manifest.update(stalled=True, tags=['auth'])
    cli.save(directory, manifest, rows, yields)
    assert invoke(capsys, 'waive', '--dir', str(directory), '--decision', 'D-123')[0] == 2
    assert invoke(capsys, 'status', '--dir', str(directory)) == (1,
        'BLOCKED plan:sample | review-loop-stalled | options: wait for a family, *waive by D-row')


def test_unwitnessed_findings_exported_after_fake_round(tmp_path, capsys):
    directory = setup_plan(tmp_path, capsys)
    def spawn(argv, **kwargs):
        return SimpleNamespace(returncode=0, stdout=json.dumps(dict(status='completed',
            run_id='run-1', attempts=[dict(stdout_tail='```plan-review\nF1-1 | minor | state | plan | defect | proof | repair\n```\nVERDICT: ACCEPT')])) )
    assert cli.main(['round', '--dir', str(directory), '--kind', 'confirm'], spawn=spawn) == 0
    line = capsys.readouterr().out.strip()
    rows = json.loads((directory / 'ledger.json').read_text())
    yields = [json.loads(s) for s in (directory / 'yield.jsonl').read_text().splitlines()]
    assert rows[0]['family'] == 'xiaomi' and rows[0]['claim'] == 'defect'
    completed = [row for row in yields if row['status'] == 'completed']
    assert len(completed) == 1
    assert completed[0]['leg'] == 'mimo-v2.6-pro' and not completed[0]['witnessed']
    assert 'accepted=0/4' in line and 'unwitnessed_accepts=xiaomi' in line
    fields = ('id seat_id round family lens severity class claim location evidence fix scope_plus '
              'disposition disposition_evidence trigger sha_seen sha_fixed closed_by unverified sightings dup_of')
    assert set(fields.split()) <= rows[0].keys()
