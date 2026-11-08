"""Offline guard for retrying workflow curl downloads (7f96931 already complies)."""
import shlex
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def run_blocks(node):
    if isinstance(node, yaml.MappingNode):
        for key, value in node.value:
            if key.value == "run" and isinstance(value, yaml.ScalarNode):
                yield value.value, value.start_mark.line + 1
            else:
                yield from run_blocks(value)
    elif isinstance(node, yaml.SequenceNode):
        for value in node.value:
            yield from run_blocks(value)


def downloads(text):
    for script, start in run_blocks(yaml.compose(text)):
        # Preserve physical line numbers while joining shell continuations.
        logical, first = "", start
        for offset, line in enumerate(script.splitlines(), 1):
            if not logical:
                first = start + offset
            logical += line
            if logical.endswith("\\"):
                logical = logical[:-1] + " "
                continue
            lexer = shlex.shlex(logical, posix=True, punctuation_chars=";&|<>()")
            lexer.whitespace_split = True
            tokens = list(lexer)  # shlex discards shell comments, not quoted '#'.
            command = []
            for token in tokens + [";"]:
                if token in {";", "&&", "||", "|", "&", "(", ")"}:
                    if command and command[0] == "curl":
                        output = any(
                            t in {"-o", "-O", "--output", "--remote-name"}
                            or t.startswith(("--output=", "-o"))
                            or (t.startswith('-') and not t.startswith('--')
                                and any(c in t[1:] for c in 'oO'))
                            for t in command[1:]
                        )
                        redirect = any(
                            t in {">", ">>"} and i + 1 < len(command)
                            and (i == 0 or command[i - 1] not in {"2", "0"})
                            and command[i + 1] != "/dev/null"
                            for i, t in enumerate(command)
                        )
                        if output or redirect:
                            yield first, command
                    command = []
                else:
                    command.append(token)
            logical = ""


def assert_retries(text, source):
    found = list(downloads(text))
    for line, command in found:
        counts = [
            command[i + 1] if i + 1 < len(command) else None
            for i, token in enumerate(command) if token == "--retry"
        ]
        counts += [t.partition("=")[2] for t in command if t.startswith("--retry=")]
        assert counts == ["5"] and (
            "--retry-all-errors" in command and "--no-retry-all-errors" not in command
        ), f"{source}:{line}: curl download requires --retry 5 --retry-all-errors"
    return len(found)


def test_repository_workflow_downloads():
    paths = sorted((ROOT / ".github/workflows").glob("*.yml"))
    paths += sorted((ROOT / ".github/workflows").glob("*.yaml"))
    # Release downloads have their own bounded retry/fallback policy below.
    total = sum(assert_retries(p.read_text(), p.relative_to(ROOT)) for p in paths
                if p.name != "tests.yml")
    assert total == 0


def test_temporal_workflow_cache_and_retry_window():
    workflow = yaml.safe_load((ROOT / ".github/workflows/tests.yml").read_text())
    sites = [
        job_name
        for job_name, job in workflow["jobs"].items()
        for step in job.get("steps", [])
        if step.get("uses") == "./.github/actions/setup-temporal"
    ]
    assert sorted(sites) == ["pytest", "pytest-postgres", "rust-worker"]
    # No inline Temporal download can bypass the shared cache condition.
    assert not any(form in (
        ROOT / ".github/workflows/tests.yml"
    ).read_text() for form in ("temporalio/cli/releases/download", "temporal.download/"))
    action = yaml.safe_load((ROOT / ".github/actions/setup-temporal/action.yml").read_text())
    cache, download, export = action["runs"]["steps"]
    assert cache["uses"] == "actions/cache@0400d5f644dc74513175e3cd8d07132dd4860809"
    assert cache["with"]["key"] == "temporal-cli-${{ runner.os }}-${{ runner.arch }}-1.9.1"
    assert cache["with"]["path"] == "~/.cache/temporal-cli/1.9.1/temporal"
    assert download["if"] == f"steps.{cache['id']}.outputs.cache-hit != 'true'"
    commands = list(downloads((ROOT / ".github/actions/setup-temporal/action.yml").read_text()))
    assert len(commands) == 2
    github, command = [command for _, command in commands]
    assert github[-1] == (
        "https://github.com/temporalio/cli/releases/download/v1.9.1/"
        "temporal_cli_1.9.1_linux_amd64.tar.gz"
    )
    assert github[github.index("--retry") + 1] == "3"
    assert "--retry-all-errors" in github
    # curl may finish one final transfer after the retry window expires.
    assert int(github[github.index("--retry-max-time") + 1]) + int(
        github[github.index("--max-time") + 1]
    ) <= 60
    assert command[-1] == (
        "https://temporal.download/cli/archive/v1.9.1?platform=linux&arch=amd64"
    )
    assert int(command[command.index("--retry-max-time") + 1]) >= 300
    assert int(command[command.index("--retry") + 1]) >= 20
    assert int(command[command.index("--retry-delay") + 1]) >= 15
    assert "--retry-all-errors" in command
    script = download["run"]
    assert "||" in script.split(github[-1], 1)[1].split("curl", 1)[0]
    for curl in (github, command):
        assert curl[curl.index("-o") + 1] == "$dir/temporal.tar.gz"
    assert "sha256sum -c -" in download["run"]
    assert script.index("sha256sum -c -") > script.index(command[-1])
    assert script.index("sha256sum -c -") < script.index("tar -xzf")
    assert "09a0326a51db84d02735e53542b9ebd8c4758daf47482a9ab0abce15844e60d5" in download["run"]
    assert "install -m 0755" in download["run"]
    assert "if" not in export  # Cache hits must also export the binary path.
    assert 'TEMPORAL_CLI=$HOME/.cache/temporal-cli/1.9.1/temporal' in export["run"]


TEMPORAL_SHA = "09a0326a51db84d02735e53542b9ebd8c4758daf47482a9ab0abce15844e60d5"
DOWNLOAD_FORMS = (
    "/releases/download/", "temporal.download/", "go install",
)


def assert_verified_downloads(document):
    """Inspect every workflow/action step, including CDN-only or Go-only additions."""
    found = 0
    if isinstance(document, dict):
        steps = document.get("steps", [])
        for index, step in enumerate(steps):
            script = step.get("run", "")
            if not any(form in script for form in DOWNLOAD_FORMS):
                continue
            found += 1
            caches = [s for s in steps[:index] if s.get("uses", "").startswith("actions/cache@")
                      and s.get("id")
                      and step.get("if") == f"steps.{s['id']}.outputs.cache-hit != 'true'"]
            assert len(caches) == 1, "download must be cache-gated"
            cache = caches[0]
            assert cache["uses"] == "actions/cache@0400d5f644dc74513175e3cd8d07132dd4860809"
            assert "${{ runner.os }}" in cache["with"]["key"]
            if "temporal" in script:
                assert "temporal-cli-" in cache["with"]["key"]
                assert "1.9.1" in cache["with"]["key"]
                check = f'echo "{TEMPORAL_SHA}  $dir/temporal.tar.gz" | sha256sum -c -'
                assert check in script, "pinned SHA-256 check required"
                assert script.index(check) < script.index("tar -xzf")
                assert script.index(check) > script.index("https://temporal.download/")
                assert "temporalio/cli/releases/download/" in script
                assert "||" in script
            else:
                assert "gitleaks-" in cache["with"]["key"]
                assert "8.30.1" in cache["with"]["key"]
                assert "sha256sum -c checks.sha256" in script
                assert "grep -E" in script and '> "$dir/checks.sha256"' in script
                assert script.index("sha256sum -c checks.sha256") < script.index("tar -xzf")
                assert "go install github.com/zricethezav/gitleaks/v8@v8.30.1" in script
                for setting in ("GOPROXY=https://proxy.golang.org", "GOSUMDB=sum.golang.org",
                                "GOPRIVATE= GONOSUMDB= GONOPROXY="):
                    assert setting in script, "Go checksum database must be enforced"
                assert "else" in script
            assert "install -m 0755" in script
        for value in document.values():
            found += assert_verified_downloads(value)
    elif isinstance(document, list):
        for value in document:
            found += assert_verified_downloads(value)
    return found


def test_every_temporal_download_step_has_fallback_and_verification():
    paths = list((ROOT / ".github/workflows").glob("*.y*ml"))
    paths += list((ROOT / ".github/actions").rglob("action.y*ml"))
    assert sum(assert_verified_downloads(yaml.safe_load(path.read_text()))
               for path in paths) == 2


@pytest.mark.parametrize("source", [
    "https://github.com/temporalio/cli/releases/download/v1.9.1/archive",
    "https://temporal.download/cli/archive/v1.9.1?platform=linux&arch=amd64",
    "go install github.com/zricethezav/gitleaks/v8@v8.30.1",
    "https://github.com/gitleaks/gitleaks/releases/download/v8.30.1/archive",
])
def test_unguarded_download_forms_are_rejected(source):
    with pytest.raises(AssertionError, match="cache-gated"):
        assert_verified_downloads(yaml.safe_load(workflow(source)))


def test_tar_listing_cannot_replace_temporal_checksum():
    text = (ROOT / ".github/actions/setup-temporal/action.yml").read_text()
    text = text.replace("sha256sum -c -", 'tar -tzf "$dir/temporal.tar.gz"')
    with pytest.raises(AssertionError, match="pinned SHA-256"):
        assert_verified_downloads(yaml.safe_load(text))


def workflow(command):
    return "jobs:\n  check:\n    steps:\n      - run: |\n" + "".join(
        "          " + line + "\n" for line in command.splitlines()
    )


@pytest.mark.parametrize("flags", [
    "", "--retry 5", "--retry-all-errors", "--retry 3 --retry-all-errors",
    "--retry 5 --retry 2 --retry-all-errors",
    "--retry=2 --retry 5 --retry-all-errors",
    "--retry 5 --retry 5 --retry-all-errors",
])
def test_multiline_missing_or_conflicting_retry(flags):
    text = workflow(f"curl {flags} \\\n  -o artifact \\\n  https://example.invalid/archive")
    with pytest.raises(AssertionError, match=r"synthetic:\d+: curl"):
        assert_retries(text, "synthetic")


def test_comments_and_non_downloads_are_ignored():
    text = workflow(
        '# curl -o artifact https://example.invalid/archive\n'
        'echo "curl -o artifact"\n'
        'curl --head https://example.invalid/ # -o artifact\n'
        'curl https://example.invalid/ 2> errors'
    )
    assert assert_retries(text, "synthetic") == 0


@pytest.mark.parametrize("output", ["-o artifact", "--output=artifact", "> artifact", "1>>artifact",
                                    "-fsSLo artifact", "-fsSLO"])
def test_output_downloads(output):
    command = f"curl https://example.invalid/archive {output}"
    with pytest.raises(AssertionError, match="curl download"):
        assert_retries(workflow(command), "synthetic")
    assert assert_retries(workflow(command + " --retry 5 --retry-all-errors"), "synthetic") == 1
