"""D-2557: the CI pytest job installs gitleaks 8.30.1 from the pinned release, verified.

``tests/test_split_make_public.py::test_public_real_pem_in_other_file_is_reported`` (the F4
gate test) refuses to skip when the binary is absent, so CI must put ``gitleaks`` into PATH
or ``~/.local/bin`` before the pytest run starts.  These tests pin that install step's
contract: the pinned official release asset plus that release's published checksums file,
verified with ``sha256sum -c`` over the asset's own line only (never a hard-coded digest,
never curl | sh), the single binary extracted to ``~/.local/bin``, and ``gitleaks version``
printing 8.30.1 -- with no action, no secret and no pipe-to-shell anywhere in the step.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "tests.yml"
F4_SOURCE = ROOT / "tests" / "test_split_make_public.py"
F4_NAME = "test_public_real_pem_in_other_file_is_reported"

VERSION = "8.30.1"
ASSET = f"gitleaks_{VERSION}_linux_x64.tar.gz"
#: the asset name as the grep pattern needs it: the dots are literal, not wildcards
ESCAPED_ASSET = ASSET.replace(".", "\\.")
SUMS = f"gitleaks_{VERSION}_checksums.txt"
RELEASE_URL = f"https://github.com/gitleaks/gitleaks/releases/download/v{VERSION}"
#: The asset's digest as published on the release.  Verification reads the DOWNLOADED
#: checksums file, so this value must never be hard-coded into the workflow script.
RELEASED_ASSET_SHA256 = "551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb"


def workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def gitleaks_steps() -> list[tuple[str, int, dict]]:
    """(job, index, step) for every step whose name mentions gitleaks."""
    found: list[tuple[str, int, dict]] = []
    for name, job in workflow()["jobs"].items():
        for i, step in enumerate(job.get("steps", [])):
            if "gitleaks" in str(step.get("name", "")).lower():
                found.append((name, i, step))
    return found


def pytest_gitleaks_step() -> tuple[int, dict]:
    """The one gitleaks install step of the pytest job: (index, step)."""
    steps = workflow()["jobs"]["pytest"]["steps"]
    found = [
        (i, s) for i, s in enumerate(steps) if "gitleaks" in str(s.get("name", "")).lower()
    ]
    assert len(found) == 1, f"job pytest must hold exactly one gitleaks step, found {len(found)}"
    return found[0]


def test_install_step_runs_before_pytest_and_nowhere_else():
    """One step, in job pytest only, before the pytest run -- a plain run, no action."""
    wf = workflow()
    job_names = [job for job, _, _ in gitleaks_steps()]
    assert job_names == ["pytest"], f"gitleaks install must live only in job pytest: {job_names}"

    index, step = pytest_gitleaks_step()
    steps = wf["jobs"]["pytest"]["steps"]
    pytest_indices = [i for i, s in enumerate(steps) if s.get("name") == "pytest"]
    assert pytest_indices, "job pytest must still run pytest"
    assert index < pytest_indices[0], "gitleaks must be installed before the pytest run"
    assert "uses" not in step, "the install step must call no action, third-party or otherwise"
    assert step.get("run"), "the install step must be a shell run"


def test_step_downloads_the_pinned_release_asset_and_checksums_file():
    """Both files come from the pinned official release URL, fetched with curl -o."""
    script = pytest_gitleaks_step()[1]["run"]
    assert RELEASE_URL in script, "the release base URL must be pinned"
    assert RELEASE_URL + "/" + ASSET in script, "the exact release asset must be pinned"
    assert RELEASE_URL + "/" + SUMS in script, "the release checksums file must be pinned"
    assert re.search(r"curl\s+-fsSL\s+-o", script), "downloads must go to files with curl -fsSL"


def test_checksum_comes_from_the_downloaded_checksums_file_fail_closed():
    """sha256sum -c over the asset's line only; missing line or mismatch fails the step."""
    script = pytest_gitleaks_step()[1]["run"]
    assert "set -euo pipefail" in script, "the step must fail closed under set -euo pipefail"
    assert RELEASED_ASSET_SHA256 not in script, "the digest is read, never hard-coded"

    grep_lines = [ln for ln in script.splitlines() if ln.lstrip().startswith("grep")]
    assert grep_lines, "the asset's line must be grepped out of the checksums file"
    line = grep_lines[0]
    assert SUMS in line, "the line is selected from the downloaded checksums file"
    assert ESCAPED_ASSET in line, "grep must name the exact asset"
    assert ".tar\\.gz$\"" in line, "the grep must be anchored to that one file name"
    assert "|| true" not in line, "a missing line must fail, not be swallowed"
    assert "sha256sum -c" in script, "the selected line must be checked with sha256sum -c"


def test_single_binary_lands_in_local_bin_and_version_is_8_30_1():
    """The binary is extracted alone into ~/.local/bin, and the version check proves it."""
    script = pytest_gitleaks_step()[1]["run"]
    tar_lines = [ln for ln in script.splitlines() if "tar -xzf" in ln]
    assert len(tar_lines) == 1, "exactly one extraction step"
    assert tar_lines[0].rstrip().endswith(" gitleaks"), "only the gitleaks member is extracted"
    assert "~/.local/bin" in script, "the test looks in PATH or ~/.local/bin"
    assert "mkdir -p ~/.local/bin" in script, "~/.local/bin must exist before the install"
    version_lines = [ln for ln in script.splitlines() if "gitleaks version" in ln]
    assert len(version_lines) == 1, "the step must assert the installed version"
    assert re.search(r"\|\s*grep\s+-Fx\s+" + re.escape(VERSION), version_lines[0]), (
        "gitleaks version must print exactly 8.30.1"
    )


def test_step_has_no_pipe_to_shell_expression_or_credential():
    """No curl|sh, no workflow expression, no credential of any kind in the step."""
    script = pytest_gitleaks_step()[1]["run"]
    assert not re.search(r"\|\s*(sudo\s+)?(ba|z)?sh\b", script), "never pipe into a shell"
    assert "${{" not in script, "no workflow expression: no secrets, no github context"
    assert "token" not in script.lower(), "the step must not touch a credential"
    assert "http://" not in script, "downloads must be https"


def test_f4_gate_test_still_requires_the_real_binary():
    """The F4 test stays real: no skipif, no skip -- missing gitleaks is a failure."""
    source = F4_SOURCE.read_text(encoding="utf-8")
    tree = ast.parse(source)
    fn = next(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == F4_NAME
        ),
        None,
    )
    assert fn is not None, f"{F4_NAME} must not be removed"
    for decorator in fn.decorator_list:
        assert "skipif" not in ast.unparse(decorator), "the F4 test must never be skipped"
    body = ast.unparse(fn)
    assert "pytest.skip" not in body, "the F4 test must not skip at run time"
    assert ".exists()" in body, "the F4 test must still fail when the binary is missing"
