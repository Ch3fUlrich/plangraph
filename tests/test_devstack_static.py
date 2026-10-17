"""Static tests for the D1 local dev stack (deploy/dev/*, tools/devstack.sh).

No docker, no network — these tests only parse the files that define the
stack and check their structure, bindings, image pins and hygiene rules.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]

DEV_DIR = ROOT / "deploy" / "dev"
COMPOSE_PATH = DEV_DIR / "compose.yml"
PINS_PATH = DEV_DIR / "PINS"
DEV_ENV_EXAMPLE_PATH = DEV_DIR / "dev.env.example"
PG_INIT_DIR = DEV_DIR / "pg-init"
SCHEMA_SCRIPT_PATH = DEV_DIR / "temporal-schema.sh"
DEVSTACK_PATH = ROOT / "tools" / "devstack.sh"
GITIGNORE_PATH = ROOT / ".gitignore"

EXPECTED_SERVICES = {
    "pg",
    "temporal-schema",
    "temporal",
    "temporal-namespace",
    "temporal-ui",
}

EXPECTED_HOST_PORTS = {55432, 7233, 8233}

# Canonical pins copied from the server repo's Temporal stack
# (server/coding/temporal/docker-compose.yml, section x-image-pins,
# commit 8640037a). Same tags and digests.
EXPECTED_IMAGE_REFS = {
    "temporalio/server:1.32.0"
    "@sha256:c3e752127759616bb1615e0f9ba0e21635aeb5fdeb922de4f371c350955f46ae",
    "temporalio/admin-tools:1.32.0"
    "@sha256:a9f84fb9a374b2374fe2e67c8efc0468ff3f1c66c8a0b14597ec86e349e62bca",
    "temporalio/ui:2.56.0"
    "@sha256:66d55d383bfb334ee0781f21a3b36c865b3f0f2262491a070bb2143394e9fe88",
    "postgres:16",
}

DEV_ENV_KEYS = {
    "PG_USER",
    "PG_PASSWORD",
    "PG_PORT",
    "TEMPORAL_PORT",
    "TEMPORAL_UI_PORT",
    "KEEPER_PORT",
}

# Secret-like env var names in compose.yml must never be literal values.
SECRET_ENV_NAMES = {"PG_PASSWORD", "POSTGRES_PASSWORD", "POSTGRES_PWD"}

SUBCOMMANDS = ["init", "up", "down", "reset", "status", "logs", "keeper"]


def _require(path: Path, why: str) -> Path:
    assert path.is_file(), (
        f"{path.relative_to(ROOT)} is missing — D1-static implementation not "
        f"written yet (needed because {why})"
    )
    return path


def _read(path: Path) -> str:
    _require(path, "static dev-stack tests")
    return path.read_text(encoding="utf-8", errors="replace")


def _compose():
    text = _read(COMPOSE_PATH)
    data = yaml.safe_load(text)
    assert isinstance(data, dict), "compose.yml must parse to a mapping"
    return data


def _services():
    data = _compose()
    svcs = data.get("services")
    assert isinstance(svcs, dict), "compose.yml must define a 'services' mapping"
    return svcs


def _env_items(svc: dict):
    """Yield (name, value_or_None) for a service's environment.

    value is None when the variable is only *named* (dict with null value,
    or bare KEY in list form), meaning docker takes it from .env.
    """
    env = svc.get("environment") or {}
    if isinstance(env, dict):
        for k, v in env.items():
            yield str(k), (None if v is None else str(v))
    else:
        for item in env:
            raw = str(item)
            if "=" in raw:
                k, _, v = raw.partition("=")
                yield k.strip(), v
            else:
                yield raw.strip(), None


def _resolve_port(value) -> int:
    """Resolve a published host port, accepting ${VAR:-default} form."""
    s = str(value).strip().strip('"').strip("'")
    m = re.fullmatch(r"\$\{([A-Za-z_][A-Za-z0-9_]*):-?(\d+)\}", s)
    if m:
        return int(m.group(2))
    assert re.fullmatch(r"\d+", s), (
        f"published port {value!r} must be a literal number or "
        f"${{VAR:-default}} form"
    )
    return int(s)


# ${NAME:-default} and ${NAME-default} — the only interpolation forms
# allowed inside a port binding, because only they carry a checkable value.
_PORT_DEFAULT_RE = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*:?-([^{}]*)\}")


def _expand_binding(text: str, where: str) -> str:
    """Substitute ${NAME:-default} / ${NAME-default} with `default`.

    Must run BEFORE splitting a short-form binding on ':', since the
    default-value syntax itself contains a colon. Any remaining ${...}
    is a variable without a default and fails the check.
    """
    expanded = _PORT_DEFAULT_RE.sub(lambda m: m.group(1), text)
    if "${" in expanded:
        raise AssertionError(
            f"{where}: port binding {text!r} interpolates a variable "
            f"without a default; use ${{VAR:-default}}"
        )
    return expanded.strip().strip('"').strip("'")


def _parse_port_binding(name, entry):
    """Return (host_ip, host_port:int) for one ports entry.

    Accepts the short form 'IP:HOST:CONTAINER' (optionally with a
    trailing /protocol or :protocol suffix handled via split) and the
    long-form mapping with host_ip/published keys.
    """
    where = f"service {name!r}"
    if isinstance(entry, dict):
        published = entry.get("published")
        assert published is not None, (
            f"{where}: long-form port binding {entry!r} must set 'published'"
        )
        return entry.get("host_ip"), _resolve_port(published)
    if not isinstance(entry, str):
        raise AssertionError(
            f"{where}: ports entry {entry!r} must be a string or "
            f"long-form mapping"
        )
    parts = _expand_binding(entry, where).split(":")
    if len(parts) == 4 and parts[3] in ("tcp", "udp", "sctp"):
        parts = parts[:3]
    assert len(parts) == 3, (
        f"{where}: ports entry {entry!r} must be "
        f"'127.0.0.1:HOST:CONTAINER' (3 colon-separated parts after "
        f"expanding ${{VAR:-default}})"
    )
    return parts[0], _resolve_port(parts[1])


def _shell_function(text: str, name: str):
    """Return the source of the shell function `name() { ... }`.

    The function starts at its `name() {` line (column 0) and ends at the
    first closing brace at column 0. Returns None when not found.
    """
    m = re.search(rf"^{re.escape(name)}\(\)[ \t]*\{{", text, re.MULTILINE)
    if m is None:
        return None
    end_m = re.search(r"^\}", text[m.start():], re.MULTILINE)
    if end_m is None:
        return None
    return text[m.start():m.start() + end_m.end()]


def _case_branch_parts(text: str, label: str):
    """Return the contiguous text segments that make up a `case` branch.

    A segment list contains the branch text (from the label line up to the
    first ';;' or 'esac' after it) and, when the branch dispatches to a
    function named `cmd_<label>` (one-line labels like `up) cmd_up ;;`), the
    body of that shell function. Labels may appear on their own line
    ('reset)') or with the branch on the same line.
    Returns None when no such label is found.
    """
    label_re = re.compile(
        r"""^[ \t]*["']?([A-Za-z][A-Za-z0-9_-]*(?:\|[A-Za-z][A-Za-z0-9_-]*)*)"""
        r"""["']?[ \t]*\)""",
        re.MULTILINE,
    )
    branches = []
    for m in label_re.finditer(text):
        labels = m.group(1).split("|")
        start = m.end()
        end_m = re.search(r";;|[ \t]esac\b", text[start:])
        end = start + (end_m.end() if end_m else len(text) - start)
        branches.append((labels, text[start:end]))
    for labels, body in branches:
        if label in labels:
            parts = [body]
            func = _shell_function(text, f"cmd_{label}")
            if func is not None and f"cmd_{label}" in body:
                parts.append(func)
            return parts
    return None


def _case_branch(text: str, label: str):
    """Return the text of a `case` branch (see _case_branch_parts).

    When the branch dispatches to `cmd_<label>`, the returned text is the
    branch text followed by the body of that shell function. Returns None
    when no such label is found.
    """
    parts = _case_branch_parts(text, label)
    if parts is None:
        return None
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# 1. compose.yml parses and defines exactly the expected services
# ---------------------------------------------------------------------------


def test_compose_parses_and_defines_exactly_the_five_services():
    svcs = _services()
    assert set(svcs) == EXPECTED_SERVICES, (
        "services must be exactly "
        f"{sorted(EXPECTED_SERVICES)}, found {sorted(svcs)}"
    )


# ---------------------------------------------------------------------------
# 2. every published port is loopbound to 127.0.0.1, host ports exact
# ---------------------------------------------------------------------------


def test_all_published_ports_are_string_or_longform_loopback_bindings():
    for name, svc in _services().items():
        assert svc.get("network_mode") != "host", (
            f"service {name!r} must not use network_mode: host"
        )
        for entry in svc.get("ports") or []:
            host_ip, _ = _parse_port_binding(name, entry)
            assert host_ip == "127.0.0.1", (
                f"service {name!r}: ports entry {entry!r} must bind "
                f"host IP 127.0.0.1, got {host_ip!r}"
            )


def test_published_host_ports_are_exactly_55432_7233_8233():
    host_ports = set()
    for name, svc in _services().items():
        for entry in svc.get("ports") or []:
            _, host_port = _parse_port_binding(name, entry)
            host_ports.add(host_port)
    assert host_ports == EXPECTED_HOST_PORTS, (
        f"published host ports must be exactly {sorted(EXPECTED_HOST_PORTS)}, "
        f"got {sorted(host_ports)}"
    )


# The ONLY line in compose.yml that may carry a wildcard bind. The
# temporalio/server image needs BIND_ON_IP set to every container
# interface so its listener also covers the container's own loopback
# (the healthcheck `nc -z 127.0.0.1 7233` runs inside the container).
# This is in-container only: it must never appear in a ports: entry or
# anywhere else, which is why the allowance is this exact assignment
# line and nothing broader.
_ALLOWED_WILDCARD_LINE_RE = re.compile(
    r'^[ \t]*BIND_ON_IP:[ \t]*"?0\.0\.0\.0"?[ \t]*(?:#.*)?$'
)


def test_compose_raw_text_has_no_wildcard_bind():
    text = _read(COMPOSE_PATH)
    bad = [
        line
        for line in text.splitlines()
        if "0.0.0.0" in line and not _ALLOWED_WILDCARD_LINE_RE.match(line)
    ]
    assert not bad, (
        "compose.yml must not contain 0.0.0.0 outside the temporal "
        "BIND_ON_IP assignment (ports entries and any other use are "
        "rejected):\n" + "\n".join(bad)
    )


# ---------------------------------------------------------------------------
# 3. no secret literals
# ---------------------------------------------------------------------------


def test_no_literal_password_values_in_compose():
    for name, svc in _services().items():
        for var, value in _env_items(svc):
            if var.upper() in SECRET_ENV_NAMES or "PASSWORD" in var.upper():
                if value is None:
                    continue  # named only -> taken from .env
                assert "${" in value, (
                    f"service {name!r}: {var} must be a ${{VAR}} reference, "
                    f"not a literal ({value!r})"
                )
            # also guard PWD-style names (POSTGRES_PWD)
            if var.upper().endswith("_PWD"):
                if value is None:
                    continue
                assert "${" in value, (
                    f"service {name!r}: {var} must be a ${{VAR}} reference, "
                    f"not a literal ({value!r})"
                )


def test_dev_env_example_has_exact_keys_and_empty_password():
    text = _read(DEV_ENV_EXAMPLE_PATH)
    keys = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        keys[k.strip()] = v.strip()
    assert set(keys) == DEV_ENV_KEYS, (
        f"dev.env.example must list exactly {sorted(DEV_ENV_KEYS)}, "
        f"got {sorted(keys)}"
    )
    assert keys["PG_PASSWORD"] == "", (
        "dev.env.example: PG_PASSWORD must be empty"
    )


def _token_shape_hits(text: str):
    """Return labels of token-shaped strings found in text.

    Patterns are assembled at runtime so this test file never contains a
    matchable literal itself.
    """
    hits = []
    literals = {
        "gh p-token prefix": "gh" + "p_",
        "gh s-token prefix": "gh" + "s_",
        "github fine-grained token prefix": "github_" + "pat_",
        "private key marker": "BEGIN PRIV" + "ATE KEY",
    }
    for label, needle in literals.items():
        if needle in text:
            hits.append(label)
    # 32+ hex run outside a sha256: digest
    stripped = re.sub(r"sha256:[0-9a-fA-F]{64}", "", text)
    if re.search(r"(?<![0-9a-fA-F])[0-9a-fA-F]{32,}(?![0-9a-fA-F])", stripped):
        hits.append("32+ hex run outside a sha256: digest")
    return hits


def test_no_token_shaped_strings_in_dev_files_devstack_or_this_test():
    assert DEV_DIR.is_dir(), (
        "deploy/dev/ is missing — D1-static implementation not written yet"
    )
    targets = sorted(p for p in DEV_DIR.rglob("*") if p.is_file())
    targets.append(DEVSTACK_PATH)
    targets.append(Path(__file__))
    problems = []
    for path in targets:
        if not path.is_file():
            continue  # missing files are reported by other tests
        hits = _token_shape_hits(_read(path))
        if hits:
            problems.append(f"{path.relative_to(ROOT)}: {', '.join(hits)}")
    assert not problems, "token-shaped strings found:\n" + "\n".join(problems)


# ---------------------------------------------------------------------------
# 4. images match PINS
# ---------------------------------------------------------------------------


def _parse_pins():
    text = _read(PINS_PATH)
    pins = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        name, sep, ref = stripped.partition("=")
        assert sep, f"PINS line {line!r} must be 'name=image-reference'"
        pins[name.strip()] = ref.strip()
    return text, pins


def test_pins_file_is_plain_text_and_cites_server_source():
    text, pins = _parse_pins()
    assert pins, "PINS must contain at least one name=image-reference line"
    assert "x-image-pins" in text, (
        "PINS must cite the server-repo source: a line containing "
        "'x-image-pins' (section of server/coding/temporal/docker-compose.yml)"
    )


def test_compose_images_match_pins_exactly_both_directions():
    _, pins = _parse_pins()
    images = set()
    for name, svc in _services().items():
        img = svc.get("image")
        assert isinstance(img, str) and img, (
            f"service {name!r} must declare an image:"
        )
        images.add(img)
    pin_refs = set(pins.values())
    assert images == pin_refs, (
        "every image: in compose.yml must equal a PINS reference and every "
        f"PINS reference must be used.\ncompose: {sorted(images)}\n"
        f"PINS:    {sorted(pin_refs)}"
    )
    assert pin_refs == EXPECTED_IMAGE_REFS, (
        "PINS references must be the canonical images with the same tags and "
        f"digests as the cloud stack.\nexpected: {sorted(EXPECTED_IMAGE_REFS)}\n"
        f"got:      {sorted(pin_refs)}"
    )


def test_temporalio_image_refs_are_digest_pinned_with_version_tag():
    _, pins = _parse_pins()
    for name, ref in pins.items():
        if "temporalio/" not in ref:
            continue
        assert "@sha256:" in ref, (
            f"PINS {name!r}: temporalio reference must be digest-pinned "
            f"(@sha256:...), got {ref!r}"
        )
        assert re.search(r":\d[^@/]*@sha256:[0-9a-f]{64}$", ref), (
            f"PINS {name!r}: temporalio reference must carry a version tag "
            f"before @sha256:, got {ref!r}"
        )


# ---------------------------------------------------------------------------
# 5. one-shot services, entrypoints, depends_on, namespace
# ---------------------------------------------------------------------------


def test_one_shot_services_have_restart_no():
    svcs = _services()
    for name in ("temporal-schema", "temporal-namespace"):
        value = svcs[name].get("restart")
        assert value == "no", (
            f"service {name!r} must set restart: \"no\" (quoted string), "
            f"got {value!r}"
        )


def test_temporal_schema_entrypoint_uses_sh_not_bash():
    svc = _services()["temporal-schema"]
    entry = svc.get("entrypoint")
    if isinstance(entry, str):
        first = entry.split()[0] if entry.split() else ""
        joined = entry
    else:
        assert isinstance(entry, list) and entry, (
            "temporal-schema must define an entrypoint"
        )
        first = str(entry[0])
        joined = " ".join(str(e) for e in entry)
    assert first == "/bin/sh", (
        f"temporal-schema entrypoint must start with /bin/sh (the image has "
        f"no bash), got {first!r}"
    )
    assert "/schema/setup.sh" in joined, (
        "temporal-schema entrypoint must run /schema/setup.sh"
    )


def test_temporal_schema_mounted_script_exists_and_is_posix_sh():
    first_line = ""
    for chunk in (_services()["temporal-schema"].get("volumes") or []):
        if "temporal-schema.sh" in str(chunk):
            assert ":ro" in str(chunk) or "readonly" in str(chunk).lower(), (
                f"temporal-schema.sh mount must be read-only: {chunk!r}"
            )
    assert SCHEMA_SCRIPT_PATH.is_file(), (
        "deploy/dev/temporal-schema.sh must exist (mounted as "
        "/schema/setup.sh)"
    )
    first_line = SCHEMA_SCRIPT_PATH.read_text(
        encoding="utf-8", errors="replace"
    ).splitlines()[0]
    assert first_line.strip() == "#!/bin/sh", (
        f"temporal-schema.sh must start with #!/bin/sh (no bash), "
        f"got {first_line!r}"
    )


def test_depends_on_conditions():
    svcs = _services()

    def condition(svc_name, dep_name):
        dep = svcs[svc_name].get("depends_on")
        if isinstance(dep, dict) and dep_name in dep:
            entry = dep[dep_name]
            if isinstance(entry, dict):
                return entry.get("condition")
            return None
        return None

    assert condition("temporal", "temporal-schema") == (
        "service_completed_successfully"
    ), "temporal must depend_on temporal-schema with condition: service_completed_successfully"
    for svc_name in ("temporal-namespace", "temporal-ui"):
        assert condition(svc_name, "temporal") == "service_healthy", (
            f"{svc_name} must depend_on temporal with condition: "
            f"service_healthy"
        )


def test_namespace_command_creates_plangraph_dev():
    svc = _services()["temporal-namespace"]
    cmd = svc.get("command")
    assert cmd is not None, "temporal-namespace must define a command"
    if isinstance(cmd, list):
        text = " ".join(str(c) for c in cmd)
    else:
        text = str(cmd)
    assert "plangraph-dev" in text, (
        f"temporal-namespace command must create namespace plangraph-dev, "
        f"got {text!r}"
    )


# ---------------------------------------------------------------------------
# 6. pg-init creates the three databases
# ---------------------------------------------------------------------------


def test_pg_init_sql_creates_keeper_temporal_and_visibility_databases():
    assert PG_INIT_DIR.is_dir(), "deploy/dev/pg-init/ must exist"
    sql_files = sorted(PG_INIT_DIR.glob("*.sql"))
    assert sql_files, "deploy/dev/pg-init/ must contain at least one *.sql"
    text = "\n".join(
        f.read_text(encoding="utf-8", errors="replace") for f in sql_files
    )
    for dbname in ("keeper", "temporal", "temporal_visibility"):
        assert re.search(
            rf"CREATE\s+DATABASE\s+(?:IF\s+NOT\s+EXISTS\s+)?[\"'`]?{dbname}"
            rf"[\"'`]?\b",
            text,
            re.IGNORECASE,
        ), (
            f"pg-init SQL must contain CREATE DATABASE {dbname}"
        )


# ---------------------------------------------------------------------------
# 7. tools/devstack.sh
# ---------------------------------------------------------------------------


def test_devstack_script_exists_with_bash_shebang():
    text = _read(DEVSTACK_PATH)
    first_line = text.splitlines()[0].strip()
    assert re.fullmatch(r"#!/(usr/bin/env )?(usr/bin/)?bash\b.*", first_line), (
        f"tools/devstack.sh must start with a bash shebang, got {first_line!r}"
    )


def test_devstack_script_passes_bash_dash_n():
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("bash executable not found — skipping `bash -n` check")
    _require(DEVSTACK_PATH, "bash -n syntax check")
    proc = subprocess.run(
        [bash, "-n", str(DEVSTACK_PATH)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, (
        f"bash -n failed on tools/devstack.sh:\n{proc.stderr}"
    )


def test_devstack_handles_all_subcommand_case_labels():
    text = _read(DEVSTACK_PATH)
    for word in SUBCOMMANDS:
        assert _case_branch(text, word) is not None, (
            f"tools/devstack.sh must handle subcommand {word!r} "
            f"(a case label like '{word})' was not found)"
        )


def test_down_v_only_inside_reset_branch():
    text = _read(DEVSTACK_PATH)
    reset_branch = _case_branch(text, "reset")
    assert reset_branch is not None, (
        "tools/devstack.sh must have a reset case branch"
    )
    # everything that is NOT the reset branch/function must not contain `down -v`
    non_reset = text
    for part in _case_branch_parts(text, "reset"):
        non_reset = non_reset.replace(part, "\n" * part.count("\n"))
    bad = [
        line
        for line in non_reset.splitlines()
        if re.search(r"down\s+(?:-\S+\s+)*-v\b", line)
    ]
    assert not bad, (
        "`down -v` is only allowed in the reset branch; found in:\n"
        + "\n".join(bad)
    )


def test_up_branch_keeps_containers_of_a_failed_start():
    text = _read(DEVSTACK_PATH)
    up_branch = _case_branch(text, "up")
    assert up_branch is not None, "tools/devstack.sh must have an up case branch"
    for line in up_branch.splitlines():
        if re.search(r"\b(docker|compose)\b.*\b(down|rm|kill)\b", line):
            raise AssertionError(
                "the up branch must not tear containers down on failure "
                "(a failed up keeps its containers and logs); "
                f"offending line: {line.strip()!r}"
            )
    assert "--no-recreate" not in up_branch, (
        "the up branch must not use --no-recreate"
    )


# ---------------------------------------------------------------------------
# 8. .gitignore
# ---------------------------------------------------------------------------


def test_gitignore_lists_env_and_keeper_json():
    candidates = [GITIGNORE_PATH, DEV_DIR / ".gitignore"]
    lines = set()
    for p in candidates:
        if p.is_file():
            lines.update(
                l.strip() for l in p.read_text(encoding="utf-8").splitlines()
            )
    for entry in ("deploy/dev/.env", "deploy/dev/keeper.json"):
        assert entry in lines, (
            f".gitignore must list {entry!r} as a line"
        )


# ---------------------------------------------------------------------------
# 9. no host names / home paths / non-loopback IPv4 in the stack files
# ---------------------------------------------------------------------------


def _valid_ipv4(s: str) -> bool:
    return all(0 <= int(o) <= 255 for o in s.split("."))


def test_no_hostnames_home_paths_or_foreign_ips_in_stack_files():
    assert DEV_DIR.is_dir(), (
        "deploy/dev/ is missing — D1-static implementation not written yet"
    )
    files = sorted(p for p in DEV_DIR.rglob("*") if p.is_file())
    files.append(DEVSTACK_PATH)
    problems = []
    for path in files:
        if not path.is_file():
            continue
        text = _read(path)
        for m in re.finditer(r"(?<![\d.])\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}"
                             r"(?![\d.])", text):
            ip = m.group(0)
            if not _valid_ipv4(ip) or ip == "127.0.0.1":
                continue
            if ip == "0.0.0.0":
                # The temporal BIND_ON_IP assignment is the single allowed
                # wildcard (in-container listener scope, not a published
                # address); every other occurrence stays a problem.
                line_start = text.rfind("\n", 0, m.start()) + 1
                line_end = text.find("\n", m.start())
                line = text[line_start:] if line_end == -1 \
                    else text[line_start:line_end]
                if _ALLOWED_WILDCARD_LINE_RE.match(line):
                    continue
            problems.append(
                f"{path.relative_to(ROOT)}: IPv4 {ip} (only 127.0.0.1 "
                f"is allowed)"
            )
        if re.search(r"/home/", text):
            problems.append(f"{path.relative_to(ROOT)}: contains /home/ path")
        if re.search(r"[Cc]:[\\/][Uu]sers", text):
            problems.append(
                f"{path.relative_to(ROOT)}: contains a Windows users path"
            )
        for m in re.finditer(
            r"\b[a-z0-9][a-z0-9-]*(?:\.[a-z0-9][a-z0-9-]*)*"
            r"\.(?:com|net|org|dev|io|ai|app|cloud|local)\b",
            text,
        ):
            problems.append(
                f"{path.relative_to(ROOT)}: looks like a host name "
                f"({m.group(0)})"
            )
        for m in re.finditer(
            r"(?:https?|ftp|ssh|git)://(?!(?:127\.0\.0\.1|localhost)"
            r"(?::|/|\b))\S+",
            text,
        ):
            problems.append(
                f"{path.relative_to(ROOT)}: non-local URL {m.group(0)!r}"
            )
    assert not problems, (
        "stack files must be free of host names, home paths and non-loopback "
        "IPv4 addresses:\n" + "\n".join(problems)
    )


# ---------------------------------------------------------------------------
# 10. docker compose config validation (skipped when docker is unavailable)
# ---------------------------------------------------------------------------

COMPOSE_CONFIG_ENV = {
    "PG_USER": "dev",
    "PG_PASSWORD": "dev-not-a-secret",
    "PG_PORT": "55432",
    "TEMPORAL_PORT": "7233",
    "TEMPORAL_UI_PORT": "8233",
}


def _require_docker_compose():
    """Return the docker executable, or skip with a clear reason."""
    docker = shutil.which("docker")
    if not docker:
        pytest.skip(
            "docker executable not found on PATH — skipping "
            "`docker compose config` checks"
        )
    try:
        probe = subprocess.run(
            [docker, "compose", "version"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        pytest.skip(f"`docker compose version` could not run ({exc}) — skipping")
    if probe.returncode != 0:
        pytest.skip(
            "`docker compose version` failed — the compose plugin is not "
            f"available: {probe.stderr.strip() or probe.stdout.strip()}"
        )
    return docker


def _write_dev_env(tmp_path) -> Path:
    env_file = tmp_path / "dev.env"
    env_file.write_text(
        "".join(f"{k}={v}\n" for k, v in COMPOSE_CONFIG_ENV.items()),
        encoding="utf-8",
    )
    return env_file


def _compose_argv(docker, compose_path, env_file, *config_args):
    # `config` only — these tests must never run `up`.
    return [
        docker, "compose",
        "-p", "plangraph-dev",
        "-f", str(compose_path),
        "--env-file", str(env_file),
        *config_args,
    ]


def test_docker_compose_config_accepts_the_file(tmp_path):
    docker = _require_docker_compose()
    _require(COMPOSE_PATH, "docker compose config validation")
    env_file = _write_dev_env(tmp_path)
    try:
        proc = subprocess.run(
            _compose_argv(docker, COMPOSE_PATH, env_file, "config", "-q"),
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        pytest.fail(
            "`docker compose config -q` did not finish within 60 s "
            "(it must not contact registries or start containers)"
        )
    assert proc.returncode == 0, (
        f"`docker compose config -q` rejected {COMPOSE_PATH} "
        f"(rc={proc.returncode}):\n{proc.stderr}"
    )


def test_docker_compose_config_publishes_only_loopback_host_ips(tmp_path):
    docker = _require_docker_compose()
    _require(COMPOSE_PATH, "docker compose config rendering")
    env_file = _write_dev_env(tmp_path)
    try:
        proc = subprocess.run(
            _compose_argv(
                docker, COMPOSE_PATH, env_file, "config", "--format", "json"
            ),
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        pytest.fail("`docker compose config --format json` timed out after 60 s")
    assert proc.returncode == 0, (
        f"`docker compose config` failed (rc={proc.returncode}):\n{proc.stderr}"
    )
    rendered = json.loads(proc.stdout)
    published = [
        (name, port)
        for name, svc in (rendered.get("services") or {}).items()
        for port in svc.get("ports") or []
    ]
    assert published, (
        "docker compose config rendered no published ports — the loopback "
        "check would be vacuous"
    )
    for name, port in published:
        host_ip = port.get("host_ip") if isinstance(port, dict) else None
        assert host_ip == "127.0.0.1", (
            f"rendered service {name!r} publishes {port!r} without "
            f"host_ip: 127.0.0.1"
        )


# ---------------------------------------------------------------------------
# 11. temporal healthcheck / namespace readiness polling
# ---------------------------------------------------------------------------


def _temporal_healthcheck_test():
    hc = _services()["temporal"].get("healthcheck") or {}
    test = hc.get("test")
    assert test is not None, "temporal must define a healthcheck test"
    if isinstance(test, list):
        return " ".join(str(part) for part in test)
    return str(test)


def _namespace_command_text():
    cmd = _services()["temporal-namespace"].get("command")
    assert cmd is not None, "temporal-namespace must define a command"
    if isinstance(cmd, list):
        return "\n".join(str(c) for c in cmd)
    return str(cmd)


def test_temporal_healthcheck_uses_only_tools_present_in_the_server_image():
    text = _temporal_healthcheck_test()
    # temporalio/server ships neither the `temporal` nor the `tctl` CLI, so a
    # healthcheck that calls one always fails with "temporal: not found".
    for absent_cli in ("temporal ", "tctl "):
        assert absent_cli not in text, (
            f"temporal healthcheck must not invoke the {absent_cli.strip()!r} "
            f"CLI (the temporalio/server image does not ship it): {text!r}"
        )
    assert re.search(r"(^|[;&|]\s*|\s)nc(\s|$)", text), (
        f"temporal healthcheck must use a tool that exists in the image "
        f"(nc); got {text!r}"
    )


def test_temporal_namespace_waits_for_cluster_serving_before_creating():
    text = _namespace_command_text()
    assert "cluster health" in text, (
        "temporal-namespace must prove readiness with "
        f"`temporal operator cluster health`; got {text!r}"
    )
    assert "SERVING" in text, (
        "temporal-namespace must check for the SERVING state"
    )
    assert "sleep" in text, (
        "temporal-namespace must retry with a sleep between attempts"
    )
    assert re.search(r"\b(?:while|until|for|if)\b.*\b\d+\b", text, re.DOTALL), (
        "temporal-namespace must bound the wait (a numeric retry limit), "
        f"got {text!r}"
    )
    assert "plangraph-dev" in text, (
        "temporal-namespace must still create the plangraph-dev namespace"
    )


# ---------------------------------------------------------------------------
# 12. temporal BIND_ON_IP / in-container loopback address
# ---------------------------------------------------------------------------


def test_temporal_binds_on_all_container_interfaces_and_stays_loopback_published():
    # Without BIND_ON_IP the server image listens only on the container's
    # bridge IP, so the `nc -z 127.0.0.1 7233` healthcheck fails inside a
    # healthy container (verified live). Setting it must not loosen the
    # host-side publication: every ports entry stays bound to 127.0.0.1.
    svcs = _services()
    env = dict(_env_items(svcs["temporal"]))
    assert env.get("BIND_ON_IP") == "0.0.0.0", (
        "temporal must set BIND_ON_IP: \"0.0.0.0\" so the gRPC listener "
        "covers the container's own loopback for the healthcheck, got "
        f"{env.get('BIND_ON_IP')!r}"
    )
    assert env.get("TEMPORAL_ADDRESS") == "127.0.0.1:7233", (
        "temporal must set TEMPORAL_ADDRESS: \"127.0.0.1:7233\" so "
        "in-container clients target the loopback listener, got "
        f"{env.get('TEMPORAL_ADDRESS')!r}"
    )
    for name, svc in svcs.items():
        for entry in svc.get("ports") or []:
            host_ip, _ = _parse_port_binding(name, entry)
            assert host_ip == "127.0.0.1", (
                f"service {name!r}: BIND_ON_IP is in-container only; "
                f"ports entry {entry!r} must still publish on host IP "
                f"127.0.0.1, got {host_ip!r}"
            )
