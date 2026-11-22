"""tests/test_spawner_import_guard.py"""

from __future__ import annotations

import ast
import re
import pytest
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
        "plangraph/stall_watchdog.py": "from plangraph.spawner.stall_watchdog import main\n",
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
        "plangraph/stall_watchdog.py",
    ]

def test_selection_has_no_feature_flag(monkeypatch) -> None:
    monkeypatch.setenv("PLANGRAPH_SPAWNER_SELECTION", "1")
    from plangraph.planner import routing

    assert not hasattr(routing, 'SPAWNER_SELECTION_ENABLED')
    assert 'SPAWNER_SELECTION_ENABLED' not in routing.__all__
    source = Path(routing.__file__).read_text(encoding="utf-8")
    assert "PLANGRAPH_SPAWNER_SELECTION" not in source


def _rust_strings(source: str) -> list[tuple[str, int]]:
    """Extract Rust strings without confusing chars, lifetimes or nested comments."""
    strings = []
    index = 0
    escape = re.compile(r"\\(?:\r?\n\s*|x([0-9a-fA-F]{2})|u\{([0-9a-fA-F_]+)\}|(.))", re.S)
    raw_string = re.compile(r'r(\#*)"')
    character = re.compile(r"'(?:\\(?:x[0-9a-fA-F]{2}|u\{[0-9a-fA-F_]+\}|.)|[^'\\\n])'")
    ordinary = re.compile(r'"(?:\\.|[^"\\])*"', re.S)
    escapes = {'n': '\n', 'r': '\r', 't': '\t', '0': '\0', "'": "'", '"': '"', '\\': '\\'}

    def unescape(match):
        if match[1]:
            return chr(int(match[1], 16))
        if match[2]:
            return chr(int(match[2].replace('_', ''), 16))
        return escapes[match[3]] if match[3] else ''

    while index < len(source):
        if source.startswith('//', index):
            end = source.find('\n', index)
            index = len(source) if end < 0 else end + 1
        elif source.startswith('/*', index):
            depth, index = 1, index + 2
            while depth and index < len(source):
                if source.startswith('/*', index):
                    depth, index = depth + 1, index + 2
                elif source.startswith('*/', index):
                    depth, index = depth - 1, index + 2
                else:
                    index += 1
        elif raw := raw_string.match(source, index):
            begin = raw.end()
            end = source.find('"' + raw[1], begin)
            assert end >= 0, 'unterminated raw Rust string'
            strings.append((source[begin:end], index))
            index = end + 1 + len(raw[1])
        elif char := character.match(source, index):
            index = char.end()
        elif source[index] == '"':
            literal = ordinary.match(source, index)
            assert literal is not None, 'unterminated Rust string'
            strings.append((escape.sub(unescape, literal[0][1:-1]), index))
            index = literal.end()
        else:
            index += 1
    return strings


def rust_python_test_imports(root: Path) -> list[str]:
    """Conservatively check Python snippets in Rust files launching python -c.

    Checking every snippet in such a file also covers scripts passed through
    local variables and helpers, without trying to resolve Rust data flow.
    """
    forbidden = {p.stem for p in (root / 'tests').rglob('*.py')} | {'pytest', 'tests'}
    hits = []
    for path in sorted((root / 'rust').rglob('*.rs')):
        if 'target' in path.relative_to(root / 'rust').parts:
            continue
        source = path.read_text(encoding='utf-8')
        strings = _rust_strings(source)
        values = {value for value, _ in strings}
        launches_python = ('test_python::command' in source
                           or re.search(r"(?:\.|Sandbox::)python_command\s*\(", source) is not None
                           or any(Path(value).name in {'python3', 'python'} for value in values))
        if '-c' not in values or not launches_python:
            continue
        for body, offset in strings:
            try:
                tree = ast.parse(body)
            except (SyntaxError, ValueError):
                continue
            for node in ast.walk(tree):
                names = ([alias.name for alias in node.names] if isinstance(node, ast.Import)
                         else [node.module or ''] if isinstance(node, ast.ImportFrom) else [])
                if any(name.split('.')[0] in forbidden for name in names):
                    line = source.count('\n', 0, offset) + 1
                    hits.append(f'{path.relative_to(root).as_posix()}:{line}: '
                                'python -c imports a pytest/test dependency')
                    break
    return hits


def test_rust_python_has_no_test_dependencies() -> None:
    # Local fast-checks always run this file; CI selects it for every Rust change.
    assert rust_python_test_imports(Path(__file__).resolve().parents[1]) == []


def test_rust_python_dependency_guard_red(tmp_path: Path) -> None:
    tests = tmp_path / 'tests'
    tests.mkdir()
    (tests / 'fixture_helper.py').touch()
    rust = tmp_path / 'rust' / 'crate' / 'tests'
    rust.mkdir(parents=True)
    for body in ('import pytest', 'from pytest import raises', 'import tests.helper',
                 'from tests import helper', 'import fixture_helper',
                 'def local():\n    import pytest'):
        path = rust / 'bad.rs'
        path.write_text('fn test() {\nCommand::new("python3")\n'
                        f'.args(["-c", r#"{body}"#]).output();\n}}')
        assert rust_python_test_imports(tmp_path) == [
            'rust/crate/tests/bad.rs:3: python -c imports a pytest/test dependency']
    path.write_text('Command::new("python3").arg("-c").arg("import sys; print(sys.version)");')
    assert rust_python_test_imports(tmp_path) == []
    path.write_text('Command::new("python3").arg("-c").arg("import pytest");')
    assert len(rust_python_test_imports(tmp_path)) == 1
    path.write_text('// Command::new("python3").args(["-c", "import pytest"]);')
    assert rust_python_test_imports(tmp_path) == []
    path.write_text('test_python::command().args(["-c", "import pytest"]);')
    assert len(rust_python_test_imports(tmp_path)) == 1


@pytest.mark.parametrize('source', [
    '''const RECYCLE: &str = r#"import pytest"#;
       fn recycle(s: &Sandbox) { s.python_command().args(["-c", RECYCLE]); }''',
    '''fn bridge(script: &str) { Command::new("python3").args(["-c", script]); }
       fn test_bad() { bridge(r#"import pytest"#); }''',
    '''let script = "import pytest"; Command::new("python3").args(["-c", script]);''',
    '''fn helper(s: &str) -> bool { s.contains('"') }
       fn test_bad() { Command::new("python3").args(["-c", "import pytest"]); }''',
    '''Command::new("python3").args(["-c", "
import pytest
"]);''',
    r'''Command::new("python3").args(["-c", "import \x70ytest"]);''',
    r'''Command::new("python3").args(["-c", "import \u{70}ytest"]);''',
    r'''Command::new("python3").args(["-c", "import pytest; print(\'ok\')"]);''',
    'Command::new("python3").args(["-c", "import py\\\n  test"]);',
    '''/* outer /* nested */ '"' */
       Command::new("python3").args(["-c", "import pytest"]);''',
])
def test_rust_python_guard_covers_helpers_and_rust_literals(tmp_path, source):
    path = tmp_path / 'rust/crate/src/lib.rs'
    path.parent.mkdir(parents=True)
    path.write_text(source)
    hits = rust_python_test_imports(tmp_path)
    assert len(hits) == 1
    assert hits[0].startswith('rust/crate/src/lib.rs:')


@pytest.mark.parametrize(('relative', 'bare', 'clean'), [
    ('rust/crate/tests/fixture.rs',
     'std::process::Command::new("python").args(["-c", "pass"]);',
     'test_python::command().args(["-c", "pass"]);'),
    ('rust/crate/src/fixture_tests.rs',
     'Command::new(if cfg!(windows) { "python" } else { "python3" });',
     'test_python::command();'),
    ('rust/crate/tests/fixture.rs',
     'Command::new("/usr/bin/timeout").args(["30s", "/usr/bin/python3"]);',
     'Command::new("/usr/bin/timeout").arg("30s").arg(test_python::command().get_program());'),
    ('rust/crate/tests/fixture.rs',
     'symlink("/usr/bin/python3", fixture.join("python3"));',
     'symlink(test_python::command().get_program(), fixture.join("python3"));'),
    ('tests/test_fixture.py',
     'from subprocess import run as invoke\ninvoke(args=["python3", "-c", "pass"])',
     'python, env = python_test_env()\nsubprocess.run([python, "-c", "pass"], env=env)'),
])
def test_bare_python_subprocess_guard(tmp_path, relative, bare, clean):
    path = tmp_path / relative
    path.parent.mkdir(parents=True)
    path.write_text(bare, encoding='utf-8')
    assert len(bare_python_subprocesses(tmp_path)) == 1
    assert bare_python_subprocesses(tmp_path)[0].startswith(f'{relative}:')
    path.write_text(clean, encoding='utf-8')
    assert bare_python_subprocesses(tmp_path) == []


def test_python_helper_replaces_inherited_python_settings(monkeypatch, tmp_path):
    import subprocess
    from _split_ci_environment import python_test_env

    monkeypatch.setenv('PYTHONPATH', str(tmp_path / 'foreign-src'))
    monkeypatch.setenv('VIRTUAL_ENV', str(tmp_path / 'foreign-venv'))
    python, env = python_test_env()
    root = Path(__file__).resolve().parents[1]
    out = subprocess.check_output(
        [python, '-c', 'from plangraph.review.family import model_family; '
         'print(model_family("gpt-6.2-sol"))'], cwd=tmp_path, env=env, text=True)
    assert out.strip() == 'openai'
    assert env['PYTHONPATH'] == str(root / 'src')
    assert env.get('VIRTUAL_ENV') in (None, str(root / '.venv'))


_PYTHON_NAMES = {'python', 'python3', 'python.exe', 'python3.exe'}
_SUBPROCESS_APIS = {'run', 'Popen', 'call', 'check_call', 'check_output'}


def _python_subprocess_lines(source):
    tree = ast.parse(source)
    modules = {'subprocess'}
    calls = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.asname or alias.name for alias in node.names
                           if alias.name == 'subprocess')
        if isinstance(node, ast.ImportFrom) and node.module == 'subprocess':
            calls.update(alias.asname or alias.name for alias in node.names
                         if alias.name in _SUBPROCESS_APIS)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not ((isinstance(func, ast.Name) and func.id in calls)
                or (isinstance(func, ast.Attribute) and func.attr in _SUBPROCESS_APIS
                    and isinstance(func.value, ast.Name) and func.value.id in modules)):
            continue
        argv = node.args[0] if node.args else next(
            (kw.value for kw in node.keywords if kw.arg == 'args'), None)
        first = argv.elts[0] if isinstance(argv, (ast.List, ast.Tuple)) and argv.elts else argv
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            words = first.value.split()
            if words and Path(words[0]).name in _PYTHON_NAMES:
                yield node.lineno


_RUST_COMMAND_PREFIX = re.compile(
    r'\bCommand\s*::\s*new\s*\(\s*(?:if\s+cfg!\s*\([^)]*\)\s*\{\s*)?'
    r'(?:r\#*)?$')


def _rust_subprocess_lines(source):
    offsets = set()
    for value, offset in _rust_strings(source):
        path = Path(value)
        if path.name not in _PYTHON_NAMES:
            continue
        if match := _RUST_COMMAND_PREFIX.search(source, 0, offset):
            offsets.add(match.start())
        elif path.parent.as_posix() in {"/bin", "/usr/bin", "/usr/local/bin"}:
            offsets.add(offset)
    return sorted(source.count('\n', 0, offset) + 1 for offset in offsets)


def bare_python_subprocesses(root):
    """Name literal Python subprocess launches in test sources."""
    hits = []
    for path in sorted((root / 'tests').rglob('*.py')):
        hits.extend(f'{path.relative_to(root).as_posix()}:{line}: use python_test_env'
                    for line in _python_subprocess_lines(path.read_text(encoding='utf-8')))
    for path in sorted((root / 'rust').rglob('*.rs')):
        parts = path.relative_to(root / 'rust').parts
        if 'target' in parts or parts[:1] == ('test_support',):
            continue
        source = path.read_text(encoding='utf-8')
        if ('tests' not in parts and not path.stem.endswith(('_test', '_tests'))
                and not re.search(r'#\[\s*test\s*\]', source)):
            continue
        relative = path.relative_to(root).as_posix()
        for line in _rust_subprocess_lines(source):
            # This production adapter selects its caller's runtime interpreter.
            if (relative == 'rust/plangraph-cli/src/feeder/adapters.rs'
                    and source.splitlines()[line - 1] == '    let mut cmd = Command::new("python3");'):
                continue
            hits.append(f'{relative}:{line}: use test_python::command')
    return hits


def test_no_bare_python_subprocesses():
    assert bare_python_subprocesses(Path(__file__).resolve().parents[1]) == []
