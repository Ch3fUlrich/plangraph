"""tools/hooks/redact_common.py: the one credential masker that the fleet hooks
and tools/autoos_refs_check.py share.

Every secret is synthetic and built from fragments; ``assert_no_leak`` never
prints one.
"""
from __future__ import annotations

import time

import pytest

from autoos_refs_support import assert_masked, assert_no_leak
from fleet_report_support import hooks  # noqa: F401  (a fixture)

SECRET = "hunter" + "2pw"
WORDS = ("pass", "secret", "token", "key", "dsn", "auth", "cred")


@pytest.fixture
def mask(hooks):
    return hooks[3].redact_credentials


@pytest.mark.parametrize("word", WORDS)
@pytest.mark.parametrize("form", [
    "{k}={s}", "{k}: {s}", "{k} = {s}", '"{k}": "{s}"', "'{k}': '{s}'", '{k}="{s}"',
    "Environment={k}={s}", 'Environment="{k}={s}"', "Environment='{k}={s}'",
    'Environment="A=1" "{k}={s}" B=2', "export {k}={s}; echo done", "?{k}={s}&x=1",
    "--{k} {s}", "--{k}={s}", "{k} => {s}", "{k}:= {s}", "A=B={k}={s}", "x {k}={s} y",
    r'{{\"{k}\": \"{s}\"}}', r'"env": "{{\"{k}\": \"{s}\", \"n\": 1}}"',
])
def test_value_of_a_secret_key_is_masked_anywhere_in_the_line(mask, word: str, form: str) -> None:
    for key in (f"my_{word}", f"MY_{word.upper()}", f"My{word.title()}Name", word.upper()):
        text = form.format(k=key, s=SECRET)
        out = mask(text)
        assert_no_leak((SECRET,), out)
        assert "[redacted]" in out
        assert mask(out) == out  # idempotent: filed once, read again


def test_masks_keep_the_key_and_the_surroundings(mask) -> None:
    assert_masked(mask(f"Environment=AUTOOS_PG_DSN=postgresql://user:{SECRET}@host"),
                  "Environment=AUTOOS_PG_DSN=[redacted]", SECRET)
    assert_masked(mask(f'Environment="A=1" "DB_PASSWORD={SECRET} two" B=2'),
                  'Environment="A=1" "DB_PASSWORD=[redacted]" B=2', SECRET)
    assert_masked(mask(f'{{"token": "{SECRET}", "n": 1}}'), '{"token": [redacted], "n": 1}', SECRET)
    assert_masked(mask(f"password={SECRET},x=1", "<redacted>"), "password=<redacted>", SECRET)
    assert_masked(mask(f"--name x --password {SECRET} --keep-going"),
                  "--name x --password [redacted] --keep-going", SECRET)


def test_url_userinfo_password_becomes_stars(mask) -> None:
    assert_masked(mask(f"url=scheme://user:{SECRET}@host/db"), "url=scheme://user:***@host/db", SECRET)
    assert_masked(mask(f"redis://:{SECRET}@host:6379/0"), "redis://:***@host:6379/0", SECRET)
    assert_masked(mask(f"x scheme://u:p%40{SECRET}@host y"), "x scheme://u:***@host y", SECRET)
    assert_masked(mask(f"s://u:p@{SECRET}:w?d#e@host/x"), "s://u:***@host/x", SECRET)
    assert_masked(mask(f"s://u:{SECRET}@h/x", "<m>"), "s://u:***@h/x", SECRET)  # whatever the mask
    for plain in ("ssh://git@host:org/repo", "http://host:8080/x?mail=a@b",
                  "jdbc:postgresql://host/db", "scheme://user@host"):
        assert mask(plain) == plain


def test_an_auth_scheme_belongs_to_the_value(mask) -> None:
    for text in (f"Authorization: Basic {SECRET}", f"Authorization: Bearer {SECRET}",
                 f'"authorization": "Token {SECRET}"', f"proxy-authorization={SECRET}"):
        out = mask(text)
        assert_no_leak((SECRET,), out)
        assert "Basic" not in out and "Bearer" not in out and "Token" not in out


def test_known_key_shapes_are_masked_without_a_key(mask) -> None:
    shapes = ("sk-" + "live-" + "A1b2C3d4E5f6G7h8", "ghp_" + "A1b2" * 5, "gho_" + "A1b2" * 5,
              "github_pat_" + "A1b2_" * 5, "xoxb-" + "123-456-" + "AbCd", "AKIA" + "ABCD1234" * 2,
              "Bearer " + "abc.def-ghi", "eyJ" + "abc.eyJdef.ghi", "sk-" + "live-ZZZZ")
    for shape in shapes:
        out = mask(f"see {shape} here")
        assert_no_leak((shape.split()[-1],), out)
        assert out == "see [redacted] here"


def test_everyday_words_that_contain_a_secret_word_are_kept(mask) -> None:
    for text in ("tests passed: 41, failed: 0", "author: someone", "keyboard=us",
                 "credit: none", "bypass: yes", "monkeypatch=on", "keyword: DONE"):
        assert mask(text) == text
    assert mask("pass=ok") == "pass=[redacted]"  # a bare 'pass' key is a secret
    assert mask("authorization: x") == "authorization: [redacted]"  # 'author' is not 'authoriz'


def test_what_is_not_a_secret_is_kept_verbatim(mask) -> None:
    for text in ("", "plain words only", "Description=Test Service", "n=1 m: 2",
                 "ExecStart=/usr/bin/daemon --verbose", "token=", "key:", "a=b=c"):
        assert mask(text) == text
    assert mask(None) == "" and mask(5) == ""


def test_mask_text_is_the_callers(mask) -> None:
    assert_masked(mask(f"token={SECRET}", "<redacted>"), "token=<redacted>", SECRET)
    assert_masked(mask(f"token={SECRET}"), "token=[redacted]", SECRET)
    assert_masked(mask("sk-" + "live-ZZZZ9999", "XX"), "XX", "ZZZZ9999")


def test_scans_stay_linear_on_hostile_lines(mask) -> None:
    n = 100_000
    for text in ("a" * n, "key" * (n // 3), "key=" * (n // 4), "a://u:" * (n // 6),
                 "http://u:" + "p" * n, '"' + "x" * n, "--t a " * (n // 6), "a=" * (n // 2),
                 '\\"key\\": \\"' * (n // 10), "Bearer " * (n // 7)):
        start = time.perf_counter()
        mask(text)
        assert time.perf_counter() - start < 3.0
