pub(super) const CODEX: &str = r#"#!/usr/bin/env python3
import sys, pathlib, subprocess, json
if sys.argv[1] == 'sandbox':
    sys.exit(subprocess.call(sys.argv[sys.argv.index('--') + 1:]))
prompt = sys.stdin.read()
if 'sandbox error marker' in prompt:
    print(json.dumps({'type':'thread.started','thread_id':'sandbox-error-session'}))
    print(json.dumps({'type':'item.completed','item':{'type':'command_execution','exit_code':1,'aggregated_output':'bwrap: setting up uid map: Permission denied'}}))
    print(json.dumps({'type':'turn.completed','usage':{'input_tokens':10,'output_tokens':10}}))
    sys.exit(0)
if 'resume' in sys.argv:
    brief = pathlib.Path(json.loads(prompt.strip().removeprefix('Read follow-up brief at ')))
    assert brief.stat().st_mode & 0o777 == 0o600 and brief.parent == pathlib.Path.cwd().parent
    prompt = brief.read_text()
    original, heading, prompt = prompt.partition('\n\n# Follow-up\n\n')
    expected = ('brief marker' if 'external fix' in prompt else
                'brief marker client marker' if 'client marker' in original else
                'brief marker reject marker\nWhy: fixture item\n# Run values\nNAME: fixture-batch\n')
    assert heading and original == expected, original
if 'kill marker' in prompt or 'fail marker' in prompt or 'missing marker' in prompt:
    import os, signal
    attempts = pathlib.Path('attempts')
    attempt = int(attempts.read_text()) + 1 if attempts.exists() else 1
    attempts.write_text(str(attempt))
    if 'fail marker' in prompt:
        sys.exit(127)
    if attempt == 1:
        if 'missing marker' in prompt:
            print(json.dumps({'type':'thread.started','thread_id':'incomplete-session'}))
            sys.exit(0)
        pathlib.Path('partial.txt').write_text('preserved writer work')
        subprocess.run(['git','add','partial.txt'], check=True)
        subprocess.run(['git','commit','-m','partial writer work'], check=True, stdout=subprocess.DEVNULL)
        checkout = pathlib.Path(subprocess.check_output(['git','rev-parse','--path-format=absolute','--git-common-dir'], text=True).strip()).parent
        (checkout/'fresh.txt').write_text('new main base')
        subprocess.run(['git','-C',str(checkout),'add','fresh.txt'], check=True)
        subprocess.run(['git','-C',str(checkout),'-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','-m','fresh main'], check=True, stdout=subprocess.DEVNULL)
        subprocess.run(['git','-C',str(checkout),'push','origin','main'], check=True, stdout=subprocess.DEVNULL)
        print(json.dumps({'type':'thread.started','thread_id':'killed-session'}), flush=True)
        os.kill(os.getpid(), signal.SIGKILL)
    if 'kill marker' in prompt:
        assert pathlib.Path('fresh.txt').read_text() == 'new main base'
        assert pathlib.Path('partial.txt').read_text() == 'preserved writer work'

assert any(marker in prompt for marker in ('brief marker', 'requests changes', 'external fix')), prompt
assert 'model_reasoning_effort=medium' in sys.argv
assert sys.argv[sys.argv.index('-m') + 1] == 'gpt-6.2-sol'
if 'hold marker' in prompt:
    import os, time
    child = subprocess.Popen(['sleep', '30'])
    pathlib.Path('hold.pid').write_text(str(os.getpid()) + ' ' + str(child.pid))
    time.sleep(30)
    sys.exit(0)
if 'resume' in sys.argv:
    assert sys.argv[sys.argv.index('resume') + 1] == 'fixture-session'
    launch = sys.argv[:sys.argv.index('resume')]
    git_dir = subprocess.check_output(['git', 'rev-parse', '--absolute-git-dir'], text=True).strip()
    assert pathlib.Path(launch[launch.index('--cd') + 1]).resolve() == pathlib.Path.cwd()
    assert launch[launch.index('--add-dir') + 1] == git_dir
    if 'external fix' in prompt:
        import os
        record = json.loads((pathlib.Path.cwd().parent/'record.json').read_text())
        assert os.environ['PATH'] == str(pathlib.Path.cwd().parent / 'cargo-shim') + ':' + record['tool_path']
        assert 'sandbox_mode="workspace-write"' in sys.argv
    else:
        assert 'requests changes' in prompt and 'HIGH writer.txt:1' in prompt, prompt
        expected = 'reject marker' in original and not pathlib.Path('fix.txt').exists()
        assert ('drop the fixture scope' in prompt) == expected, prompt
    # The fix turn commits its fix: a turn that leaves HEAD unchanged is resumed on its items.
    with open('fix.txt', 'a') as fix: fix.write('fixed after review\n')
    subprocess.run(['git','add','fix.txt'], check=True)
    subprocess.run(['git','-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','-m','fix'], check=True, stdout=subprocess.DEVNULL)
else:
    import os
    if 'reject marker' in prompt:
        run = pathlib.Path.cwd().parent
        queued = subprocess.run(['fixture-spawnd', 'steer-local', run.name,
            '--state', str(run.parent.parent)], input='drop the fixture scope', text=True, capture_output=True)
        assert json.loads(queued.stdout)['steered'], (queued.stdout, queued.stderr)
    pathlib.Path('writer-path').write_text(os.environ['PATH'])
    marker = next((name for name in ('reject marker', 'client marker', 'late marker') if name in prompt), '')
    pathlib.Path('writer.txt').write_text('written on selected host\n' + marker)
    subprocess.run(['git','add','writer.txt','writer-path'], check=True)
    subprocess.run(['git','-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','-m','writer'], check=True, stdout=subprocess.DEVNULL)
    print(json.dumps({'type':'thread.started','thread_id':'fixture-session'}))
    if 'quota marker' in prompt:
        import datetime
        at = datetime.datetime.now() + datetime.timedelta(days=3)
        day = at.day
        ordinal = 'th' if 11 <= day % 100 <= 13 else {1: 'st', 2: 'nd', 3: 'rd'}.get(day % 10, 'th')
        reset = at.strftime('%b ') + f"{day}{ordinal}" + at.strftime(', %Y ') + f"{int(at.strftime('%I'))}:" + at.strftime('%M %p')
        limit = f"You've hit your usage limit. Upgrade to Pro, or try again at {reset}."
        print(json.dumps({'type':'error','message':limit}))
        print(json.dumps({'type':'turn.failed','error':{'message':limit}}))
        sys.exit(1)
print(json.dumps({'type':'turn.completed','usage':{'input_tokens':1000,'output_tokens':234}}))
print('tokens used\n1,234')
"#;
