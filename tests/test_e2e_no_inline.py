"""tools/e2e_devstack.py: --no-inline-projection and the wait_for_projection helper.

Hermetic: the module is loaded by path exactly as tests/test_e2e_devstack_args.py does (it is a
standalone script, not an importable package) and only its pure helpers are exercised -- no
Temporal, keeper, network, subprocess or real sleeping. wait_for_projection with an injected
read/now/sleep proves the check-mode path is read-only: it only ever reads attempt rows the
deployed projector unit wrote.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "e2e_devstack.py"


@pytest.fixture(scope="module")
def e2e():
    """The script as a module, loaded by path (it is not on any import path)."""
    name = "e2e_devstack"          # same registration style as tests/test_e2e_devstack_args.py
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    assert spec is not None and spec.loader is not None, f"cannot load {SCRIPT}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module            # registered before exec: modules look themselves up by name
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """The arg parser falls back to these env vars; clear them so a case tests only its argv."""
    for var in ("TEMPORAL_ADDR", "KEEPER_URL", "PLANGRAPH_TEMPORAL_HTTP"):
        monkeypatch.delenv(var, raising=False)


class FakeClock:
    """Injected now/sleep: time only moves when the helper calls sleep()."""

    def __init__(self, base=1_700_000_000.0):
        self.base = base
        self.t = base
        self.sleeps = 0

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s
        self.sleeps += 1


def test_returns_on_first_read_with_n_at_least_want(e2e):
    """No sleeping when the first read already satisfies want_n."""
    clock = FakeClock()
    seen = []

    def read(key):
        seen.append(key)
        return [{"n": 1}]

    out = e2e.wait_for_projection(read, "card-a", 1, 75.0, now=clock.now, sleep=clock.sleep)
    assert seen == ["card-a"]
    assert clock.sleeps == 0
    assert out["card"] == "card-a"
    assert out["waited_s"] == 0.0

    # n >= want, not n == want: row 3 satisfies want 2.
    out = e2e.wait_for_projection(lambda k: [{"n": 1}, {"n": 3}], "card-a", 2, 75.0,
                                  now=clock.now, sleep=clock.sleep)
    assert out["card"] == "card-a"
    assert clock.sleeps == 0


def test_times_out_raising_fail_with_the_message(e2e):
    clock = FakeClock()

    def read(key):
        return []

    with pytest.raises(e2e.Fail) as exc:
        e2e.wait_for_projection(read, "card-b", 2, 3.0, now=clock.now, sleep=clock.sleep,
                                step_s=1.0)
    assert str(exc.value) == (
        "no attempt 2 for card-b within 3.0 s (is the projector unit running?)")
    assert clock.sleeps == 3


def test_waited_s_and_attempt_seen_at_shape(e2e):
    clock = FakeClock()
    calls = {"n": 0}

    def read(key):
        calls["n"] += 1
        return [] if calls["n"] < 3 else [{"n": 1}]

    out = e2e.wait_for_projection(read, "card-c", 1, 75.0, now=clock.now, sleep=clock.sleep,
                                  step_s=1.0)
    assert set(out) == {"card", "attempt_seen_at", "waited_s"}
    seen = out["attempt_seen_at"]
    assert (len(seen) == 20 and seen[4] == seen[7] == "-" and seen[10] == "T"
            and seen[13] == seen[16] == ":" and seen[19] == "Z")
    assert isinstance(out["waited_s"], float)
    assert out["waited_s"] == 2.0       # two 1 s sleeps, measured from the call start


def test_parse_args_defaults(e2e):
    args = e2e.parse_args([])
    assert args.no_inline_projection is False
    assert args.projection_wait == 75.0


def test_parse_args_flag_and_wait(e2e):
    args = e2e.parse_args(["--no-inline-projection", "--projection-wait", "10"])
    assert args.no_inline_projection is True
    assert args.projection_wait == 10.0
