"""Masking of secrets in the detail lines of tools/autoos_refs_check.py.

The scanner prints the line that names AutoOS, so a secret on that line must
never survive: leading key or nested, bare or quoted, a URL password, a flag
value. Every secret is synthetic and ``assert_no_leak`` never prints one.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from autoos_refs_support import (  # noqa: F401  (ar is a fixture)
    ar, assert_masked, assert_no_leak, run_cli, write,
)

SECRET = "hunter" + "2pw"  # synthetic; built from fragments on purpose
PLACE = "@S@"  # where SECRET goes in LINES

# One line per way a credential shows up next to an AutoOS mention.
LINES = {
    "systemd nested dsn": "Environment=AUTOOS_PG_DSN=postgresql://user:@S@@host",
    "systemd nested dsn path": "Environment=AUTOOS_PG_DSN=postgresql://user:@S@@host/db?x=1",
    "systemd quoted env": 'Environment="AUTOOS_DB_PASSWORD=@S@ more" "A=1"',
    "dsn flag": "ExecStart=/x/AutoOS/bin/run --dsn postgresql://user:@S@@host/db",
    "flag then value": "ExecStart=AutoOS-run --password @S@",
    "flag equals value": "ExecStart=/x/AutoOS/bin/run --db-pass=@S@",
    "shell export": "export AUTOOS_API_TOKEN=@S@; /x/AutoOS/run",
    "json dsn": '{"cmd": "/x/AutoOS/h.py", "dsn": "postgresql://user:@S@@host/db"}',
    "json url key": '{"database_url": "postgresql://user:@S@@host/db", "p": "/x/AutoOS"}',
    "json escaped": r'{"env": "{\"api_key\": \"@S@\"}", "p": "/x/AutoOS"}',
    "json bare key": '{cmd: "/x/AutoOS/h.py", auth: "@S@"}',
    "header basic": "curl -H 'Authorization: Basic @S@' https://host/AutoOS",
    "header bearer": 'curl -H "Authorization: Bearer @S@" https://host/AutoOS',
    "query string": "https://host/AutoOS/cb?user=u&access_token=@S@&x=1",
    "upper case key": "AUTOOS_PG_PASSWORD=@S@",
    "lower case key": "autoos_pg_password=@S@",
    "yaml pair": "autoos_credentials: @S@",
    "conninfo": "host=h dbname=AutoOS user=u password=@S@",
    "url only": "ExecStart=/x/AutoOS/bin/run postgres://u:@S@@host/db",
    "url raw at": "ExecStart=/x/AutoOS/bin/run postgres://u:p@@S@@host/db",
}


def line(name: str) -> str:
    return LINES[name].replace(PLACE, SECRET)


@pytest.mark.parametrize("name", sorted(LINES))
def test_redact_masks_the_secret_in_every_line_shape(ar, name: str) -> None:
    out = ar.redact(line(name))
    assert_no_leak((SECRET,), out)
    assert "<redacted>" in out or "***" in out
    assert_masked(ar.redact(out), out, SECRET)  # masking twice changes nothing


@pytest.mark.parametrize("name", sorted(LINES))
def test_scan_reports_the_line_without_the_secret(ar, tmp_path: Path, name: str) -> None:
    home = tmp_path / "h"
    write(home / ".config" / "systemd" / "user" / "svc.service",
          "[Service]\n" + line(name) + "\n")
    hits = ar.scan_all(home)
    assert len(hits) == 1 and hits[0][0] == "systemd"
    assert_no_leak((SECRET,), hits[0][2])


def test_nested_dsn_keeps_the_names_and_masks_the_value(ar) -> None:
    assert_masked(ar.redact(line("systemd nested dsn")),
                  "Environment=AUTOOS_PG_DSN=<redacted>", SECRET)


def test_url_password_becomes_stars(ar) -> None:
    assert_masked(ar.redact(f"url = scheme://user:{SECRET}@host/x"),
                  "url = scheme://user:***@host/x", SECRET)
    assert_masked(ar.redact(f"redis://:{SECRET}@host:6379/0"),
                  "redis://:***@host:6379/0", SECRET)
    assert ar.redact("scheme://user@host:22/x") == "scheme://user@host:22/x"
    assert ar.redact("http://host:8080/x?mail=a@b") == "http://host:8080/x?mail=a@b"


def test_cli_prints_no_secret_for_any_line_shape(tmp_path: Path) -> None:
    home = tmp_path / "h"
    write(home / ".config" / "systemd" / "user" / "svc.service",
          "[Service]\n" + "\n".join(line(n) for n in sorted(LINES)) + "\n")
    write(home / ".claude" / ".mcp.json",
          '{"mcpServers": {"x": {"command": "/x/AutoOS/srv", "env": '
          '{"OMNIGRAPH_TOKEN": "' + SECRET + '", "PG_TOKEN_FILE": "/p"}}}}\n')
    proc = run_cli("--home", str(home))
    out, err, rc = proc.stdout, proc.stderr, proc.returncode
    assert_no_leak((SECRET,), out, err)
    assert rc == 1
    assert len(out.strip().splitlines()) == len(LINES) + 1


def test_text_that_is_not_a_secret_is_kept(ar) -> None:
    for text in (
        '{"command": "/home/x/code/AutoOS/h.py", "n": 1}',
        "Description=Test AutoOS Service",
        "ExecStart=/home/x/code/AutoOS/bin/daemon --verbose",
        "AutoOS tests passed: 41, author: someone, keyboard=us",
    ):
        assert ar.redact(text) == text


def test_redact_masks_before_it_truncates(ar) -> None:
    out = ar.redact("x" * 140 + " AUTOOS_API_TOKEN=" + SECRET + " tail")
    assert len(out) <= 160
    assert_no_leak((SECRET, SECRET[:4]), out)
