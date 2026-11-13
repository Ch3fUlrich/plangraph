"""Python-only A1 decisions; A2, slice C and main's joins stay explicit."""
import json
import statistics
import subprocess
import sys
import time
from pathlib import Path
import pytest
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


def test_x06_keyword_child_timeout():
    code = ("import sys,time; sys.path.insert(0,sys.argv[1]); from plangraph.spawner.env_redact import redact; "
            "s=('TO'+'KEN')*20000; t=time.perf_counter(); assert redact(s)==s; "
            "assert time.perf_counter()-t<2; assert redact(s+'=V1')==s+'=[REDACTED]'")
    subprocess.run([sys.executable, '-B', '-c', code, str(Path(__file__).parents[1] / 'src')], check=True, timeout=5)


@pytest.mark.parametrize('kind', ['masked-lines', 'runs', 'failed-headers', 'new-blanks', 'old-blanks'])
def test_x07_linear_scaling(kind):
    timings = []
    # Large 1x workloads avoid millisecond noise; 4x separates linear (4) from quadratic (16).
    for size in (1, 4):
        text = {'masked-lines': (NAME + '=' + MASK + '\n') * (50000 * size), 'runs': 'x' * (1000000 * size),
                'failed-headers': (NAME + ' ? ') * (1000000 * size // (len(NAME) + 3)),
                'new-blanks': NAME + '=' + ' ' * (600000 * size), 'old-blanks': 'TO'+'KEN=' + ' ' * (600000 * size)}[kind]
        expected = text[:-1] + MASK if kind == 'old-blanks' else text
        samples = []
        for _ in range(7):
            start = time.perf_counter(); assert redact(text) == expected; samples.append(time.perf_counter() - start)
        timings.append(statistics.median(samples))
    assert timings[1] < timings[0] * 7
