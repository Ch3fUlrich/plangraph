"""resolve_alias: the snapshot-shaped alias following (Store.resolve_key's cap)."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from plangraph.keeper.cli_local import resolve_alias  # noqa: E402


def test_chain_is_followed_to_the_live_key():
    aliases = {"a:A": "b:B", "b:B": "c:C"}
    assert resolve_alias(aliases, "a:A") == "c:C"


def test_unknown_key_is_returned_unchanged():
    aliases = {"a:A": "b:B"}
    assert resolve_alias(aliases, "z:Z") == "z:Z"


def test_no_aliases_is_the_key_itself():
    assert resolve_alias(None, "a:A") == "a:A"
    assert resolve_alias({}, "a:A") == "a:A"
