"""Package policy comes from the commit gate and refuses incomplete branches."""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('package_gate', ROOT / 'tools/ci_commit_gate.py')
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)


def test_package_rules_cover_gate_trailers():
    rules = gate.package_rules()
    for pattern in (gate.WRITER_RE, gate.HAND_EDIT_RE, gate.GATE_EDIT_RE, gate.TEST_REMOVAL_RE):
        assert pattern.pattern in rules['trailer_patterns']
    assert 'Writer: {writer}' in rules['template']
    assert 'Plan-Item: plangraph:{key}' in rules['template']
    for text in ('Sergej Maul', '71930650+Ch3fUlrich' + '@' + 'users.noreply.github.com',
                 'STATUS-', '800', 'Rust', 'affected', 'ALL DONE'):
        assert text in rules['template']
