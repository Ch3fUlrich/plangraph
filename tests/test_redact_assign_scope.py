"""Python-only A1 decisions; A2, slice C and main's joins stay explicit."""
import ast
import importlib.util
import json
import py_compile
import re
import subprocess
import sys
import types
from pathlib import Path
import pytest
from plangraph.spawner import redact_tokens
from plangraph.spawner.env_redact import redact, redact_output, redact_source

TABLE = json.loads((Path(__file__).parent / 'fixtures/redact_name_scope.json').read_text())
NAME, MASK = 'MY_' + 'SECRET', '[REDACTED]'


@pytest.fixture(scope='module')
def pre_a1(tmp_path_factory):
    """Load both historical modules from local git objects, never current helpers."""
    root = Path(__file__).resolve().parents[1]
    directory = tmp_path_factory.mktemp('pre_a1')
    package = types.ModuleType('_redact_pre_a1')
    package.__path__ = [str(directory)]
    modules = {package.__name__: package}
    try:
        sys.modules.update(modules)
        for name in ('redact_tokens', 'env_redact'):
            path = directory / (name + '.py')
            path.write_bytes(subprocess.check_output(
                ['git', 'show', 'e7964367:src/plangraph/spawner/' + name + '.py'], cwd=root))
            spec = importlib.util.spec_from_file_location(package.__name__ + '.' + name, path)
            module = importlib.util.module_from_spec(spec)
            modules[spec.name] = sys.modules[spec.name] = module
            spec.loader.exec_module(module)
        yield module
    finally:
        for name in modules:
            sys.modules.pop(name, None)


def _visible_positions(text, output):
    # Scrubbers replace spans with MASK; map every remaining byte/character to input.
    pattern = r'[\s\S]*?'.join('(' + re.escape(part) + ')' for part in output.split(MASK))
    match = re.fullmatch(pattern, text)
    assert match is not None, (text, output)
    return {i for group in range(1, len(match.groups()) + 1)
            for i in range(*match.span(group))}


def _assert_known_gap(row, historical, current):
    assert row['known_gap'] in ('A2', 'slice C', 'plangraph:REDACT-RUST-NAME-SCOPE-ALIGN')
    assert row['accepted_by'] == 'D-5703'
    assert row['readable_parts']
    for piece in row['readable_parts']:
        assert piece in current and historical.count(piece) == current.count(piece), row['id']


@pytest.mark.parametrize('fixture', ['redact_name_scope', 'redact_parity'])
@pytest.mark.parametrize('mode', ['redact', 'redact_output', 'redact_source'])
def test_a1_masks_only_grow_against_pre_a1(pre_a1, fixture, mode):
    from plangraph.spawner import env_redact
    rows = json.loads((Path(__file__).parent / ('fixtures/' + fixture + '.json')).read_bytes())
    for row in rows:
        text = row.get('input', ''.join(row.get('input_parts', []))) * row.get('repeat', 1)
        old, current = getattr(pre_a1, mode)(text), getattr(env_redact, mode)(text)
        assert _visible_positions(text, current) <= _visible_positions(text, old), row.get('id')
        if mode == ('redact_source' if row.get('mode') == 'source' else 'redact'):
            expected = text if row.get('expected') == 'I' else ''.join(row.get('expected', ''))
            # Sweep both tables, including unlabelled tails and below-floor bodies.
            readable = re.findall(r'V[12]|B8(?:B8)*|A_-9[A_9-]*|A9(?:A9)+', expected)
            unchanged_assignment = row.get('id', '').startswith('a1-') and expected == text and any(c in text for c in '=:')
            if readable or unchanged_assignment or row.get('known_gap'):
                _assert_known_gap(row, old, current)
                assert set(readable) <= set(row['readable_parts']), row.get('id')


@pytest.mark.parametrize('nl', ['\n', '\r\n'])
@pytest.mark.parametrize('quote', ['', '"', "'"])
@pytest.mark.parametrize('marker', ['', '\ue000', '\ue001', '\ue0000\ue001'])
def test_a1_wrapped_regressions_keep_pre_a1_masks(pre_a1, nl, quote, marker):
    for name in (NAME, 'os.environ["' + NAME + '"]', 'e\u0301api_' + 'token'):
        text = marker + name + '=' + quote + 'gh' + 'p_' + 'A9' * 7 + nl + 'B8' * 8 + quote + nl + 'next retained'
        old, current = pre_a1.redact(text), redact(text)
        assert 'B8' not in old and 'B8' not in current
        assert _visible_positions(text, current) <= _visible_positions(text, old)


def test_a1_stray_closing_bracket(pre_a1):
    text = 'API_' + 'KEY=[REDACTED]]'
    assert pre_a1.redact(text) == redact(text) == redact_output(text) == 'API_' + 'KEY=' + MASK


@pytest.mark.parametrize('row', TABLE, ids=lambda r: r['id'])
@pytest.mark.parametrize('scrub', [redact, redact_output])
def test_a1_decisions(row, scrub):
    text = ''.join(row['input_parts']) * row.get('repeat', 1)
    expected = text if row['expected'] == 'I' else ''.join(row['expected'])
    if row.get('known_gap'):
        assert row['accepted_by'] == 'D-5703' and row['readable_parts']
    assert (redact_source if row.get('mode') == 'source' else scrub)(text) == expected


@pytest.mark.parametrize('h', [chr(i) for i in range(0x3100) if chr(i).isspace() and chr(i) not in '\r\n'])
def test_x01_all_27_horizontal_blanks(h):
    for before, after in [(h, ''), ('', h), (h, h)]:
        header = NAME + before + '=' + after
        assert redact(header + 'V1') == header + MASK


@pytest.mark.parametrize('middle', ['\v', '\f', '\x1c', '\x1d', '\x1e', '\x1f', '\x85', '\u2028', '\u2029', '\x00'])
def test_physical_ends_and_crlf_bytes(middle):
    for ending in ('\r', '\n', '\r\n'):
        assert redact(NAME + '=V1' + middle + 'V2' + ending + 'tail').encode() == (NAME + '=' + MASK + ending + 'tail').encode()


@pytest.mark.parametrize('keyword', ['to'+'ken', 'ſe'+'cret', 'pass'+'word', 'pass'+'wd', 'api'+'key', 'api_'+'key', 'access'+'key', 'access_'+'key', 'private'+'key', 'private_'+'Key', 'cred'+'ential'])
def test_x02_wide_folding_offsets(keyword):
    name = 'İ９e\u0301\u203f\u200c\u200d' + keyword + 's'
    assert redact(name + '=V1') == name + '=' + MASK
    for dot in ('İ', 'ı'):
        nonkeyword = 'xap' + dot + 'key'
        assert redact(nonkeyword + '=V1') == nonkeyword + '=V1'


@pytest.mark.parametrize('quote', ['"', "'"])
@pytest.mark.parametrize('name', [NAME, 'TO'+'KEN', 'API_'+'KEY'])
def test_x03_exact_subscripts(quote, name):
    header = 'os.environ[' + quote + name + quote + '] = '
    assert redact(header + '"V1" tail') == header + MASK


@pytest.mark.parametrize('inside', ['', '.', ' ', '!'])
def test_exact_wide_run_exclusion(inside):
    header = '{"' + inside + NAME + '"\v: '
    assert redact(header + 'V1}') == (header + 'V1}' if not inside else header + MASK)


def test_x04_old_overlap_and_inner_suppression():
    old = 'TO' + 'KEN'
    assert redact('e\u0301' + 'API_' + 'KEY="V1"tail') == 'e\u0301' + 'API_' + 'KEY="' + MASK + '"tail'
    assert redact(old + '="V1" ' + NAME + '=V2') == old + '="' + MASK + '" ' + NAME + '=' + MASK
    # known gap for A2 (b) old label swallowed
    assert redact(NAME + '=V1 ' + old + '="V2"tail') == NAME + '=' + MASK
    assert redact(old + '=V1 ' + NAME + '=V2') == old + '=' + MASK
    assert redact(old + '\n=\n' + NAME + '=V1') == old + '\n=\n' + MASK


@pytest.mark.parametrize('tail', ['', ' ', '\v\u2028'])
def test_x05_empty_and_horizontal_only(tail):
    assert redact(NAME + '=' + tail) == NAME + '=' + tail


def test_a1_artifacts_parse(tmp_path):
    root = Path(__file__).parent
    for fixture in ('redact_name_scope', 'redact_parity'):
        raw = (root / ('fixtures/' + fixture + '.json')).read_bytes()
        rows = json.loads(raw)  # Strict JSON rejects literal CR/LF inside strings.
        assert rows
        for row in rows:
            for part in row.get('input_parts', []):
                assert isinstance(part, str)
                encoded = json.dumps(part)
                assert '\r' not in encoded and '\n' not in encoded
                assert json.loads(encoded) == part
        for broken in (b'["raw\nline"]', b'["raw\rline"]', b'["broken]'):
            with pytest.raises(json.JSONDecodeError):
                json.loads(broken)
    for name in ('test_redact_assign_scope', 'test_redact_parity', 'test_spawner_runner_answer'):
        py_compile.compile(str(root / (name + '.py')), cfile=str(tmp_path / (name + '.pyc')), doraise=True)
    tree = ast.parse((root / 'test_opencode_json_limit.py').read_text())
    imports = {alias.name for node in tree.body if isinstance(node, ast.Import) for alias in node.names}
    assert {'json', 'pytest'} <= imports


def test_a1_marker_and_deferred_gap_coverage(pre_a1):
    for marker in ('\ue000', '\ue001', '\ue0000\ue001'):
        assert any(marker in ''.join(row['input_parts']) and row['expected'] != 'I' for row in TABLE)
    gaps = ('N16', 'N19', 'N20', 'N21', 'A02', 'A07', 'A08', 'N59', 'N60')
    for suffix in gaps:
        row = next(row for row in TABLE if row['id'] == 'a1-' + suffix)
        assert row.get('gap', '').startswith(('A2', 'D-5685')), row['id']
    wrapped = next(row for row in TABLE if row['id'] == 'a1-J04')
    text = ''.join(wrapped['input_parts'])
    _assert_known_gap(wrapped, pre_a1.redact(text), redact(text))
    assert _visible_positions(text, redact(text)) < _visible_positions(text, pre_a1.redact(text))


@pytest.mark.parametrize('scrub', [redact, redact_output])
def test_a1_markers_do_not_bypass_name_masking(scrub):
    for row in TABLE:
        if row['id'].startswith('a1-M'):
            text = ''.join(row['input_parts'])
            unmarked = text.replace('\ue0000\ue001', '').replace('\ue000', '').replace('\ue001', '')
            result = scrub(text).replace('\ue0000\ue001', '').replace('\ue000', '').replace('\ue001', '')
            assert result == scrub(unmarked)


def _redact_work(text):
    """Count scanner/folding steps, independent of scheduling and CI load."""
    operations = 0
    def trace(frame, event, arg):
        nonlocal operations
        if frame.f_code.co_filename == redact_tokens.__file__:
            operations += event == 'line'
            return trace
    previous = sys.gettrace()
    try:
        sys.settrace(trace)
        result = redact(text)
    finally:
        sys.settrace(previous)
    assert operations > 0
    return result, operations


def test_x06_keyword_child_timeout(monkeypatch):
    # Keep the historical name and 100k-character adversary, not a wall-clock deadline.
    searches, keyword = [], redact_tokens._NAME_KEYWORD
    class CountSearch:
        def search(self, text):
            searches.append(len(text))
            return keyword.search(text)
    monkeypatch.setattr(redact_tokens, '_NAME_KEYWORD', CountSearch())
    text = ('TO'+'KEN') * 20000
    for suffix in ('', '=V1'):
        result, operations = _redact_work(text + suffix)
        assert result == text + ('=' + MASK if suffix else '')
        assert operations < 150 * len(text)
    assert searches == [len(text), len(text), 2]


@pytest.mark.parametrize('kind', ['masked-lines', 'runs', 'failed-headers', 'new-blanks', 'old-blanks'])
def test_x07_linear_scaling(kind):
    counts = []
    # Trace counts need no huge/noisy timing samples; 4x rejects quadratic (16x) growth.
    for size in (1, 4):
        # Preserve the original large functional workloads; instrument a smaller copy.
        for scale in (1, 100):
            n = size * scale
            text = {'masked-lines': (NAME + '=' + MASK + '\n') * (500 * n), 'runs': 'x' * (10000 * n),
                    'failed-headers': (NAME + ' ? ') * (10000 // (len(NAME) + 3) * n),
                    'new-blanks': NAME + '=' + ' ' * (6000 * n), 'old-blanks': 'TO'+'KEN=' + ' ' * (6000 * n)}[kind]
            expected = text[:-1] + MASK if kind == 'old-blanks' else text
            if scale == 1:
                result, operations = _redact_work(text)
                counts.append(operations)
            else:
                result = redact(text)
            assert result == expected
    assert counts[0] < counts[1] <= counts[0] * 4
