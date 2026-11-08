import importlib.util
from pathlib import Path
import subprocess
import socket
import pytest

SPEC = importlib.util.spec_from_file_location(
    "dell_ci_run", Path(__file__).parents[1] / "tools/dell_ci_run.py")
ci = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ci)


def test_protoc_path_and_missing_reason(tmp_path):
    script = ci.service_script("rust-spike", str(tmp_path / "scratch"))
    assert "$HOME/.local/bin" not in script
    isolated = ci.isolated_script(':', '/source', '/job')
    assert 'protoc=$(command -v protoc || command -v "${HOME}/.local/bin/protoc")' in isolated
    assert '--ro-bind "$protoc" /toolchain/protoc-bin/protoc' in isolated
    script = script.replace("command -v protoc", "command -v nonexistent_protoc_test")
    result = subprocess.run(["sh", "-c", script], capture_output=True, text=True)
    assert result.returncode == 1
    assert "rust-spike: host fault: protoc missing" in result.stderr


def test_empty_output_names_command(tmp_path, monkeypatch):
    # Exercise the remote producer: even a silent failing cargo command must
    # identify itself before the service script exits and cleans up.
    monkeypatch.setitem(ci.service_script.__globals__["RUST"], "rust-spike",
                        "cargo test")
    script = ci.service_script("rust-spike", str(tmp_path / "scratch"))
    stubs = "protoc() { :; }; cargo() { return 7; }; "
    result = subprocess.run(["sh", "-c", stubs + script],
                            capture_output=True, text=True)
    assert result.returncode == 7
    assert result.stdout == ""
    assert result.stderr == "Running: cargo test\n"
    assert not (tmp_path / "scratch").exists()


@pytest.mark.parametrize("host", ["local", socket.gethostname()])
def test_local_runner(host):
    def runner(argv, **kwargs):
        assert argv == ["bash", "-c", "true"]
        assert kwargs["timeout"] == 17 and kwargs["capture_output"]
        return subprocess.CompletedProcess(argv, 0, "", "")
    assert ci.remote(host, "true", 17, runner).returncode == 0


def test_bounded_scrubbed_tail():
    output = "\n".join(str(n) for n in range(60))
    assert ci.output_tail(output).splitlines() == output.splitlines()[-40:]
    assert ci.output_tail(str(Path.home()) + "/cache") == "$HOME/cache"
    assert "secret" not in ci.output_tail("password=secret")
    assert len(ci.output_tail("x" * 3000)) == 2000


@pytest.mark.parametrize("prefix", ["Authorization: Bearer ", "Bearer ", "bearer "],
                         ids=["header", "standalone", "lowercase"])
def test_bearer_tail_redaction(prefix):
    value = "opaque" + "x" * 20
    assert ci.output_tail(prefix + value) == prefix + "[REDACTED]"


@pytest.mark.parametrize("prefix", ["s" + "k-", "gh" + "p_", "github" + "_pat_"])
def test_token_tail_redaction(prefix):
    value = prefix + "x" * 20
    assert ci.output_tail("credential=" + value + "\nfinished") == (
        "credential=[REDACTED]\nfinished")
    assert ci.output_tail(value.encode()) == "[REDACTED]"


def test_outer_paths_expand_once():
    script = ci.isolated_script(":", '"$HOME"/cache/source', '"$HOME"/cache/job')
    assert 'scratch="$HOME"/cache/job\n' in script
    assert 'cp -a "$HOME"/cache/source/.' in script
    assert 'init -q --initial-branch=dell-ci-private' in script
