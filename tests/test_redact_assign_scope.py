"""Python-only A1 decisions; A2, slice C and main's joins stay explicit."""
import ast
import json
import py_compile
import sys
from pathlib import Path
import pytest
from plangraph.spawner import redact_tokens
from plangraph.spawner.env_redact import redact, redact_output, redact_source

TABLE = json.loads((Path(__file__).parent / 'fixtures/redact_name_scope.json').read_text())
NAME, MASK = 'MY_' + 'SECRET', '[REDACTED]'


@pytest.mark.parametrize('row', TABLE, ids=lambda r: r['id'])
@pytest.mark.parametrize('scrub', [redact, redact_output])
def test_a1_decisions(row, scrub):
    text = ''.join(row['input_parts']) * row.get('repeat', 1)
    expected = text if row['expected'] == 'I' else ''.join(row['expected'])
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
    rows = json.loads((root / 'fixtures/redact_name_scope.json').read_text(encoding='utf-8'))
    assert rows == TABLE
    for name in ('test_redact_assign_scope', 'test_redact_parity', 'test_spawner_runner_answer'):
        py_compile.compile(str(root / (name + '.py')), cfile=str(tmp_path / (name + '.pyc')), doraise=True)
    tree = ast.parse((root / 'test_opencode_json_limit.py').read_text())
    imports = {alias.name for node in tree.body if isinstance(node, ast.Import) for alias in node.names}
    assert {'json', 'pytest'} <= imports


def test_a1_marker_and_deferred_gap_coverage():
    for marker in ('\ue000', '\ue001', '\ue0000\ue001'):
        assert any(marker in ''.join(row['input_parts']) and row['expected'] != 'I' for row in TABLE)
    gaps = ('N16', 'N19', 'N20', 'N21', 'A02', 'A07', 'A08', 'N59', 'N60')
    for suffix in gaps:
        row = next(row for row in TABLE if row['id'] == 'a1-' + suffix)
        assert row.get('gap', '').startswith(('A2', 'D-5685')), row['id']
    wrapped = next(row for row in TABLE if row['id'] == 'a1-J04')
    assert wrapped['gap'].startswith('D-5685')
    text = ''.join(wrapped['input_parts'])
    assert redact_tokens.redact_wrapped(text) == text  # Joined body is still below the floor.


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
