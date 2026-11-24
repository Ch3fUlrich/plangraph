"""The reserve the replan runner keeps around a routing line fits the bus client's own wire size."""
import json
import re
from pathlib import Path
from types import SimpleNamespace

from plangraph.fleet_bus.client import BusClient
from plangraph.fleet_bus.contract import BODY_MAX

SOURCE = Path(__file__).resolve().parents[2] / "rust/pg-spawnd/src/round_replan.rs"


def _reserve():
    return int(re.search(r"const ENVELOPE_RESERVE: usize = (\d+);", SOURCE.read_text()).group(1))


def _wire(text):
    """The POST body the client sends for `text`, with every identifier at its length limit."""
    cfg = SimpleNamespace(host="h" * 64, key_id="k" * 64, key_file=Path("unused"))
    me = SimpleNamespace(cfg=cfg, clock=lambda: 9_999_999_999,
                         _nonce=lambda: "n" * 128, _secret=lambda: b"s" * 32)
    entry = {"msg_id": "m" * 32, "to": "t" * 64, "text": text}
    payload = BusClient._payload(me, entry)
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def test_the_reserve_covers_the_signed_envelope_at_its_identifier_limits():
    empty = len(_wire(""))
    assert empty <= _reserve()


def test_a_text_at_the_runner_budget_fits_the_bus_cap_and_del_costs_six_bytes():
    budget = BODY_MAX - _reserve()
    # DEL and non-ASCII characters are escaped to six bytes each in the wire JSON.
    text = "\x7f" * (budget // 6)
    assert len(json.dumps(text, ensure_ascii=True)) - 2 <= budget
    assert len(_wire(text)) <= BODY_MAX
    assert len(_wire("é" * (budget // 6))) <= BODY_MAX
