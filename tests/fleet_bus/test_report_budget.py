"""The replan runner shortens a routing line only when the bus client or the bus refuses it for size.

The runner (rust/pg-spawnd/src/round_replan_ports.rs) recognises that refusal by the words
"bus cap" and "too_large" in the sender's error; this proves the production client and the
bus validator really say so, for both the text cap and the whole-body cap.
"""
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from plangraph.fleet_bus.client import BusClient, Refused
from plangraph.fleet_bus.contract import BODY_MAX, ERROR_STATUS, TEXT_MAX

PORTS = Path(__file__).resolve().parents[2] / "rust/pg-spawnd/src/round_replan_ports.rs"


def _markers():
    return re.findall(r'message\.contains\("([a-z_ ]+)"\)', PORTS.read_text())


def test_the_runner_knows_exactly_the_size_refusal_words():
    assert _markers() == ["bus cap", "too_large"]


def test_the_client_refuses_an_over_long_text_with_the_bus_cap_words():
    with pytest.raises(Refused) as excinfo:
        BusClient._check_text("x" * (TEXT_MAX + 1))
    assert _markers()[0] in str(excinfo.value)


def test_the_client_refuses_an_over_large_signed_body_with_the_bus_cap_words():
    cfg = SimpleNamespace(host="h", key_id="k", key_file=Path("unused"))
    me = SimpleNamespace(cfg=cfg, clock=lambda: 9_999_999_999,
                         _nonce=lambda: "n", _secret=lambda: b"s" * 32)
    # 1800 non-ASCII characters are under the text cap and over the body cap in ASCII JSON.
    entry = {"msg_id": "m" * 32, "to": "t", "text": "é" * 1800}
    with pytest.raises(Refused) as excinfo:
        BusClient._payload(me, entry)
    assert _markers()[0] in str(excinfo.value)
    assert str(BODY_MAX) in str(excinfo.value)


def test_the_bus_names_its_own_size_refusal_too_large():
    assert "too_large" in ERROR_STATUS
    assert _markers()[1] == "too_large"
