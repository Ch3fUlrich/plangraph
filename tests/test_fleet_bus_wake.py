"""RT-P1-BUS-WAKE: the wake hook turns one keeper `messages` row into at most
one session wake.

The event is the long-poll return, never a timer.  Each DONE behaviour and each
attack vector has its own test:

* router-authored row -> wakes nothing,
* a real report wakes the router within 60 s (fake clock + fake long-poll),
* duplicate NOTIFY -> one wake,
* cursor lost on restart -> no replay storm and no loss,
* unknown session -> refused and logged, no crash,
* wake storm -> debounced,
* busy target -> retried later, never dropped (including across a restart).
"""
from __future__ import annotations

import json

import pytest

from plangraph.fleet_bus.wake import (
    DEFAULT_DEBOUNCE_S,
    DEFAULT_RETRY_S,
    DEFAULT_TEXT,
    SessionTarget,
    WakeBusy,
    WakeHook,
    WakeRefused,
    WakeState,
)

EPOCH = 1_700_000_000.0


class Clock:
    """Fake epoch clock: the tests move time, nothing ever sleeps."""

    def __init__(self, now=EPOCH):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds
        return self.now


class Sink:
    """Recording wake sink with scripted busy/refusal; never opens a socket."""

    def __init__(self, busy=0, refuse=False):
        self.calls = []
        self.busy = busy
        self.refuse = refuse

    def wake(self, target, text):
        self.calls.append((target.session, text))
        if self.refuse:
            raise WakeRefused("unknown session")
        if self.busy:
            self.busy -= 1
            raise WakeBusy("session busy")

    @property
    def sessions(self):
        return [session for session, _text in self.calls]


class Stop:
    """Duck-typed stop event, the shape `discovery.Registry.run` already takes."""

    def __init__(self):
        self.stopped = False
        self.waits = []

    def is_set(self):
        return self.stopped

    def wait(self, timeout):
        self.waits.append(timeout)
        return False

    def stop(self):
        self.stopped = True


def row(seq, to="router", sender="relay", ts=None):
    """A `messages` row as the API returns it (ordered by `seq`)."""
    return {"seq": seq, "from": sender, "to": to,
            "ts": int((EPOCH if ts is None else ts) * 1000) + seq,
            "body": {"op": "report"}, "key": f"K-{seq}"}


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def sink():
    return Sink()


@pytest.fixture
def sessions():
    return {"router": SessionTarget("ses_r1", "http://router.example.test:4096"),
            "workst": SessionTarget("ses_w1", "http://workst.example.test:4096")}


def make(session_map, wake_sink, clock, state=None, log=None, **kw):
    return WakeHook(session_map, wake_sink, state, clock=clock, log=log, **kw)


# ------------------------------------------------------------- event: the poll

def test_report_wakes_router_within_60s_on_the_long_poll(sessions, sink, clock):
    """A real report wakes the router within 60 s: the fake long-poll returns
    the row and the fake clock only moves while the poll blocks.  No timer
    anywhere: the loop never waits on an interval."""
    hook = make(sessions, sink, clock)
    stop, waits = Stop(), []

    def poll(since, wait_ms):
        waits.append((since, wait_ms))
        clock.advance(45)                     # the poll blocked on the event
        stop.stop()
        return [row(1, sender="worker")]

    hook.run(poll, stop, wait_ms=30000)

    assert waits == [(0, 30000)]
    assert sink.sessions == ["ses_r1"]
    assert sink.calls[0][1] == DEFAULT_TEXT
    assert clock.now - EPOCH <= 60            # report -> wake inside 60 s
    assert stop.waits == []                   # no sleep, no interval, no timer


def test_run_survives_a_poll_failure_and_keeps_waking(sessions, clock):
    """A failed long-poll is logged and backed off; the loop still delivers."""
    log, wake_sink, stop, attempts = [], Sink(), Stop(), []
    hook = make(sessions, wake_sink, clock, log=log.append)

    def poll(since, wait_ms):
        attempts.append(since)
        if len(attempts) == 1:
            raise OSError("connection refused")
        stop.stop()
        return [row(1, sender="worker")]

    hook.run(poll, stop, error_backoff_s=1.0)

    assert attempts == [0, 0]
    assert any("OSError" in message for message in log)
    assert stop.waits == [1.0]
    assert wake_sink.sessions == ["ses_r1"]


def test_long_poll_budget_follows_the_next_due_wake(sessions, sink, clock):
    """The poll returns by the next due wake, so retries need no timer."""
    hook = make(sessions, sink, clock)
    hook.feed([row(1)])
    assert hook.feed([row(2)]) == ["deferred"]
    assert hook.poll_wait_ms(30000) == DEFAULT_DEBOUNCE_S * 1000
    clock.advance(5)
    assert hook.poll_wait_ms(30000) == (DEFAULT_DEBOUNCE_S - 5) * 1000
    assert hook.poll_wait_ms(5000) == 5000    # never above the caller's cap
    clock.advance(DEFAULT_DEBOUNCE_S)
    assert hook.poll_wait_ms(30000) == 0      # due now: poll, then tick


# --------------------------------------------------- DONE: what never wakes

def test_router_authored_row_wakes_nothing(sessions, sink, clock):
    hook = make(sessions, sink, clock)
    assert hook.feed([row(1, sender="router"),
                      row(2, to="workst", sender="router")]) == ["ignored", "ignored"]
    assert sink.calls == []
    assert hook.state.pending == {}
    assert hook.cursor == 2
    # a real report from a worker still wakes the router afterwards
    assert hook.feed([row(3, sender="worker")]) == ["woken"]
    assert sink.sessions == ["ses_r1"]


def test_infrastructure_recipients_are_ignored_not_refused(sessions, sink, clock):
    hook = make(sessions, sink, clock, log=lambda message: pytest.fail(message))
    assert hook.feed([row(1, to="keeper"), row(2, to="relay")]) == ["ignored", "ignored"]
    assert sink.calls == []
    assert hook.refused == []
    assert hook.cursor == 2


def test_on_row_is_the_interface_other_code_calls(sessions, sink, clock):
    """W2's fleet-msg client pushes a row and gets its outcome back."""
    hook = make(sessions, sink, clock)
    assert hook.on_row(row(1)) == "woken"
    assert hook.on_row(row(1)) == "duplicate"
    assert hook.since == hook.cursor == 1
    assert hook.on_row(row(2)) == "deferred"  # inside the debounce window
    # a row-only caller still gets its deferred wake: on_row() ticks for it
    clock.advance(DEFAULT_DEBOUNCE_S)
    assert hook.on_row(row(3)) == "coalesced"
    assert sink.sessions == ["ses_r1", "ses_r1"]
    assert hook.state.pending == {}           # the deferred wake was delivered


# --------------------------------------------------------- vector: duplicates

def test_duplicate_notify_wakes_once(sessions, sink, clock):
    """One event = one wake, whether the redelivery is a whole re-read or a
    repeated row inside one read."""
    hook = make(sessions, sink, clock)
    assert hook.feed([row(1)]) == ["woken"]
    assert hook.feed([row(1)]) == ["duplicate"]              # duplicate NOTIFY
    assert hook.feed([row(1), row(1)]) == ["duplicate", "duplicate"]
    clock.advance(DEFAULT_DEBOUNCE_S)
    assert hook.feed([row(2)]) == ["woken"]
    assert hook.feed([row(2)]) == ["duplicate"]               # redelivered after the wake
    assert sink.sessions == ["ses_r1", "ses_r1"]              # two events, two wakes


def test_restart_resumes_from_the_persisted_cursor(tmp_path, sessions, sink, clock):
    path = tmp_path / "wake.state"
    first = make(sessions, sink, clock, state=WakeState(path))
    assert first.feed([row(1)]) == ["woken"]

    reopened_sink = Sink()
    second = make(sessions, reopened_sink, clock, state=WakeState(path))
    assert second.cursor == 1                                 # restart-safe
    assert second.feed([row(1)]) == ["duplicate"]             # no replay storm
    assert reopened_sink.calls == []
    assert second.feed([row(2)]) == ["woken"]                 # no loss
    assert reopened_sink.sessions == ["ses_r1"]


# ----------------------------------------------------- vector: lost cursor

def test_lost_cursor_replays_without_a_storm_and_without_a_loss(
        tmp_path, sessions, clock):
    path = tmp_path / "wake.state"
    first = make(sessions, Sink(), clock, state=WakeState(path))
    first.feed([row(i) for i in range(1, 41)])
    path.unlink()                                             # the cursor is gone

    replay_sink = Sink()
    replay = make(sessions, replay_sink, clock, state=WakeState(path))
    assert replay.cursor == 0
    outcomes = replay.feed([row(i) for i in range(1, 41)])
    assert outcomes[0] == "woken"                             # still pokes the router
    assert outcomes[1] == "deferred"
    assert set(outcomes[2:]) == {"coalesced"}
    assert len(replay_sink.calls) <= 2                        # no replay storm
    assert replay.cursor == 40                                # nothing skipped

    clock.advance(DEFAULT_DEBOUNCE_S)
    replay.tick()
    assert len(replay_sink.calls) <= 2
    assert replay.feed([row(41)]) == ["deferred"]             # new rows still count
    clock.advance(DEFAULT_DEBOUNCE_S)
    replay.tick()
    assert len(replay_sink.calls) == 3
    assert replay_sink.sessions[-1] == "ses_r1"


def test_corrupt_cursor_file_is_rebuilt_not_fatal(tmp_path, sessions, sink, clock):
    path = tmp_path / "wake.state"
    path.write_text("{ not json", encoding="utf-8", newline="\n")
    log = []
    hook = make(sessions, sink, clock, state=WakeState(path, log=log.append))
    assert hook.cursor == 0
    assert any("corrupt" in message for message in log)
    assert hook.feed([row(1)]) == ["woken"]
    assert json.loads(path.read_text(encoding="utf-8"))["cursor"] == 1


# ------------------------------------------------------- vector: unknown to

def test_unknown_session_is_refused_logged_and_never_crashes(sessions, sink, clock):
    log = []
    hook = make(sessions, sink, clock, log=log.append)
    assert hook.feed([row(1, to="ghost")]) == ["refused"]
    assert sink.calls == []
    assert hook.cursor == 1                                   # accounted: no loop
    assert any("ghost" in message for message in log)
    assert any("ghost" in entry for entry in hook.refused)
    assert len(hook.refused) <= 100                         # bounded
    assert hook.feed([row(2)]) == ["woken"]                   # later rows still wake
    assert sink.sessions == ["ses_r1"]


def test_row_without_a_usable_sequence_is_refused_not_fatal(sessions, sink, clock):
    log = []
    hook = make(sessions, sink, clock, log=log.append)
    assert hook.feed([{"to": "router", "from": "relay"}, {"seq": "x"}]) == ["refused", "refused"]
    assert hook.feed(["not a row"]) == ["refused"]
    assert sink.calls == []
    assert log and hook.refused


# --------------------------------------------------------- vector: wake storm

def test_wake_storm_is_debounced_to_one_wake(sessions, sink, clock):
    hook = make(sessions, sink, clock)
    outcomes = hook.feed([row(i) for i in range(1, 26)])
    assert outcomes[0] == "woken"
    assert outcomes[1] == "deferred"
    assert set(outcomes[2:]) == {"coalesced"}
    assert len(sink.calls) == 1                               # 25 events -> one wake

    clock.advance(DEFAULT_DEBOUNCE_S - 1)
    hook.tick()
    assert len(sink.calls) == 1                               # still inside the window
    clock.advance(1)
    hook.tick()
    assert len(sink.calls) == 2                               # exactly one follow-up
    assert sink.sessions == ["ses_r1", "ses_r1"]


def test_storms_of_two_sessions_stay_independent(sessions, sink, clock):
    hook = make(sessions, sink, clock)
    assert hook.feed([row(1), row(2, to="workst")]) == ["woken", "woken"]
    assert sink.sessions == ["ses_r1", "ses_w1"]      # each session wakes on its own
    assert hook.feed([row(3), row(4, to="workst")]) == ["deferred", "deferred"]
    assert len(sink.calls) == 2                       # both debounced, neither lost
    clock.advance(DEFAULT_DEBOUNCE_S)
    assert hook.tick() == ["woken", "woken"]
    assert sink.sessions == ["ses_r1", "ses_w1", "ses_r1", "ses_w1"]
    assert hook.state.pending == {}


# ---------------------------------------------------------- vector: busy target

def test_busy_target_is_retried_later_and_never_dropped(sessions, clock):
    busy_sink = Sink(busy=1)
    hook = make(sessions, busy_sink, clock)
    assert hook.feed([row(1)]) == ["retry"]
    assert len(busy_sink.calls) == 1
    assert hook.state.pending["router"]["count"] == 1

    hook.tick()
    assert len(busy_sink.calls) == 1                           # not due yet
    clock.advance(DEFAULT_RETRY_S - 1)
    hook.tick()
    assert len(busy_sink.calls) == 1
    clock.advance(1)
    assert hook.tick() == ["woken"]
    assert len(busy_sink.calls) == 2
    assert hook.state.pending == {}


def test_pending_wake_survives_a_restart(tmp_path, sessions, clock):
    path = tmp_path / "wake.state"
    blocked = make(sessions, Sink(busy=99), clock, state=WakeState(path))
    assert blocked.feed([row(1)]) == ["retry"]

    freed_sink = Sink()
    revived = make(sessions, freed_sink, clock, state=WakeState(path))
    assert revived.state.pending                               # obligation persisted
    assert revived.tick() == []                               # retry time not reached
    clock.advance(DEFAULT_RETRY_S)
    assert revived.tick() == ["woken"]
    assert freed_sink.sessions == ["ses_r1"]                  # never dropped


def test_sink_refusal_is_logged_and_does_not_retry_forever(sessions, clock):
    log = []
    hook = make(sessions, Sink(refuse=True), clock, log=log.append)
    assert hook.feed([row(1)]) == ["refused"]
    assert hook.state.pending == {}                           # no poison retry loop
    assert any("refused" in message for message in log)
    assert any("unknown session" in entry for entry in hook.refused)


# --------------------------------------------------------------- configuration

def test_hook_configuration_is_validated(sessions, sink, clock):
    with pytest.raises(ValueError):
        WakeHook({"router": "ses_r1"}, sink, clock=clock)
    with pytest.raises(ValueError):
        WakeHook(sessions, sink, clock=clock, retry_s=0)
    with pytest.raises(ValueError):
        WakeHook(sessions, sink, clock=clock, debounce_s=-1)
