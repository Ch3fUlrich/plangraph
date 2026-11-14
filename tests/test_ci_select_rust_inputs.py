"""Keep Rust's out-of-tree repository inputs in the CI selector (D-5294)."""
from __future__ import annotations

import importlib.util
from fnmatch import fnmatchcase
from pathlib import Path
import re

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("ci_select", ROOT / "tools/ci_select.py")
assert spec is not None and spec.loader is not None
ci_select = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ci_select)

# Scan literals rather than only test modules: production include_str! inputs
# are compiled into Rust tests too. Comments and scratch paths are not inputs.
RUST_STRINGS = re.compile(
    r'//[^\n]*|/\*.*?\*/|r(?P<hashes>#{0,16})"(?P<raw>.*?)"(?P=hashes)'
    r'|"(?P<plain>(?:\\.|[^"\\])*)"', re.DOTALL)


def rust_repo_inputs(root):
    """Resolve include literals, manifest-relative paths and repo-root joins.

    Parent-relative runtime literals are checked from both the source directory
    and Cargo's package working directory. Only existing repository files count,
    so temporary fixture names and path-traversal rejection samples are ignored.
    """
    inputs = {}
    for source in sorted((root / "rust").rglob("*.rs")):
        if "target" in source.relative_to(root).parts:
            continue
        text = source.read_text(encoding="utf-8")
        manifest = next(p for p in source.parents if (p / "Cargo.toml").is_file())
        manifest_relative = bool(re.search(r'env!\s*\(\s*"CARGO_MANIFEST_DIR"\s*\)', text))
        for match in RUST_STRINGS.finditer(text):
            value = match["raw"] if match["raw"] is not None else match["plain"]
            if value is None:
                continue  # comment
            before = text[:match.start()]
            include = re.search(r'include_(?:str|bytes)!\s*\(\s*$', before)
            bases = [source.parent] if include else []
            if manifest_relative and value.startswith("{}/"):
                value = value[3:]
                bases.append(manifest)
            if "../" in value:
                bases.extend((source.parent, manifest))
                if value.startswith("/../") and manifest_relative:
                    value = value.lstrip("/")
            if manifest_relative and re.search(r'\.join\(\s*$', before):
                bases.append(root)  # e.g. manifest.parent().parent().join(...)
            for base in bases:
                path = (base / value).resolve()
                if (path.is_file() and path.is_relative_to(root)
                        and not path.is_relative_to(root / "rust")
                        and not path.is_relative_to(root / ".git")):
                    relative = path.relative_to(root).as_posix()
                    inputs[relative] = f"{source.relative_to(root)}:{before.count(chr(10)) + 1}"
    return inputs


def assert_rust_inputs_selected(root):
    inputs = rust_repo_inputs(root)
    assert inputs, "Rust repository input scan found nothing"
    missing = [f"{location}: unlisted Rust input {path}"
               for path, location in inputs.items()
               if not any(fnmatchcase(path, pattern) for pattern in ci_select.RUST_INPUTS)]
    assert not missing, "\n".join(missing)
    assert all(ci_select.select([path])["rust"] for path in inputs)
    return inputs


@pytest.mark.parametrize("path", [
    "docs/schemas/examples/hosts.example.json", "docs/schemas/hosts.schema.json",
    "docs/schemas/new/nested/input.json", "./docs/schemas/workspace.schema.json",
    r"docs\schemas\examples\hosts.example.json",
])
def test_schema_changes_select_only_rust_and_rust_windows(path):
    selection = ci_select.select([path])
    assert selection == {suite: suite == "rust" for suite in ci_select.SUITES} | {"full": False}
    workflow = yaml.safe_load((ROOT / ".github/workflows/tests.yml").read_text())
    for job in ("rust", "rust-windows"):
        assert workflow["jobs"][job]["needs"] == "select"
        assert workflow["jobs"][job]["if"] == "needs.select.outputs.rust == 'true'"


def test_rust_repository_inputs_are_selected():
    inputs = assert_rust_inputs_selected(ROOT)
    # Witness all three forms in the live tree, not an empty/partial scan.
    assert {"docs/schemas/examples/hosts.example.json", "tests/fixtures/redact_parity.json",
            "tools/check_changelog_docs.py", "tools/probe_admission_policy_guard.py"} <= inputs.keys()


def test_schema_change_cli_outputs(tmp_path, monkeypatch, capsys):
    output = tmp_path / "github-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    assert ci_select.main(["select", "--files", "docs/schemas/examples/hosts.example.json"]) == 0
    expected = {suite: "true" if suite == "rust" else "false" for suite in ci_select.SUITES}
    expected["full"] = "false"
    assert dict(line.split("=") for line in output.read_text().splitlines()) == expected
    assert capsys.readouterr().out == output.read_text()


@pytest.mark.parametrize("read", [
    'include_str!("../../../docs/unlisted.json")',
    'include_bytes!(r#"../../../docs/unlisted.json"#)',
    'include_str!(concat!(env!("CARGO_MANIFEST_DIR"), "/../../docs/unlisted.json"))',
    'std::fs::read_to_string(Path::new(env!("CARGO_MANIFEST_DIR")).join("../../docs/unlisted.json"))',
    'std::fs::read("../../docs/unlisted.json")',
    'std::fs::read(format!("{}/../../docs/unlisted.json", env!("CARGO_MANIFEST_DIR")))',
    'include_str!(concat!(env! ( "CARGO_MANIFEST_DIR" ), "/../../docs/unlisted.json"))',
    'std::fs::read(Path::new(env!("CARGO_MANIFEST_DIR")).parent().unwrap().parent().unwrap().join("docs/unlisted.json"))',
])
@pytest.mark.parametrize("asset_path", ["docs/unlisted.json", "setup/unlisted.json"])
def test_unlisted_rust_repository_read_fails_check(tmp_path, read, asset_path):
    source = tmp_path / "rust/sample/src/lib.rs"
    source.parent.mkdir(parents=True)
    (source.parent.parent / "Cargo.toml").write_text('[package]\nname = "sample"\n')
    asset = tmp_path / asset_path
    asset.parent.mkdir()
    asset.write_text("{}")
    source.write_text(f"#[test]\nfn new_input() {{ let _ = {read.replace('docs/unlisted.json', asset_path)}; }}\n")
    with pytest.raises(AssertionError, match=f"lib.rs:2: unlisted Rust input {re.escape(asset_path)}"):
        assert_rust_inputs_selected(tmp_path)


@pytest.mark.parametrize("path, expected", [
    ("docs/guide.md", set()), ("docs/schemas-other/input.json", set()),
    ("docs/placement-rules.md", {"rust"}),
    ("tests/fixtures/redact_parity.json", {"pytest", "pytest-postgres", "rust"}),
    ("src/plangraph/web/agents_schema.json", {"pytest", "pytest-postgres", "rust"}),
    ("tools/check_changelog_docs.py", {"pytest", "pytest-postgres", "rust"}),
    ("tests/fixtures/unrelated.json", {"pytest", "pytest-postgres"}),
    ("setup/placement-rules.schema.toml", set(ci_select.SUITES) | {"full"}),
    ("deploy/systemd/plangraph-agents-view.service", set(ci_select.SUITES) | {"full"}),
])
def test_non_rust_inputs_preserve_other_job_selection(path, expected):
    assert {job for job, selected in ci_select.select([path]).items() if selected} == expected


@pytest.mark.parametrize("path, expected", [
    (".docs-map", {"rust"}),
    ("docs/rust-hooks.md", {"rust"}),
    ("tests/hooks/parity_payloads.json", {"pytest", "pytest-postgres", "rust"}),
    ("tools/new_code_allowlist.txt", {"pytest", "pytest-postgres", "rust"}),
])
def test_pg_hook_repository_inputs_are_explicitly_selected(path, expected):
    # policy.rs reads through repo_file(), beyond the literal-path scanner.
    assert (ROOT / path).is_file()
    assert path in ci_select.RUST_INPUTS
    assert {job for job, selected in ci_select.select([path]).items() if selected} == expected


def test_pg_hook_runs_in_windows_job():
    # Keep the regression name while enforcing the D-5579 Unix-only rollback.
    workflow = yaml.safe_load((ROOT / ".github/workflows/tests.yml").read_text())
    steps = workflow["jobs"]["rust-windows"]["steps"]
    assert all("pg-hook" not in step.get("run", "") for step in steps)
    assert "slice 1 is Unix-only" in (ROOT / "docs/rust-hooks.md").read_text()
    for source in (ROOT / "rust/pg-hook/tests").glob("*.rs"):
        assert source.read_text().startswith("#![cfg(unix)]\n"), source
    for name in ("lib.rs", "main.rs"):
        source = (ROOT / "rust/pg-hook/src" / name).read_text()
        assert "#[cfg(all(test, unix))]\nmod tests" in source


@pytest.mark.parametrize('metadata', ['.git', '.git/worktrees/lane/HEAD'])
def test_rust_input_scan_excludes_repository_metadata(tmp_path, metadata):
    source = tmp_path / 'rust/sample/src/lib.rs'
    source.parent.mkdir(parents=True)
    (source.parent.parent / 'Cargo.toml').write_text('[package]\nname = "sample"\n')
    entry = tmp_path / metadata
    entry.parent.mkdir(parents=True, exist_ok=True)
    entry.write_text('fixture metadata')
    source.write_text('fn metadata() { let _ = Path::new(env!("CARGO_MANIFEST_DIR"))'
                      f'.parent().unwrap().parent().unwrap().join("{metadata}"); }}')
    assert rust_repo_inputs(tmp_path) == {}
