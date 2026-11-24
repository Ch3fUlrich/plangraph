"""The native launch caller holds no shared Cargo lease across the supervisor (LE-NATIVE-PUSH-CARGO-TRIM-HANDOFF)."""
import ast
import inspect
import textwrap

from plangraph.spawner import overflow


def test_native_spawn_takes_no_cargo_lease_so_the_supervisor_can_reclaim_before_its_push():
    tree = ast.parse(textwrap.dedent(inspect.getsource(overflow.native_spawn)))
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    names |= {alias.name for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) for alias in node.names}
    assert "cargo_lease" not in names
    assert "cargo_child_env" in names
