"""Lint deploy/systemd/plangraph-projector-reconcile units against the CLI and docs; see docs/projector.md Operations."""

import configparser
import re
import shlex
from pathlib import Path

from plangraph.projector import cli

ROOT = Path(__file__).resolve().parents[1]
UNITS = ROOT / "deploy" / "systemd"
SERVICE = UNITS / "plangraph-projector-reconcile.service"
TIMER = UNITS / "plangraph-projector-reconcile.timer"
IPV4 = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")


def read(path):
    assert path.is_file(), f"missing file: {path}"
    return path.read_text(encoding="utf-8")


def parse(path):
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.optionxform = str
    parser.read_string(read(path), source=str(path))
    return parser


def section(path, heading):
    lines = read(path).splitlines()
    start = next((i for i, line in enumerate(lines) if line.startswith(heading)), None)
    assert start is not None, f"{path.name} lacks a {heading!r} heading"
    stop = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    return "\n".join(lines[start + 1:stop])


def test_unit_structure():
    svc, tim = parse(SERVICE), parse(TIMER)
    assert svc.get("Service", "Type", fallback="") == "oneshot", "service must be Type=oneshot"
    assert not svc.has_section("Install"), "service must not ship an [Install] section"
    assert tim.get("Install", "WantedBy", fallback="") == "timers.target", "timer must be WantedBy timers.target"


def test_execstart_matches_cli():
    lines = read(SERVICE).splitlines()
    assert sum(line.startswith("ExecStart=") for line in lines) == 1, "exactly one ExecStart (strict=False keeps the last)"
    tokens = shlex.split(parse(SERVICE).get("Service", "ExecStart"), posix=True)
    assert tokens[0] == "/usr/bin/python3", tokens
    assert tokens[1:3] == ["-m", "plangraph.projector"], tokens
    assert "--history-dir" not in tokens, "unit must not pass --history-dir"
    ns = cli._build_parser().parse_args(tokens[3:])
    assert ns.command == "reconcile", ns
    assert ns.config == "%h/.local/share/plangraph/keeper.json", ns


def test_timer_schedule():
    tim, oncal = parse(TIMER), parse(TIMER).get("Timer", "OnCalendar", fallback="")
    assert tim.get("Timer", "Unit", fallback="") == SERVICE.name, "timer must activate the service unit"
    assert oncal and "03:30" in oncal, f"OnCalendar must be non-empty and nightly: {oncal!r}"
    assert tim.get("Timer", "Persistent", fallback="") == "true", "Persistent must be true"
    assert tim.has_option("Timer", "RandomizedDelaySec"), "RandomizedDelaySec must be set"


def test_no_machine_specific_paths():
    for path in (SERVICE, TIMER):
        cfg = parse(path)
        for number, line in enumerate(read(path).splitlines(), 1):
            where = f"{path.name}:{number}"
            assert "/home/" not in line and "/Users/" not in line, f"{where} hard-codes a home directory"
            for ip in IPV4.findall(line):
                assert ip == "127.0.0.1", f"{where} hard-codes {ip}"
            if not line.lstrip().startswith("#"):
                assert "cron" not in line.lower(), f"{where} mentions cron"
        for sec in cfg.sections():
            for key in ("ExecStart", "WorkingDirectory", "Environment", "EnvironmentFile"):
                if not cfg.has_option(sec, key):
                    continue
                value = cfg.get(sec, key).lstrip("-")
                if value.startswith("PYTHONPATH="):
                    value = value.split("=", 1)[1]
                for token in shlex.split(value, posix=True):
                    if token.startswith("/") and token != "/usr/bin/python3":
                        assert token.startswith("%h"), f"{path.name} {key} leaks {token}"


def test_docs_operations_section():
    body = section(ROOT / "docs" / "projector.md", "## Operations")
    assert TIMER.name in body, "Operations must document the timer unit"
    assert "deploy/systemd" in body, "Operations must reference deploy/systemd"


def test_systemd_readme():
    body = read(UNITS / "README.md")
    assert SERVICE.name in body, "README must mention the service unit"
    assert TIMER.name in body, "README must mention the timer unit"
    assert "enable --now" in body, "README must show enable --now"
    assert body.count("```") % 2 == 0, "README has an unclosed code fence"
