import pytest

from test_run_prompt_record import test_run_call_sites_require_prompt_record as check_seams
from plangraph.spawner import runner


@pytest.mark.parametrize('arguments', ["'p', 'model'", '**kwargs', '*args, **kwargs'])
def test_guard_rejects_every_argument_form(tmp_path, monkeypatch, arguments):
    root = tmp_path / 'spawner'
    root.mkdir()
    (root / 'probe.py').write_text(f'def probe():\n    service.spawn({arguments})\n')
    monkeypatch.setattr(runner, '__file__', str(root / 'runner.py'))
    with pytest.raises(AssertionError, match=r"\('probe.py', 'probe'\)"):
        check_seams()
