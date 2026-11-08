"""Offline guard for retrying workflow curl downloads (7f96931 already complies)."""
from pathlib import Path
import shlex

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
    total = sum(assert_retries(p.read_text(), p.relative_to(ROOT)) for p in paths)
    assert total == 2  # Temporal downloads live in the shared composite action.


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
    assert "temporalio/cli/releases/download" not in (
        ROOT / ".github/workflows/tests.yml"
    ).read_text()
    action = yaml.safe_load((ROOT / ".github/actions/setup-temporal/action.yml").read_text())
    cache, download, export = action["runs"]["steps"]
    assert cache["uses"] == "actions/cache@0400d5f644dc74513175e3cd8d07132dd4860809"
    assert cache["with"]["key"] == "temporal-cli-${{ runner.os }}-${{ runner.arch }}-1.9.1"
    assert cache["with"]["path"] == "~/.cache/temporal-cli/1.9.1/temporal"
    assert download["if"] == f"steps.{cache['id']}.outputs.cache-hit != 'true'"
    commands = list(downloads((ROOT / ".github/actions/setup-temporal/action.yml").read_text()))
    assert len(commands) == 1
    command = commands[0][1]
    assert int(command[command.index("--retry-max-time") + 1]) >= 300
    assert int(command[command.index("--retry") + 1]) >= 20
    assert int(command[command.index("--retry-delay") + 1]) >= 15
    assert "--retry-all-errors" in command
    assert "sha256sum -c -" in download["run"]
    assert "09a0326a51db84d02735e53542b9ebd8c4758daf47482a9ab0abce15844e60d5" in download["run"]
    assert "install -m 0755" in download["run"]
    assert "if" not in export  # Cache hits must also export the binary path.
    assert 'TEMPORAL_CLI=$HOME/.cache/temporal-cli/1.9.1/temporal' in export["run"]


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
