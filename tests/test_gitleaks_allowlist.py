"""PG-REPUBLISH-FRESH: the PROMPT.md / orchestration-setup allowlist entries are value-scoped.

Proves that after the regexes+AND tightening only the reviewed synthetic prose strings
are waived in those two paths: a different key-shaped string (assembled at runtime, never
a literal in this file) planted in a copy of the same paths IS still reported.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / ".gitleaks.toml"

PROMPT_REL = Path("PROMPT.md")
PLAN_REL = Path("docs/plans/2026-10-16-orchestration-setup.md")
# assembled at runtime so this test file is not itself a generic-api-key finding
# (it sits in the same scanner gate as the paths it exercises)
def _join(*parts: str) -> str:
    return "".join(parts)

WAIVED_PROMPT_SECRET = _join("bool/str/", "negative")
WAIVED_PLAN_SECRET = _join("RT-P2-", "DECISIONS")
# the exact prose that the generic-api-key rule mis-fires on in the two
# historically allow-listed files
PROMPT_PROSE = _join("It accepts tokens, ", WAIVED_PROMPT_SECRET, " values.\n")
PLAN_PROSE = _join("Phases: Q-API-PUSH, ", WAIVED_PLAN_SECRET, " are done.\n")


def _gitleaks() -> str:
    import os

    env = os.environ.get("GITLEAKS_BIN")
    if env and Path(env).exists():
        return env
    found = shutil.which("gitleaks")
    if found:
        return found
    fallback = Path("C:/fleet/tools/gl/gitleaks.exe")
    if fallback.exists():
        return str(fallback)
    pytest.skip("gitleaks binary not installed (PATH, GITLEAKS_BIN, or C:/fleet/tools/gl)")


def _make_tree(tmp_path: Path) -> Path:
    tree = tmp_path / "tree"
    (tree / PLAN_REL.parent).mkdir(parents=True)
    (tree / PROMPT_REL).write_text(PROMPT_PROSE, encoding="utf-8")
    (tree / PLAN_REL).write_text(PLAN_PROSE, encoding="utf-8")
    return tree


def _scan(exe: str, tree: Path, config: Path | None) -> tuple[int, list[dict]]:
    report = tree.parent / "findings.json"
    if report.exists():
        report.unlink()
    cmd = [exe, "detect", "--no-git", "--source", ".", "--no-banner",
           "--report-format", "json", "--report-path", str(report)]
    if config is not None:
        cmd += ["--config", str(config)]
    proc = subprocess.run(cmd, cwd=tree, capture_output=True, text=True)
    findings = json.loads(report.read_text(encoding="utf-8")) if report.exists() else []
    return proc.returncode, findings


def _fake_secret(fragments: tuple[str, ...]) -> str:
    return "".join(fragments)


def test_known_synthetic_strings_are_waived(tmp_path):
    exe = _gitleaks()
    tree = _make_tree(tmp_path)
    rc, findings = _scan(exe, tree, CONFIG)
    assert rc == 0, f"known prose must stay waived, findings: {findings}"


def test_waived_strings_are_real_findings_without_the_config(tmp_path):
    # guard against the prose silently stopping matching generic-api-key (which would
    # make the waived-path test above pass vacuously)
    exe = _gitleaks()
    tree = _make_tree(tmp_path)
    rc, findings = _scan(exe, tree, None)
    secrets = {f["Secret"] for f in findings if f["RuleID"] == "generic-api-key"}
    assert rc == 1 and secrets == {WAIVED_PROMPT_SECRET, WAIVED_PLAN_SECRET}, secrets


@pytest.mark.parametrize("rel", [PROMPT_REL, PLAN_REL])
def test_other_key_shaped_string_in_same_path_is_reported(tmp_path, rel):
    exe = _gitleaks()
    tree = _make_tree(tmp_path)
    planted = _fake_secret(("Vk7", "qP2", "mR9x", "Tl4b", "Zr")) + rel.name[:2]
    with (tree / rel).open("a", encoding="utf-8") as fh:
        fh.write(f"api key = {planted}\n")
    rc, findings = _scan(exe, tree, CONFIG)
    secrets = {f["Secret"] for f in findings if f["RuleID"] == "generic-api-key"}
    assert rc == 1, "tightened allowlist must not waive a new key-shaped value"
    assert planted in secrets
    waived = {WAIVED_PROMPT_SECRET, WAIVED_PLAN_SECRET}
    assert not secrets & waived, f"known strings leaked into findings: {secrets & waived}"
