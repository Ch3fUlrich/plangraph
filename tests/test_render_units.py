"""Lint deploy/systemd/plangraph-render units + examples; see deploy/README.md "Live page: follow main and render".

Portable on purpose: pure configparser/regex over the shipped text, so it runs without systemd.
"""

import base64
import configparser
import hashlib
import http.client
import os
import subprocess
import re
import shlex
import shutil
import socket
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
UNITS = ROOT / "deploy" / "systemd"
SERVICE = UNITS / "plangraph-render.service"
TIMER = UNITS / "plangraph-render.timer"
DROPINS = [UNITS / "plangraph-render.service.d" / "kg.conf.example",
           UNITS / "plangraph-render.service.d" / "live-tree.conf.example"]
ENV_EXAMPLE = UNITS / "render.env.example"

NEW_FILES = [SERVICE, TIMER, *DROPINS, ENV_EXAMPLE,
             ROOT / "deploy" / "nginx" / "plan-page.conf.example",
             ROOT / "deploy" / "README.md", Path(__file__)]

INI_KEYS = ("Environment", "EnvironmentFile", "ExecStartPre", "ExecStart", "ExecStopPost")
SYSTEM_BINARIES = {"/bin/bash", "/usr/bin/python3"}
ALLOWED_IP = "127.0.0.1"

# Fragments are split so this file does not match its own hygiene scan.
IPV4 = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
HOME_PATH = re.compile(r"[/](?:home|Users)/")
WINDOWS_PATH = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/]")
LOCAL_HOST = re.compile(r"[A-Za-z0-9_-]\.local\b")
TOKEN_SECRET = re.compile(r"gh[a-z]{1,3}_" r"|github" r"_pat_")
TOKEN_ASSIGN = re.compile(r"(?i)\b\w*(?:token|secret|password|passwd)\w*\s*=\s*\S")
ENV_NAME = re.compile(r"PLANGRAPH_[A-Z0-9_]+")


def read(path):
    assert path.is_file(), f"missing file: {path}"
    return path.read_text(encoding="utf-8")


def parse(path):
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.optionxform = str
    parser.read_string(read(path), source=str(path))
    return parser


def values(parser):
    """Every (section, key, value) the hygiene checks care about, in file order."""
    for section in parser.sections():
        for key in INI_KEYS:
            if parser.has_option(section, key):
                yield section, key, parser.get(section, key)


def path_tokens(section, key, value):
    """Tokens that carry a location: systemd specifiers, environment references, absolute paths."""
    value = value.lstrip("-")
    if key == "Environment" and "=" in value:
        value = value.split("=", 1)[1]
    for token in shlex.split(value, posix=True):
        if token[:1] in ("/", "%", "$"):
            yield token


def test_service_shape():
    svc = parse(SERVICE)
    assert svc.get("Service", "Type", fallback="") == "oneshot", "service must be Type=oneshot"
    assert not svc.has_section("Install"), "service must not ship an [Install] section"
    lines = read(SERVICE).splitlines()
    assert sum(line.startswith("ExecStart=") for line in lines) == 1, "exactly one ExecStart (strict=False keeps the last)"
    assert not any(line.startswith("ExecStartPost") for line in lines), \
        "ExecStartPost is skipped when ExecStart fails: a oneshot check hook must be ExecStopPost"
    assert sum(line.startswith("ExecStopPost=") for line in lines) == 1, "exactly one ExecStopPost"
    assert svc.get("Service", "ExecStartPre", fallback="").startswith("-"), \
        "ExecStartPre must start with '-' so a follow failure cannot block the render"
    assert svc.get("Service", "ExecStopPost", fallback="").lstrip().startswith("-"), \
        "ExecStopPost must start with '-' so a stamp check failure cannot taint the unit"


def test_execstart_publishes_in_place():
    start = parse(SERVICE).get("Service", "ExecStart").replace(r'\"', '"')
    assert 'grep -q "</html>"' in start, "a truncated render must not replace a good page"
    assert 'cat "$T" >' in start, "publish with cat > so the destination inode survives the bind mount"
    assert "mv " not in start, "mv would swap the inode the web server bind-mounted"
    assert not any("mv " in line for line in read(SERVICE).splitlines()
                   if line.startswith("ExecStart") and not line.startswith("ExecStart=")), \
        "only ExecStart publishes the page; no other ExecStart-family line may mv"


def test_follow_hooks():
    svc = parse(SERVICE)
    for key, step in (("ExecStartPre", "follow"), ("ExecStopPost", "check")):
        hook = svc.get("Service", key, fallback="").lstrip("-")
        assert "plangraph.live_follow" in hook, f"{key} must run the live_follow module"
        assert step in shlex.split(hook, posix=True)[-1].split(), f"{key} must run live_follow {step}"


def test_timer_schedule():
    tim = parse(TIMER)
    assert tim.get("Timer", "OnBootSec", fallback="") == "2min", "first pass two minutes after boot"
    assert tim.get("Timer", "OnUnitActiveSec", fallback="") == "5min", "keep the existing 5 min cadence"
    assert tim.get("Timer", "AccuracySec", fallback="") == "15s", "coalesce wakeups within 15s"
    assert tim.get("Install", "WantedBy", fallback="") == "timers.target", "timer must be WantedBy timers.target"
    assert not tim.has_option("Timer", "OnCalendar"), "no wall-clock schedule: the tick is relative"
    assert not tim.has_option("Timer", "Persistent"), "a missed tick is worthless: the next one renders the same head"


def test_host_hygiene():
    for path in NEW_FILES:
        for number, line in enumerate(read(path).splitlines(), 1):
            where = f"{path.name}:{number}"
            assert not HOME_PATH.search(line), f"{where} hard-codes a home directory"
            assert not WINDOWS_PATH.search(line), f"{where} is a Windows path"
            assert not LOCAL_HOST.search(line), f"{where} names a .local host"
            assert not TOKEN_SECRET.search(line), f"{where} looks like a token literal"
            for ip in IPV4.findall(line):
                assert ip == ALLOWED_IP, f"{where} hard-codes {ip}"
            for match in TOKEN_ASSIGN.finditer(line):
                tail = line[match.end():]
                assert not re.match(r"[A-Za-z0-9_\-]{20,}", tail), f"{where} assigns a real-looking token"


def test_unit_paths_are_portable():
    for path in [SERVICE, TIMER, *DROPINS]:
        for section, key, value in values(parse(path)):
            for token in path_tokens(section, key, value):
                assert (token.startswith("%h") or token.startswith("$PLANGRAPH_") or token in SYSTEM_BINARIES), \
                    f"{path.name} {key} leaks {token}"


def test_all_listed_files_exist():
    for path in NEW_FILES:
        read(path)


def test_env_names_are_documented():
    documented = set(re.findall(r"^\s*#?\s*(PLANGRAPH_[A-Z0-9_]+)\s*=", read(ENV_EXAMPLE), re.M))
    for path in [SERVICE, TIMER, *DROPINS]:
        for section, key, value in values(parse(path)):
            for name in ENV_NAME.findall(value):
                assert name in documented, f"{path.name} uses {name}, undocumented in {ENV_EXAMPLE.name}"


def test_nginx_plan_requires_http_authentication():
    config = read(ROOT / "deploy" / "nginx" / "plan-page.conf.example")
    location = config.split("location = /plan {", 1)[1]
    assert re.search(r'^\s*auth_basic "(?!off")[^"]+";', location, re.M)
    assert re.search(r'^\s*auth_basic_user_file /[^;]+;', location, re.M)
    assert "satisfy all;" in location


def test_execstart_keeps_page_when_checkout_is_unverified(tmp_path):
    (tmp_path / "plan-cdn.html").write_text("<html>published</html>")
    # Stub only Python. The actual ExecStart must avoid invoking the renderer if the gate refuses.
    python = tmp_path / "python3"
    python.write_text("#!/bin/sh\ncase \"$*\" in *live_follow*) exit 1 ;; esac\nprintf '<html>unverified</html>' > \"$6\"\n")
    python.chmod(0o700)
    command = shlex.split(parse(SERVICE).get("Service", "ExecStart").replace(r'\"', '"'))
    env = dict(os.environ, PATH=f"{tmp_path}:{os.environ['PATH']}",
               PLANGRAPH_LIVE_TREE=str(tmp_path), PLANGRAPH_OUT=str(tmp_path))
    subprocess.run(command, env=env, capture_output=True)
    assert (tmp_path / "plan-cdn.html").read_text() == "<html>published</html>"


def test_live_tree_dropin_supplies_private_gate_state():
    assert "Environment=PLANGRAPH_STATE=%h/.local/state/" in read(DROPINS[1])


@pytest.mark.skipif(shutil.which("nginx") is None, reason="nginx required for HTTP fixture")
def test_plan_http_challenges_anonymous_requests(tmp_path):
    socket_path = tmp_path / "http.sock"
    page, auth = tmp_path / "page.html", tmp_path / "htpasswd"
    page.write_text("private fixture")
    credentials = b"fixture:fixture-password"
    digest = base64.b64encode(hashlib.sha1(credentials.split(b":", 1)[1]).digest()).decode()
    auth.write_text(f"fixture:{{SHA}}{digest}\n")
    conf = read(ROOT / "deploy" / "nginx" / "plan-page.conf.example")
    conf = conf.replace("/srv/plan/plan-cdn.html", str(page))
    conf = conf.replace("/etc/nginx/private/plan.users", str(auth))
    temp_dirs = " ".join(f"{name}_temp_path {tmp_path}/{name};"
                         for name in ("client_body", "proxy", "fastcgi", "uwsgi", "scgi"))
    (tmp_path / "nginx.conf").write_text(
        f"daemon off; pid {tmp_path}/nginx.pid; error_log stderr;\n"
        f"events {{}} http {{ {temp_dirs} access_log off; server {{ listen unix:{socket_path}; {conf} }} }}")
    nginx = subprocess.Popen(["nginx", "-e", "stderr", "-p", str(tmp_path), "-c", "nginx.conf"],
                             stderr=subprocess.PIPE)

    class Connection(http.client.HTTPConnection):
        def connect(self):
            self.sock = socket.socket(socket.AF_UNIX)
            self.sock.settimeout(3)
            self.sock.connect(str(socket_path))

    def fetch(headers):
        conn = Connection("fixture")
        try:
            conn.request("GET", "/plan", headers=headers)
            response = conn.getresponse()
            return response.status, response.headers, response.read()
        finally:
            conn.close()

    try:
        deadline = time.monotonic() + 3
        while not socket_path.exists() and nginx.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        if not socket_path.exists():
            nginx.terminate()
            _, stderr = nginx.communicate(timeout=5)
            pytest.fail(stderr.decode())
        status, headers, body = fetch({})
        assert status == 401 and "Basic" in headers["WWW-Authenticate"]
        assert b"private fixture" not in body
        status, headers, body = fetch({"Authorization": "Basic " + base64.b64encode(credentials).decode()})
        assert status == 200 and body == b"private fixture"
        assert headers["Cache-Control"] == "private, no-store"
    finally:
        nginx.terminate()
        nginx.communicate(timeout=5)
