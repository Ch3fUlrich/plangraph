"""Keep legacy execution and backend coverage wired into the contract suite."""
import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_claim_import_order():
    tree = ast.parse((ROOT / "tests/test_worker_claim.py").read_text())
    imports = [node.names[0].name for node in tree.body if isinstance(node, ast.Import)
               and node.names[0].name != "pytest"]
    assert imports == sorted(imports)


def test_raw_empty_red_first_coverage():
    source = (ROOT / "rust/worker/src/attempt.rs").read_text()
    assert 'assert_legacy_change_path("");' in source


def test_parity_is_not_skipped_without_postgres():
    tree = ast.parse((ROOT / "tests/test_worker_claim.py").read_text())
    test = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                and node.name == "test_claim_metadata_sqlite_postgres_parity")
    decorators = [ast.unparse(node) for node in test.decorator_list]
    assert not any("skip" in decorator for decorator in decorators)
    assert isinstance(test.body[0], ast.Assign)
    assert '"sqlite"' in ast.get_source_segment(
        (ROOT / "tests/test_worker_claim.py").read_text(), test.body[0])
