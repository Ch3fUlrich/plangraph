"""Python-only A1 decisions; A2, slice C and main's joins stay explicit."""
import ast
import functools
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
                ['git', 'show', 'e7964367542646ea603a1fc84f67f7036b298bcb:src/plangraph/spawner/' + name + '.py'], cwd=root))
            spec = importlib.util.spec_from_file_location(package.__name__ + '.' + name, path)
            module = importlib.util.module_from_spec(spec)
            modules[spec.name] = sys.modules[spec.name] = module
            spec.loader.exec_module(module)
        yield module
    finally:
        for name in modules:
            sys.modules.pop(name, None)


def test_pre_a1_uses_pinned_scanner(pre_a1):
    historical = sys.modules[pre_a1.__package__ + '.redact_tokens']
    for name in ('redact_wrapped', 'join_shielded_assignments'):
        helper = getattr(pre_a1, name)
        assert helper is getattr(historical, name)
        assert helper is not getattr(redact_tokens, name)
        assert Path(helper.__code__.co_filename).parent == Path(pre_a1.__file__).parent
    assert not hasattr(historical, 'redact_wide_assignments')
    row = next(row for row in TABLE if row['id'] == 'a1-N01')
    text = ''.join(row['input_parts'])
    assert pre_a1.redact(text) == text
    assert redact(text) == ''.join(row['expected']) != pre_a1.redact(text)


def _visible_positions(text, output):
    """Map retained characters, preserving literal masks; refuse ambiguous offsets."""
    if text == output:
        return set(range(len(text)))
    parts = re.split('(' + re.escape('"' + MASK + '"') + '|' + re.escape(MASK) + ')', output)

    @functools.lru_cache(None)
    def align(part, offset):
        if part == len(parts):
            return (frozenset(),) if offset == len(text) else ()
        token, choices = parts[part], set()
        if text.startswith(token, offset):
            retained = frozenset(range(offset, offset + len(token)))
            choices.update(retained | rest for rest in align(part + 1, offset + len(token)))
        if token in (MASK, '"' + MASK + '"'):
            following = parts[part + 1]
            # Quoted scalar masks can insert at an empty value; literals retain themselves.
            end = offset + int(token == MASK)
            while end <= len(text):
                if text.startswith(following, end):
                    literal = frozenset(range(offset, end)) if text[offset:end] == token else frozenset()
                    choices.update(literal | rest for rest in align(part + 1, end))
                    if len(choices) > 1:
                        break
                end += 1
        return tuple(choices)

    choices = align(0, 0)
    assert len(choices) == 1, 'invalid or ambiguous redaction alignment'
    return set(choices[0])


def test_visible_positions_preserve_literal_masks():
    assert _visible_positions(MASK, MASK) == set(range(len(MASK)))
    text = MASK + ' tail hidden'
    assert _visible_positions(text, MASK + ' tail ' + MASK) == set(range(len(MASK + ' tail ')))
    assert _visible_positions(NAME + '=' + MASK + ' tail', NAME + '=' + MASK) == set(range(len(NAME + '=')))
    assert _visible_positions(NAME + '=}', NAME + '=' + MASK) == set(range(len(NAME + '=')))
    with pytest.raises(AssertionError, match='ambiguous'):
        _visible_positions('x a a y', MASK + 'a' + MASK)
    for value in ('12', '', '"V1"'):
        assert _visible_positions('x:' + value + '}', 'x:"' + MASK + '"}') == {0, 1, len(value) + 2}


@pytest.mark.parametrize('row_id,masked', [('a1-N19', ()), ('a1-A02', ()),
                                         ('shield-marker-skips-prepass', (10, 28)),
                                         ('shield-marker-skips-prepass-e001', (10, 28)),
                                         ('shield-marker-skips-prepass-indexed', (12, 30))])
def test_ruling_join_gaps_match_real_pre_a1(pre_a1, row_id, masked):
    parity = json.loads((Path(__file__).parent / 'fixtures/redact_parity.json').read_bytes())
    row = next(row for row in TABLE + parity if row.get('id') == row_id)
    text = ''.join(row['input_parts'])
    assert row['known_gap'] == 'slice C'
    # MY_SECRET with a newline before '=' stays readable in Python, as before A1.
    for scrub in (redact, redact_output):
        old, current = getattr(pre_a1, scrub.__name__)(text), scrub(text)
        assert old == current
        _assert_known_gap(row, old, current)
        positions = set(range(*masked)) if masked else set()
        assert set(range(len(text))) - _visible_positions(text, old) == positions
        assert set(range(len(text))) - _visible_positions(text, current) == positions


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
    # MY_SECRET's value stays readable in pre-A1 Python (not an OLD name);
    # A1 now masks it, including the stray bracket, unlike the unchanged source mode.
    wider = NAME + '=[REDACTED]]'
    assert pre_a1.redact(wider) == redact_source(wider) == wider
    assert redact(wider) == redact_output(wider) == NAME + '=' + MASK


@pytest.mark.parametrize('row', [r for r in TABLE if 'reader' not in r], ids=lambda r: r['id'])
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
    assert redact(header + 'V1}') == header + ('"' + MASK + '"' if not inside else MASK)


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
    """Count both modules' Python steps; the child CPU bound also sees C regexes."""
    from plangraph.spawner import env_redact
    files = {redact_tokens.__file__, env_redact.__file__}
    operations = 0
    seen = set()
    def trace(frame, event, arg):
        nonlocal operations
        if frame.f_code.co_filename in files:
            seen.add(frame.f_code.co_filename)
            operations += event == 'line'
            return trace
    previous = sys.gettrace()
    try:
        sys.settrace(trace)
        result = redact(text)
    finally:
        sys.settrace(previous)
    assert operations > 0
    assert seen == files
    return result, operations


@pytest.mark.parametrize('old', [False, True], ids=['new-blanks', 'old-blanks'])
def test_blank_runs_bound_c_regex_work(old):
    # CPU time includes regex internals without a CI-load-sensitive scaling ratio.
    code = """import resource, sys
resource.setrlimit(resource.RLIMIT_CPU, (4, 4))
sys.path.insert(0, sys.argv[1])
from plangraph.spawner.env_redact import redact
old = sys.argv[2] == 'True'
text = ('TO' + 'KEN' if old else 'MY_' + 'SECRET') + '=' + ' ' * 2400000
assert redact(text) == (text[:-1] + '[REDACTED]' if old else text)
"""
    subprocess.run([sys.executable, '-B', '-c', code, str(Path(__file__).parents[1] / 'src'), str(old)],
                   check=True, timeout=20)


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
    assert searches == [len(text), len(text), len(text), len(text), len('REDACTED')]


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


@pytest.mark.parametrize('first', [',', '}', ']', '{', '[', '\x00', '', '\r', '\n', '\r\n'])
def test_a2a_first_byte(first):
    header = '"auth_' + 'token": \t'
    tail = first + '"V1"}' if first else ''
    assert redact(header + tail) == header + '"' + MASK + '"' + (tail if first.startswith(('\r', '\n')) else '')


def test_a2a_source_idempotence():
    for scalar in ('"V1\\uD800"', 'true', '-1.25e+2'):
        text = '{"auth_' + 'token":' + scalar + ',"next":1}'
        masked = '{"auth_' + 'token":"' + MASK + '","next":1}'
        assert redact_source(text) == text
        assert redact_output(text) == redact_output(masked) == masked


def test_a2a_scalar_child_timeout():
    code = ("import sys,statistics,timeit; sys.path.insert(0,sys.argv[1]); "
            "from plangraph.spawner.env_redact import redact_output; "
            "s=('\"auth_'+'token\":1,')*20000; assert len(s)==300000; "
            "assert redact_output(s).count('\"[REDACTED]\"')==20000; "
            "t=statistics.median(timeit.repeat(lambda:redact_output(s),number=1,repeat=7)); print(t); assert t<2")
    subprocess.run([sys.executable, '-B', '-c', code, str(Path(__file__).parents[1] / 'src')], check=True, timeout=20)


@pytest.mark.parametrize("field", [p + "tokens" for p in ("input_", "output_", "cache_read_input_", "cache_creation_input_")])
@pytest.mark.parametrize("prior", [False, True])
def test_a2a_masked_usage_aborts_no_upload(tmp_path, monkeypatch, field, prior):
    from types import SimpleNamespace
    from unittest.mock import Mock
    from plangraph.governor import cli, metrics
    from test_governor_metrics import _assistant, _write_jsonl
    row = _assistant("m", "r", "2024-01-01T00:00:00Z")
    row["message"]["usage"][field] = "[REDACTED]"
    _write_jsonl(tmp_path / "p" / "s.jsonl", ([_assistant("old", "r", "2024-01-01T00:00:00Z", inp=123)] if prior else []) + [row])
    for reader in (metrics.iter_usage, metrics.iter_usage_first):
        with pytest.raises(OSError): reader(tmp_path)
    session = Mock()
    assert cli._cmd_daily(SimpleNamespace(date="2024-01-01", projects_dir=str(tmp_path), weekly_cap=1, repo=None), session) == cli.EXIT_USAGE
    session.call.assert_not_called()
