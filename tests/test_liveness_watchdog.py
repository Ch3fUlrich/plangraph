"""Tests for the generic external liveness watchdog (``tools/liveness_watchdog.py``).

Every test injects a fake clock (``main(..., now=...)``), builds its config and
state under ``tmp_path``, and drives configured commands through tiny python
scripts that append their last argument (the message) to a marker file.  Nothing
here is site specific: no host, path or session names of any real deployment.
"""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time


REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "tools" / "liveness_watchdog.py"

spec = importlib.util.spec_from_file_location("liveness_watchdog", MODULE_PATH)
assert spec is not None and spec.loader is not None
lw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lw)

MINUTE = 60
#: One fixed instant for the fake clock; ages are always relative to it.
NOW = 1_760_000_000.0


def make_cmd(directory, name, exit_code=0):
    """A tiny python command: appends its last argument to *name*.marker, exits."""
    marker = Path(directory) / f"{name}.marker"
    script = Path(directory) / f"{name}.py"
    script.write_text(
        "import sys\n"
        f"with open({str(marker)!r}, 'a', encoding='utf-8') as handle:\n"
        "    handle.write((sys.argv[1] if len(sys.argv) > 1 else 'check') + '\\n')\n"
        f"sys.exit({int(exit_code)})\n",
        encoding="utf-8",
    )
    return [sys.executable, str(script)], marker


def write_config(directory, targets, inbox=None, name="watchdog.json"):
    """A JSON config file (JSON and TOML configs take the same shape)."""
    config = {"state_dir": str(Path(directory) / "state"), "targets": targets}
    if inbox is not None:
        config["inbox"] = str(inbox)
    path = Path(directory) / name
    path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    return path


def state_file(directory, name):
    return Path(directory) / "state" / f"liveness-{name}.json"


def read_state(directory, name):
    return json.loads(state_file(directory, name).read_text(encoding="utf-8"))


def lines(path):
    return Path(path).read_text(encoding="utf-8").splitlines()


def test_fresh_stamp_no_actions(tmp_path, capsys):
    stamp = tmp_path / "stamp.json"
    lw.write_stamp(stamp, now=NOW)
    alert_cmd, alert_marker = make_cmd(tmp_path, "alert")
    config = write_config(
        tmp_path, [{"name": "alpha", "stamp": str(stamp), "alert_cmd": alert_cmd}]
    )
    assert lw.main(["--config", str(config), "--apply"], now=NOW) == 0
    assert not alert_marker.exists()
    assert not (tmp_path / "state").exists()
    assert capsys.readouterr().out == ""


def test_stale_alerts_once_per_episode(tmp_path, capsys):
    stamp = tmp_path / "stamp.json"
    lw.write_stamp(stamp, now=NOW - 50 * MINUTE)
    alert_cmd, alert_marker = make_cmd(tmp_path, "alert")
    inbox = tmp_path / "inbox.log"
    config = write_config(
        tmp_path,
        [{"name": "alpha", "stamp": str(stamp), "alert_cmd": alert_cmd}],
        inbox=inbox,
    )
    assert lw.main(["--config", str(config), "--apply"], now=NOW) == 0
    fired = lines(alert_marker)
    assert len(fired) == 1 and "alpha" in fired[0]
    assert len(lines(inbox)) == 1 and "alpha" in lines(inbox)[0]

    # A later pass of the same episode alerts nothing new.
    assert lw.main(["--config", str(config), "--apply"], now=NOW + MINUTE) == 0
    assert len(lines(alert_marker)) == 1
    assert len(lines(inbox)) == 1
    assert read_state(tmp_path, "alpha")["alerted"] is True


def test_stale_relaunches_once_with_message(tmp_path):
    stamp = tmp_path / "stamp.json"
    lw.write_stamp(stamp, now=NOW - 80 * MINUTE)
    alert_cmd, alert_marker = make_cmd(tmp_path, "alert")
    relaunch_cmd, relaunch_marker = make_cmd(tmp_path, "relaunch")
    config = write_config(
        tmp_path,
        [
            {
                "name": "alpha",
                "stamp": str(stamp),
                "alert_cmd": alert_cmd,
                "relaunch_cmd": relaunch_cmd,
            }
        ],
    )
    assert lw.main(["--config", str(config), "--apply"], now=NOW) == 0
    assert len(lines(alert_marker)) == 1
    fired = lines(relaunch_marker)
    assert len(fired) == 1
    assert "alpha" in fired[0] and "80" in fired[0]

    assert lw.main(["--config", str(config), "--apply"], now=NOW + MINUTE) == 0
    assert len(lines(relaunch_marker)) == 1
    assert read_state(tmp_path, "alpha")["relaunched"] is True


def test_alive_failure_relaunches_with_fresh_stamp(tmp_path):
    stamp = tmp_path / "stamp.json"
    lw.write_stamp(stamp, now=NOW)
    alive_cmd, alive_marker = make_cmd(tmp_path, "alive", exit_code=1)
    alert_cmd, alert_marker = make_cmd(tmp_path, "alert")
    relaunch_cmd, relaunch_marker = make_cmd(tmp_path, "relaunch")
    config = write_config(
        tmp_path,
        [
            {
                "name": "alpha",
                "stamp": str(stamp),
                "alive_cmd": alive_cmd,
                "alert_cmd": alert_cmd,
                "relaunch_cmd": relaunch_cmd,
            }
        ],
    )
    assert lw.main(["--config", str(config), "--apply"], now=NOW) == 0
    assert not alert_marker.exists()  # the stamp itself is fresh: no alert
    fired = lines(relaunch_marker)
    assert len(fired) == 1 and "not alive" in fired[0]
    assert lines(alive_marker) == ["check"]

    assert lw.main(["--config", str(config), "--apply"], now=NOW + MINUTE) == 0
    assert len(lines(relaunch_marker)) == 1


def test_target_without_relaunch_cmd_is_alert_only(tmp_path, capsys):
    stamp = tmp_path / "stamp.json"
    lw.write_stamp(stamp, now=NOW - 80 * MINUTE)
    alert_cmd, alert_marker = make_cmd(tmp_path, "alert")
    _, never_cmd = make_cmd(tmp_path, "relaunch")
    config = write_config(
        tmp_path,
        [{"name": "alpha", "stamp": str(stamp), "alert_cmd": alert_cmd}],
    )
    assert lw.main(["--config", str(config), "--apply"], now=NOW) == 0
    assert lw.main(["--config", str(config), "--apply"], now=NOW + MINUTE) == 0
    assert len(lines(alert_marker)) == 1
    assert not never_cmd.exists()
    state = read_state(tmp_path, "alpha")
    assert state["relaunched"] is False and state["noted"] is True
    out = capsys.readouterr().out
    assert out.count("no relaunch_cmd configured") == 1  # noted once per episode


def test_mtime_only_stamp_counts(tmp_path):
    stamp = tmp_path / "beat.card"
    stamp.write_text("heartbeat\n", encoding="utf-8")
    atime = NOW - 50 * MINUTE
    os.utime(stamp, (atime, atime))
    alert_cmd, alert_marker = make_cmd(tmp_path, "alert")
    config = write_config(
        tmp_path, [{"name": "alpha", "stamp": str(stamp), "alert_cmd": alert_cmd}]
    )
    assert lw.main(["--config", str(config), "--apply"], now=NOW) == 0
    assert len(lines(alert_marker)) == 1


def test_corrupt_stamp_is_stale_forever(tmp_path):
    stamp = tmp_path / "stamp.json"
    stamp.write_text('{"ts": ', encoding="utf-8")  # truncated JSON...
    os.utime(stamp, (NOW, NOW))  # ...but an mtime that says "just beat"
    alert_cmd, alert_marker = make_cmd(tmp_path, "alert")
    config = write_config(
        tmp_path, [{"name": "alpha", "stamp": str(stamp), "alert_cmd": alert_cmd}]
    )
    assert lw.main(["--config", str(config), "--apply"], now=NOW) == 0
    assert len(lines(alert_marker)) == 1


def test_missing_stamp_is_stale(tmp_path):
    alert_cmd, alert_marker = make_cmd(tmp_path, "alert")
    config = write_config(
        tmp_path,
        [{"name": "alpha", "stamp": str(tmp_path / "absent.json"), "alert_cmd": alert_cmd}],
    )
    assert lw.main(["--config", str(config), "--apply"], now=NOW) == 0
    assert len(lines(alert_marker)) == 1


def test_fresh_again_resets_the_episode(tmp_path):
    stamp = tmp_path / "stamp.json"
    lw.write_stamp(stamp, now=NOW - 50 * MINUTE)
    alert_cmd, alert_marker = make_cmd(tmp_path, "alert")
    config = write_config(
        tmp_path, [{"name": "alpha", "stamp": str(stamp), "alert_cmd": alert_cmd}]
    )
    assert lw.main(["--config", str(config), "--apply"], now=NOW) == 0
    assert len(lines(alert_marker)) == 1

    lw.write_stamp(stamp, now=NOW)  # producer beat again -> episode over
    assert lw.main(["--config", str(config), "--apply"], now=NOW) == 0
    assert len(lines(alert_marker)) == 1
    assert read_state(tmp_path, "alpha") == {
        "alerted": False,
        "relaunched": False,
        "noted": False,
    }

    lw.write_stamp(stamp, now=NOW - 50 * MINUTE)  # a new stale episode
    assert lw.main(["--config", str(config), "--apply"], now=NOW) == 0
    assert len(lines(alert_marker)) == 2


def test_dry_run_writes_nothing_and_runs_no_commands(tmp_path, capsys):
    stamp = tmp_path / "stamp.json"
    lw.write_stamp(stamp, now=NOW - 80 * MINUTE)
    alert_cmd, alert_marker = make_cmd(tmp_path, "alert")
    relaunch_cmd, relaunch_marker = make_cmd(tmp_path, "relaunch")
    inbox = tmp_path / "inbox.log"
    config = write_config(
        tmp_path,
        [
            {
                "name": "alpha",
                "stamp": str(stamp),
                "alert_cmd": alert_cmd,
                "relaunch_cmd": relaunch_cmd,
            }
        ],
        inbox=inbox,
    )
    assert lw.main(["--config", str(config)], now=NOW) == 0
    out = capsys.readouterr().out
    assert "would alert" in out and "would relaunch" in out
    assert not alert_marker.exists() and not relaunch_marker.exists()
    assert not (tmp_path / "state").exists()
    assert not inbox.exists()
    # ...and the dry run left the episode untouched, so --apply still fires both.
    assert lw.main(["--config", str(config), "--apply"], now=NOW) == 0
    assert len(lines(alert_marker)) == 1 and len(lines(relaunch_marker)) == 1


def test_mtime_card_target_is_alert_only(tmp_path):
    card = tmp_path / "beat.card"
    card.write_text("card beat\n", encoding="utf-8")
    atime = NOW - 200 * MINUTE
    os.utime(card, (atime, atime))
    alert_cmd, alert_marker = make_cmd(tmp_path, "alert")
    relaunch_cmd, relaunch_marker = make_cmd(tmp_path, "relaunch")
    config = write_config(
        tmp_path,
        [
            {
                "name": "card",
                "stamp": str(card),
                "alert_after_min": 45,
                "relaunch_after_min": None,  # alert only
                "alert_cmd": alert_cmd,
                "relaunch_cmd": relaunch_cmd,
            }
        ],
    )
    assert lw.main(["--config", str(config), "--apply"], now=NOW) == 0
    assert len(lines(alert_marker)) == 1
    assert not relaunch_marker.exists()
    assert read_state(tmp_path, "card")["relaunched"] is False


def test_stamp_cli_writes_atomic_json(tmp_path):
    target = tmp_path / "beat.json"
    proc = subprocess.Popen(
        [sys.executable, str(MODULE_PATH), "stamp", str(target)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    stdout, stderr = proc.communicate(timeout=60)
    assert proc.returncode == 0
    assert "stamp written" in stdout and stderr == ""
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert set(payload) == {"ts", "iso", "pid"}
    assert abs(payload["ts"] - time.time()) < 10
    assert payload["iso"].endswith("Z")
    assert payload["pid"] == proc.pid
    # atomic write: the temp file is gone, only the stamp itself is left
    assert sorted(p.name for p in tmp_path.iterdir()) == ["beat.json"]
    # a watchdog pass reads that CLI stamp as fresh
    alert_cmd, alert_marker = make_cmd(tmp_path, "alert")
    config = write_config(
        tmp_path, [{"name": "alpha", "stamp": str(target), "alert_cmd": alert_cmd}]
    )
    assert lw.main(["--config", str(config), "--apply"]) == 0
    assert not alert_marker.exists()


def test_command_failure_is_logged_never_a_crash(tmp_path, capsys):
    stamp = tmp_path / "stamp.json"
    lw.write_stamp(stamp, now=NOW - 50 * MINUTE)
    config = write_config(
        tmp_path,
        [
            {
                "name": "alpha",
                "stamp": str(stamp),
                "alert_cmd": [str(tmp_path / "no-such-program"), "arg"],
            }
        ],
    )
    assert lw.main(["--config", str(config), "--apply"], now=NOW) == 0
    out = capsys.readouterr().out
    assert "command not found" in out
    assert read_state(tmp_path, "alpha")["alerted"] is True


def test_unusable_config_exits_two(tmp_path, capsys):
    assert lw.main(["--config", str(tmp_path / "absent.toml")], now=NOW) == 2
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    assert lw.main(["--config", str(broken)], now=NOW) == 2
    out = capsys.readouterr().out
    assert out.count("config error") == 2


def test_relaunch_skip_log_names_the_trigger(tmp_path, capsys):
    stamp = tmp_path / "stamp.json"
    lw.write_stamp(stamp, now=NOW)  # fresh: only the alive probe can make relaunch due
    alive_cmd, _ = make_cmd(tmp_path, "alive", exit_code=3)
    alert_cmd, _ = make_cmd(tmp_path, "alert")
    relaunch_cmd, relaunch_marker = make_cmd(tmp_path, "relaunch")
    stale = tmp_path / "stale.card"
    stale.write_text("beat\n", encoding="utf-8")
    os.utime(stale, (NOW - 80 * MINUTE, NOW - 80 * MINUTE))
    config = write_config(
        tmp_path,
        [
            {
                "name": "alertonly",
                "stamp": str(stamp),
                "relaunch_after_min": None,  # alert only
                "alive_cmd": alive_cmd,
                "alert_cmd": alert_cmd,
                "relaunch_cmd": relaunch_cmd,
            },
            {
                "name": "nocmd",
                "stamp": str(stale),
                "alert_cmd": alert_cmd,
            },
        ],
    )
    assert lw.main(["--config", str(config), "--apply"], now=NOW) == 0
    assert not relaunch_marker.exists()
    out = capsys.readouterr().out
    skipped = {
        name: line
        for name in ("alertonly", "nocmd")
        for line in out.splitlines()
        if "relaunch due but skipped" in line and f": {name}:" in line
    }
    assert "alertonly" in skipped["alertonly"]
    assert "alert-only" in skipped["alertonly"]
    assert "alive probe rc=3" in skipped["alertonly"]
    assert "alert-only" in skipped["nocmd"]
    assert "no relaunch_cmd configured" in skipped["nocmd"]
    assert "stale age 80.0 min" in skipped["nocmd"]


def test_dry_run_reset_not_saved_and_apply_reset_rearms_alert(tmp_path, capsys):
    stamp = tmp_path / "stamp.json"
    lw.write_stamp(stamp, now=NOW - 50 * MINUTE)
    alert_cmd, alert_marker = make_cmd(tmp_path, "alert")
    config = write_config(
        tmp_path, [{"name": "alpha", "stamp": str(stamp), "alert_cmd": alert_cmd}]
    )
    # 1. stale --apply: the episode alerts once and records it.
    assert lw.main(["--config", str(config), "--apply"], now=NOW) == 0
    assert len(lines(alert_marker)) == 1
    assert read_state(tmp_path, "alpha")["alerted"] is True

    # 2. a dry run while fresh admits it cannot reset: nothing is saved.
    lw.write_stamp(stamp, now=NOW)  # producer beat again -> the episode is over
    assert lw.main(["--config", str(config)], now=NOW) == 0
    out = capsys.readouterr().out
    assert "would reset (not saved)" in out
    assert "episode reset" not in out
    assert read_state(tmp_path, "alpha")["alerted"] is True  # dry run wrote nothing

    # 3. --apply while fresh resets the episode on its own run.
    assert lw.main(["--config", str(config), "--apply"], now=NOW) == 0
    assert read_state(tmp_path, "alpha") == {
        "alerted": False,
        "relaunched": False,
        "noted": False,
    }
    assert len(lines(alert_marker)) == 1

    # ... so the second stale --apply alerts again.
    lw.write_stamp(stamp, now=NOW - 50 * MINUTE)
    assert lw.main(["--config", str(config), "--apply"], now=NOW) == 0
    assert len(lines(alert_marker)) == 2


def test_load_state_error_names_the_oserror_subclass(tmp_path, capsys, monkeypatch):
    stamp = tmp_path / "stamp.json"
    lw.write_stamp(stamp, now=NOW - 50 * MINUTE)
    alert_cmd, _ = make_cmd(tmp_path, "alert")
    config = write_config(
        tmp_path, [{"name": "alpha", "stamp": str(stamp), "alert_cmd": alert_cmd}]
    )
    state = state_file(tmp_path, "alpha")
    state.parent.mkdir(parents=True)
    state.write_text('{"alerted": true}\n', encoding="utf-8")
    real_read_text = Path.read_text

    def fake_read_text(self, *args, **kwargs):
        if Path(self) == state:
            raise PermissionError(13, "Permission denied")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fake_read_text)
    assert lw.main(["--config", str(config), "--apply"], now=NOW) == 0
    out = capsys.readouterr().out
    assert "state unreadable (PermissionError); starting clean" in out


def test_stale_forever_reports_distinct_reasons(tmp_path, capsys, monkeypatch):
    alert_cmd, alert_marker = make_cmd(tmp_path, "alert")

    unreadable = tmp_path / "unreadable.json"
    unreadable.write_text('{"ts": 1}\n', encoding="utf-8")
    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text('{"ts": ', encoding="utf-8")  # truncated JSON
    nots = tmp_path / "nots.json"
    nots.write_text('{"iso": "x"}\n', encoding="utf-8")  # valid JSON, no ts field
    real_read_bytes = Path.read_bytes

    def fake_read_bytes(self, *args, **kwargs):
        if Path(self) == unreadable:
            raise PermissionError(13, "Permission denied")
        return real_read_bytes(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_bytes", fake_read_bytes)
    config = write_config(
        tmp_path,
        [
            {
                "name": reason,
                "stamp": str(stamp),
                "alert_cmd": alert_cmd,
            }
            for reason, stamp in (
                ("missing", tmp_path / "absent.json"),
                ("unreadable", unreadable),
                ("corrupt", corrupt),
                ("nots", nots),
            )
        ],
    )
    assert lw.main(["--config", str(config), "--apply"], now=NOW) == 0
    assert len(lines(alert_marker)) == 4
    out = capsys.readouterr().out
    for reason in ("missing", "unreadable", "corrupt json", "no ts field"):
        assert f"stale forever ({reason})" in out


def test_toml_config_round_trip(tmp_path):
    stamp = tmp_path / "stamp.json"
    lw.write_stamp(stamp, now=NOW - 50 * MINUTE)
    alert_cmd, alert_marker = make_cmd(tmp_path, "alert")
    config = tmp_path / "watchdog.toml"
    config.write_text(
        "state_dir = {}\n"
        "inbox = {}\n"
        "[[targets]]\n"
        "name = \"alpha\"\n"
        "stamp = {}\n"
        "alert_after_min = 45\n"
        "relaunch_after_min = 75\n"
        "alert_cmd = {}\n".format(
            json.dumps(str(tmp_path / "state")),
            json.dumps(str(tmp_path / "inbox.log")),
            json.dumps(str(stamp)),
            json.dumps(alert_cmd),
        ),
        encoding="utf-8",
    )
    assert lw.main(["--config", str(config), "--apply"], now=NOW) == 0
    assert len(lines(alert_marker)) == 1
    assert len(lines(tmp_path / "inbox.log")) == 1
