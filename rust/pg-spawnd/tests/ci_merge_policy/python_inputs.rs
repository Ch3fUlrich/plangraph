//! Python inputs run by the spawnd Rust tests select Rust CI, and the scan stays inside the checkout.
//! A submodule of `ci_merge_policy` so its tests run under that Cargo target.
use super::python;

/// Collect a Python input only when it lies inside the checkout, by its name and by its
/// resolved target. A path such as `../../../tools/x.py` (an include_str! of a sibling
/// file) is a real file only in a worktree nested under another checkout, and a symlink can
/// lead out of the checkout; neither may change the result by location.
const MAKE_ADD: &str = r####"
import os, pathlib
def make_add(root, inputs):
    def add(path):
        try:
            rel = pathlib.Path(os.path.normpath(path)).relative_to(os.path.normpath(root))
            pathlib.Path(path).resolve().relative_to(pathlib.Path(root).resolve())
        except ValueError: return
        if path.is_file(): inputs.add(rel.as_posix())
    return add
"####;

#[test]
fn python_input_scan_ignores_paths_outside_the_checkout() {
    python(&format!(
        "{MAKE_ADD}{}",
        r####"
import tempfile
with tempfile.TemporaryDirectory() as tmp:
    base = pathlib.Path(tmp)
    (base / 'checkout/tools').mkdir(parents=True)
    (base / 'checkout/tools/in.py').write_text('')
    (base / 'tools').mkdir()
    (base / 'tools/out.py').write_text('')
    root = base / 'checkout'
    inputs = set()
    add = make_add(root, inputs)
    add(root / '../tools/out.py')
    add(root / 'tools/in.py')
    add(root / 'tools/../tools/in.py')
    add(root / 'tools/missing.py')
    try:
        os.symlink(base / 'tools/out.py', root / 'tools/escape.py')
        os.symlink(root / 'tools/in.py', root / 'tools/inside.py')
    except (OSError, NotImplementedError):
        pass
    else:
        add(root / 'tools/escape.py')
        add(root / 'tools/inside.py')
        assert 'tools/escape.py' not in inputs and 'tools/inside.py' in inputs, inputs
        inputs.discard('tools/inside.py')
    assert inputs == {'tools/in.py'}, inputs
"####
    ));
}

#[test]
fn python_inputs_executed_by_spawnd_tests_select_rust_ci() {
    python(&format!(
        "{MAKE_ADD}{}",
        r####"
import ast, pathlib, re, sys
from fnmatch import fnmatchcase
sys.path.insert(0, 'tools')
import ci_select as ci
root = pathlib.Path.cwd()
inputs = set()
add = make_add(root, inputs)
def module(name):
    for base in (root / 'src', root / 'tools'):
        path = base.joinpath(*name.split('.'))
        add(path.with_suffix('.py'))
        add(path / '__init__.py')
# Rust embeds Python with raw strings; inspect their AST rather than matching
# just literal .join paths, which cannot see imports or runpy.run_path calls.
for source in (root / 'rust/pg-spawnd').rglob('*.rs'):
    text = source.read_text(encoding="utf-8")
    for match in re.finditer(r'r(#+)"(.*?)"\1', text, re.S):
        if 'ast.parse(match[2])' in match[2]: continue  # exclude this scanner's own program
        try: tree = ast.parse(match[2])
        except SyntaxError: continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names: module(alias.name)
            elif isinstance(node, ast.ImportFrom) and node.module:
                module(node.module)
                for alias in node.names: module(node.module + '.' + alias.name)
            elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                  and node.func.attr == 'run_path' and node.args
                  and isinstance(node.args[0], ast.Constant)):
                add(root / node.args[0].value)
    # Direct script arguments and the native writer's tools/<gate> loop.
    for path in re.findall(r'"([^"\n]+\.py)"', text):
        add(root / path.lstrip('/'))
        add(root / 'tools' / path)
required = {'src/plangraph/spawner/overflow.py', 'src/plangraph/keeper/evidence_spawner.py',
            'src/plangraph/routing_support/leg_state.py', 'tools/hooks/pre-push',
            'tools/merge_batch_land.py', 'tools/ci_select.py', 'tools/check_file_size.py',
            'tools/check_new_code_lang.py'}
assert required <= inputs, required - inputs
missing = sorted(p for p in inputs if not any(fnmatchcase(p, g) for g in ci.RUST_INPUTS))
assert not missing, 'Unlisted Rust Python inputs: ' + repr(missing)
assert all(ci.select([p])['rust'] for p in inputs)
"####
    ));
}
