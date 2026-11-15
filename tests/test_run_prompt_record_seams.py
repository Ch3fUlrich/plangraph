import pytest
import shutil
from collections import Counter
from pathlib import Path

from test_run_prompt_record import (
    check_run_call_sites,
    test_run_call_sites_require_prompt_record as check_seams,
)
from plangraph.spawner import runner


@pytest.mark.parametrize('arguments', ["'p', 'model'", '**kwargs', '*args, **kwargs'])
def test_guard_rejects_every_argument_form(tmp_path, monkeypatch, arguments):
    root = tmp_path / 'spawner'
    root.mkdir()
    (root / 'probe.py').write_text(f'def probe():\n    service.spawn({arguments})\n')
    monkeypatch.setattr(runner, '__file__', str(root / 'runner.py'))
    check_expected_probe(root)


def check_expected_probe(root):
    with pytest.raises(AssertionError, match=r"\('probe.py', 'probe',"):
        check_run_call_sites(root, Counter())


def test_probe_expectation_rejects_recorded_call(tmp_path):
    (tmp_path / 'probe.py').write_text(
        'def probe():\n    service.spawn(prompt_record=record)\n')
    with pytest.raises(pytest.fail.Exception, match='DID NOT RAISE'):
        check_expected_probe(tmp_path)


@pytest.mark.parametrize('mutation', ['new_call', 'stale_entry'])
@pytest.mark.parametrize(('filename', 'original'), [
    ('base_fresh.py', 'service.spawn(**payload, wait_seconds=0)'),
    ('cli.py', 'service.spawn(**payload, wait_seconds=None)'),
])
def test_guard_checks_exact_allowlisted_call(tmp_path, mutation, filename, original):
    root = tmp_path / 'spawner'
    shutil.copytree(Path(runner.__file__).parent, root)
    path = root / filename
    source = path.read_text()
    assert source.count(original) == 1, 'mutation must change exactly one real call site'
    replacement = ('(' + original + ', runner.spawn(prompt=p, model_or_combo=m))[0]'
                   if mutation == 'new_call' else 'service.spawn(**payload, wait_seconds=1, prompt_record=record)')
    path.write_text(source.replace(original, replacement))
    with pytest.raises(AssertionError, match='runner.spawn' if mutation == 'new_call' else 'stale allowlist'):
        check_seams(root)
