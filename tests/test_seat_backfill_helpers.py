"""Backfill extraction retains the original public import paths."""
import pytest


@pytest.mark.parametrize("module,name", [
    ("seat_record", "backfill"),
    ("seat_verdict", "apply_backfill"),
])
def test_backfill_helpers_are_reexported(module, name, tools_importable):
    from importlib import import_module

    helpers = import_module("seat_backfill")
    original = import_module(module)
    assert getattr(original, name) is getattr(helpers, name)
