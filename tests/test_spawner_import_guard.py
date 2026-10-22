"""tests/test_spawner_import_guard.py"""

from __future__ import annotations

import ast
from pathlib import Path

SPAWNER_PREFIX = ("plangraph", "spawner")
ALLOWED_REL = Path("plangraph/planner/routing.py")
SPAWNER_DIR = Path("plangraph/spawner")

def _is_spawner(target: str) -> bool:
    """True when a fully qualified name lands in plangraph.spawner[.*]."""
    parts = target.split(".")
    return len(parts) >= 2 and tuple(parts[:2]) == SPAWNER_PREFIX

def _from_targets(node: ast.ImportFrom, pkg_parts: list[str]) -> list[str]:
    """Fully qualified modules an `ImportFrom` binds into this file's scope."""
    if node.level:
        up = node.level - 1
        if up > len(pkg_parts):
            return []  # relative import beyond the top-level package
        base = pkg_parts[: len(pkg_parts) - up]
        if node.module:
            return [".".join(base + node.module.split("."))]
        return [".".join(base + [a.name]) for a in node.names]
    if not node.module:
        return []
    if node.module == "plangraph":
        return ["plangraph." + a.name for a in node.names]
    return [node.module]

def find_spawner_importers(root: Path) -> list[str]:
    """Sorted relative posix paths of files importing plangraph.spawner.

    Skips the spawner package itself and the single allowed runtime file.
    """
    root = Path(root)
    hits: set[str] = set()
    for path in sorted(root.rglob("*.py")):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if rel == ALLOWED_REL:
            continue
        if rel.parent == SPAWNER_DIR or SPAWNER_DIR in rel.parents:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (SyntaxError, UnicodeDecodeError):
            continue
        pkg_parts = list(rel.parent.parts)
        rel_posix = rel.as_posix()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                if any(_is_spawner(a.name) for a in node.names):
                    hits.add(rel_posix)
            elif isinstance(node, ast.ImportFrom):
                if any(_is_spawner(t) for t in _from_targets(node, pkg_parts)):
                    hits.add(rel_posix)
    return sorted(hits)

def test_main_clean() -> None:
    src = Path(__file__).resolve().parents[1] / "src"
    assert src.is_dir(), f"source tree missing: {src}"
    anchor = src / ALLOWED_REL
    assert anchor.is_file(), f"expected runtime module missing: {anchor}"
    assert find_spawner_importers(src) == []

def test_fixture_red(tmp_path: Path) -> None:
    files = {
        "plangraph/__init__.py": "",
        "plangraph/keeper/__init__.py": "",
        "plangraph/keeper/x.py": "from plangraph.spawner.resolver import resolve\n",
        "plangraph/keeper/a.py": "from plangraph import spawner\n",
        "plangraph/keeper/b.py": "import plangraph.spawner.clients as c\n",
        "plangraph/foo/__init__.py": "",
        "plangraph/foo/y.py": "from ..spawner import clients\n",
        "plangraph/foo/z.py": "from . import spawner\nfrom .. import spawner\n",
        "plangraph/spawner/__init__.py": "",
        "plangraph/spawner/ok.py": "from plangraph.spawner.clients import make\n",
        "plangraph/planner/__init__.py": "",
        "plangraph/planner/routing.py": "from plangraph.spawner.resolver import resolve\n",
    }
    for rel, body in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    hits = find_spawner_importers(tmp_path)
    assert hits == sorted(hits), "guard must return a sorted list"
    assert hits == [
        "plangraph/foo/y.py",
        "plangraph/foo/z.py",
        "plangraph/keeper/a.py",
        "plangraph/keeper/b.py",
        "plangraph/keeper/x.py",
    ]

def test_flag_default_off(monkeypatch) -> None:
    monkeypatch.setenv("PLANGRAPH_SPAWNER_SELECTION", "1")
    from plangraph.planner import routing

    flag = routing.SPAWNER_SELECTION_ENABLED
    assert flag is False
    assert type(flag) is bool  # plain bool: not the str an env read would yield
    source = Path(routing.__file__).read_text(encoding="utf-8")
    assert "PLANGRAPH_SPAWNER_SELECTION" not in source
