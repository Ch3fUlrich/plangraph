import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location('sweep', Path(__file__).parents[1] / 'tools/mimo_path_sweep.py')
sweep = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sweep)

def test_precise_key_exception(tmp_path):
    bare = 'mimo/mimo-v2.6-flash'.split('/', 1)[1]
    path = tmp_path / 'opencode.jsonc'
    for provider, expected in [('mimo', 0), ('omniroute', 1)]:
        path.write_text('{\n"providers": {\n"' + provider + '": {\n"models": {\n"' + bare + '": {\n}\n}\n}\n}\n}')
        assert len(list(sweep.scan(tmp_path, []))) == expected
    path.write_text('{"model": "omniroute/' + bare + '"}')
    assert len(list(sweep.scan(tmp_path, []))) == 1
