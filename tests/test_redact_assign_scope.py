"""Legacy assignment compatibility and bounded Rust-shaped widened names."""
import json
import subprocess
import sys
import time

import pytest

from plangraph.spawner.env_redact import opencode_limit, redact, redact_output


# Literal outputs captured from main d8cf0586, including its unusual quote and
# cross-line separator behavior. Inputs are synthetic and assembled in pieces.
OLD_CASES = [
    ('PASSWORD=' + 'v', 'PASSWORD=[REDACTED]'),
    ('Password: ' + 'v', 'Password: [REDACTED]'),
    ('TOKEN = ' + 'v w', 'TOKEN = [REDACTED]'),
    ('SECRET: ' + 'v, next', 'SECRET: [REDACTED]'),
    ('PG_DSN=' + 'db://u:v@host/x', 'PG_DSN=[REDACTED]'),
    ('pg_dsn : ' + 'v', 'pg_dsn : [REDACTED]'),
    ('API_KEY=' + 'v', 'API_KEY=[REDACTED]'),
    ('export PRIVATE_KEY=' + 'v', 'export PRIVATE_KEY=[REDACTED]'),
    ('é_KEY=' + 'v', 'é_KEY=[REDACTED]'),
    ('ключ_KEY: ' + 'v', 'ключ_KEY: [REDACTED]'),
    ('"PASSWORD": ' + '"v"', '"PASSWORD": "[REDACTED]"'),
    ("'TOKEN' = " + "'v'", "'TOKEN' = '[REDACTED]'"),
    ('"PG_DSN"=' + "'v'", '"PG_DSN"=\'[REDACTED]\''),
    ('PASSWORD=' + '"a\\"b" tail', 'PASSWORD="[REDACTED]" tail'),
    ('SECRET=' + "'a\\'b' tail", "SECRET='[REDACTED]' tail"),
    ('TOKEN=' + '"unterminated', 'TOKEN=[REDACTED]'),
    ('TOKEN=' + '"a"b', 'TOKEN="[REDACTED]"b'),
    ('PASSWORD=' + '"a"; next', 'PASSWORD="[REDACTED]"; next'),
    ('{TOKEN: ' + 'v}', '{TOKEN: [REDACTED]'),
    ('?API_KEY=' + 'v&next=1', '?API_KEY=[REDACTED]'),
    ('Password:\n' + 'VERDICT: ACCEPT', 'Password:\n[REDACTED]'),
    ('TOKEN\r\n=\r\n' + 'v', 'TOKEN\r\n=\r\n[REDACTED]'),
    ('SECRET=\n' + 'next line', 'SECRET=\n[REDACTED]'),
    ('API_KEY=   ', 'API_KEY=  [REDACTED]'),
    ('TOKEN=', 'TOKEN='),
    ('notPASSWOR=' + 'v', 'notPASSWOR=v'),
    ('_KEY=' + 'v', '_KEY=[REDACTED]'),
    ('KEY=' + 'v', 'KEY=v'),
    ('"api_key": ' + '12345678', '"api_key": [REDACTED]'),
    ('"api_key": ' + '"v","next":1', '"api_key": "[REDACTED]","next":1'),
    ('x gh' + 'p_' + 'A9' * 7 + '\nAPI_KEY=' + 'v', 'x [REDACTED]=[REDACTED]'),
    ('x gh' + 'p_' + 'A9' * 7 + '\r\nPASSWORD=' + 'v', 'x [REDACTED]=[REDACTED]'),
    ('API_KEY=gh' + 'p_' + 'A9' * 7 + '\nB8' * 8 + '\nnext retained',
     'API_KEY=[REDACTED]\nnext retained'),
]


@pytest.mark.parametrize('text,expected', OLD_CASES)
def test_old_assignment_outputs_equal_main(text, expected):
    assert redact(text) == expected


@pytest.mark.parametrize('word', ['TOKEN', 'SECRET_', 'ab_KEYx', 'PASSWORD', 'x'])
def test_assignment_word_run_cost_is_linear(word):
    # Pre-fix TOKEN * 20000 took above 80 s. Bound the child too, so the
    # regression fails promptly instead of stalling a CI worker for minutes.
    code = ('import sys; sys.path[:] = ' + repr(sys.path) + '; '
            'from plangraph.spawner.env_redact import redact; '
            's = sys.argv[1] * 20000; assert redact(s) == s')
    started = time.monotonic()
    subprocess.run([sys.executable, '-c', code, word], check=True, timeout=5)
    assert time.monotonic() - started < 5


@pytest.mark.parametrize('text', ['Tokens:\nVERDICT: ACCEPT',
                                 'max_tokens:\r\nnext line', 'MY_SECRET:\nvalue',
                                 'MY_SECRET\n= value', 'AUTH_TOKEN=\nvalue'])
def test_new_assignment_separator_never_crosses_lines(text):
    assert redact(text) == text


def test_new_json_scalar_keeps_usage_limit_error_parseable():
    text = '{"type":"error","max_tokens":4096,"error":{"status":429}}'
    expected = '{"type":"error","max_tokens":"[REDACTED]","error":{"status":429}}'
    for scrub in (redact, redact_output):
        assert scrub(text) == expected
        assert opencode_limit(scrub(text)) == 'HTTP 429: usage_limit'


def test_new_json_scalar_masks_each_usage_counter():
    text = '{"usage":{"input_tokens":12,"output_tokens":34}}'
    assert json.loads(redact(text)) == {
        'usage': {'input_tokens': '[REDACTED]', 'output_tokens': '[REDACTED]'}}


@pytest.mark.parametrize('scalar', ['12345678', '-12.5e+3', 'true', 'false', 'null'])
def test_new_json_scalar_never_exposes_numeric_or_literal_credentials(scalar):
    text = '"db_' + 'password": ' + scalar
    assert redact(text) == '"db_password": "[REDACTED]"'


@pytest.mark.parametrize('sep', ['=', ': '])
def test_new_assignment_never_exposes_synthetic_value(sep):
    value = 'hunter' + '2value'
    assert redact('MY_' + 'SECRET' + sep + value) == 'MY_SECRET' + sep + '[REDACTED]'
    assert redact('DB_' + 'PASSWORD=' + 'v, next') == 'DB_PASSWORD=[REDACTED]'
