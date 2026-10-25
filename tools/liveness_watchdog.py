#!/usr/bin/env python3
"""Generic external liveness watchdog, configured entirely from a file.

It runs outside any agent session -- typically from a systemd user timer (see
``deploy/liveness/liveness-watchdog@.service``) -- so a producer whose own
in-session schedules died is still noticed and relaunched.  Nothing
site-specific lives in this file: target names, stamp paths, thresholds and
every command come from the config it is pointed at.

    liveness_watchdog.py --config <toml|json> [--apply]   # default: dry run
    liveness_watchdog.py stamp <path>                     # write a stamp now

Config shape (TOML shown; JSON is the same structure)::

    state_dir = "<directory for per-target episode state>"
    inbox = "<optional file: one line appended per alert>"

    [[targets]]
    name = "<target id>"                 # state: <state_dir>/liveness-<name>.json
    stamp = "<stamp file>"               # a {"ts": <unix>} JSON stamp, or any file
                                         # whose mtime counts
    alert_after_min = 45                 # default 45
    relaunch_after_min = 75              # default 75; null = this target is alert only
    alive_cmd = ["<prog>", "<arg>"]      # optional; exit 0 = alive
    alert_cmd = ["<prog>", "<arg>"]      # required; the message is appended last
    relaunch_cmd = ["<prog>", "<arg>"]   # optional; the message is appended last

Semantics
---------
* ``age = now - stamp ts``.  A stamp that is missing, unreadable or corrupt is
  stale forever, reported as ``stale forever (missing | unreadable |
  corrupt json | no ts field)``.  Content that looks like JSON (it starts with
  ``{``) must parse to an object with a numeric ``ts``; anything else is
  corrupt.  Content that is not JSON at all is an mtime stamp and its
  modification time counts.
* ``age > alert_after_min`` -> alert ONCE per stale episode.
* ``age > relaunch_after_min`` OR ``alive_cmd`` exiting non-zero -> relaunch
  ONCE per episode.  ``relaunch_after_min = null`` (or a missing
  ``relaunch_cmd``) makes the target alert only; a due-but-skipped relaunch is
  logged once per episode with its trigger (``stale age N min``,
  ``alive probe rc=X``, or ``alive probe skipped in dry-run``).
* An episode ends when the stamp is fresh again and ``alive_cmd`` (if any)
  exits 0; the next stale episode alerts again.  A dry run only reports
  ``would reset (not saved)``: the episode state is reset and saved by the
  ``--apply`` run that finds the stamp fresh.
* Commands are argv lists taken verbatim from the config -- no shell, no
  ``shlex`` re-parsing -- each with a 120 s timeout.  A command that cannot be
  started or exits non-zero becomes one log line (at most a 200-char tail of
  its own output) and never a crash.
* Without ``--apply`` the run is a dry run: planned actions are printed, no
  command is executed and nothing at all is written (no state, no inbox, no
  ``state_dir``).  A dry run judges staleness only; it cannot probe
  ``alive_cmd`` without running a command.  Where that skipped probe alone
  could make the target due, the run reports ``alive probe skipped in
  dry-run`` instead of a stale age the stamp does not support.
* Nothing from the environment is echoed, and no command output beyond that
  200-char tail is ever printed.

Exit status: 0 for every run the watchdog could evaluate (whatever the
configured commands did); 2 only when the config itself cannot be read, parsed
or lacks ``state_dir``/``targets``, so a timer can surface a broken install.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

try:  # python 3.11+; stdlib TOML reader
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - python < 3.11
    tomllib = None  # type: ignore[assignment]

PROG = "liveness-watchdog"

#: Defaults for [[targets]] entries that omit the threshold.
DEFAULT_ALERT_AFTER_MIN = 45.0
DEFAULT_RELAUNCH_AFTER_MIN = 75.0

#: Hard timeout for every configured command (alive, alert, relaunch).
COMMAND_TIMEOUT_S = 120

#: At most this many trailing characters of a command's output are echoed.
OUTPUT_TAIL_CHARS = 200

MINUTE = 60.0


class ConfigError(Exception):
    """The config file cannot be used as a watchdog config."""


def log(message: str, out=None) -> None:
    """One plain line for the journal: no environment, no secrets, no tracebacks."""
    stream = out if out is not None else sys.stdout
    stream.write(f"{PROG}: {message}\n")
    try:
        stream.flush()
    except (OSError, ValueError):  # pragma: no cover - closed stream
        pass


def _tail(text: str | None) -> str:
    """The last ``OUTPUT_TAIL_CHARS`` characters of *text*, flattened to one line."""
    flat = " ".join((text or "").split())
    if len(flat) <= OUTPUT_TAIL_CHARS:
        return flat
    return "..." + flat[-OUTPUT_TAIL_CHARS:]


def _iso(moment: float) -> str:
    stamp = datetime.datetime.fromtimestamp(moment, datetime.timezone.utc)
    return stamp.isoformat(timespec="seconds").replace("+00:00", "Z")


def atomic_write_json(path, payload) -> Path:
    """Write *payload* as JSON through a same-directory temp file + ``os.replace``."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".liveness-tmp-", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return target


def write_stamp(path, now: float | None = None) -> dict:
    """Write ``{"ts", "iso", "pid"}`` atomically so any session/beat can beat once.

    CLI form: ``liveness_watchdog.py stamp <path>``.
    """
    moment = time.time() if now is None else float(now)
    payload = {
        "ts": moment,
        "iso": _iso(moment),
        "pid": os.getpid(),
    }
    atomic_write_json(path, payload)
    return payload


def stamp_status(path, now: float) -> tuple[float, str | None]:
    """``(age_seconds, stale_reason)`` for the stamp at *path*.

    ``stale_reason`` is ``None`` while the stamp is usable and one of
    ``missing``, ``unreadable``, ``corrupt json`` or ``no ts field`` when the
    stamp is stale forever.  A ``stat()`` failure is classified like a read
    failure: ``FileNotFoundError`` is ``missing``, any other ``OSError`` is
    ``unreadable``.  Content starting with ``{`` is meant to be a JSON stamp
    and must carry a numeric ``ts``; anything else is read as an mtime stamp.
    A JSON stamp whose ``ts`` parses keeps that ``ts`` no matter what
    ``stat()`` does -- it never reads as a fresh age of 0.
    """
    target = Path(path)
    try:
        raw = target.read_bytes()
    except FileNotFoundError:
        return float("inf"), "missing"
    except OSError:
        return float("inf"), "unreadable"
    stripped = raw.strip()
    if stripped.startswith(b"{"):
        try:
            parsed = json.loads(stripped.decode("utf-8"))
        except (ValueError, UnicodeError):
            return float("inf"), "corrupt json"
        ts = parsed.get("ts") if isinstance(parsed, dict) else None
        if isinstance(ts, bool) or not isinstance(ts, (int, float)):
            return float("inf"), "no ts field"
        return max(0.0, now - float(ts)), None
    try:
        mtime = target.stat().st_mtime
    except FileNotFoundError:
        return float("inf"), "missing"
    except OSError:
        return float("inf"), "unreadable"
    return max(0.0, now - mtime), None


def stamp_age_seconds(path, now: float) -> float:
    """``now - stamp ts`` in seconds; ``inf`` when the stamp is stale forever."""
    return stamp_status(path, now)[0]


def load_config(path) -> dict:
    """Read a TOML or JSON config into a plain dict; raise ``ConfigError``."""
    source = Path(path)
    try:
        raw = source.read_bytes()
    except OSError as exc:
        raise ConfigError(f"cannot read config ({type(exc).__name__})") from None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ConfigError("config is not UTF-8 text") from None
    try:
        if text.lstrip().startswith("{"):
            parsed = json.loads(text)
        else:
            if tomllib is None:
                raise ConfigError("TOML config needs python 3.11 or newer")
            parsed = tomllib.loads(text)
    except ValueError as exc:  # json and TOML parse errors are both ValueErrors
        raise ConfigError(f"config does not parse ({type(exc).__name__})") from None
    if not isinstance(parsed, dict):
        raise ConfigError("config top level is not a table/object")
    if not isinstance(parsed.get("state_dir"), str) or not parsed["state_dir"]:
        raise ConfigError("state_dir is missing or not a string")
    inbox = parsed.get("inbox")
    if inbox is not None and (not isinstance(inbox, str) or not inbox):
        raise ConfigError("inbox is missing a usable path")
    if not isinstance(parsed.get("targets"), list):
        raise ConfigError("targets is missing or not a list")
    return parsed


def _as_argv(value):
    """A non-empty list of strings, or ``None`` when absent; else raise."""
    if value is None:
        return None
    if not isinstance(value, (list, tuple)) or not value:
        raise ValueError("must be a non-empty list of strings")
    argv = []
    for item in value:
        if not isinstance(item, str) or not item:
            raise ValueError("must be a non-empty list of strings")
        argv.append(item)
    return argv


def _as_minutes(value, default: float) -> float:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("threshold must be a number of minutes")
    if value < 0:
        raise ValueError("threshold must not be negative")
    return float(value)


def _target(entry, where: str, out) -> dict | None:
    """Normalize one ``[[targets]]`` table; ``None`` (already logged) to skip it."""
    if not isinstance(entry, dict):
        log(f"{where}: target entry is not a table; skipped", out)
        return None
    name = entry.get("name")
    if (
        not isinstance(name, str)
        or not name.strip()
        or name in {".", ".."}
        or "/" in name
        or os.sep in name
    ):
        log(f"{where}: target has no usable name; skipped", out)
        return None
    where = f"target {name}"
    try:
        stamp = entry.get("stamp")
        if not isinstance(stamp, str) or not stamp:
            raise ValueError("stamp must be a file path")
        alert_cmd = _as_argv(entry.get("alert_cmd"))
        if alert_cmd is None:
            raise ValueError("alert_cmd is required")
        alive_cmd = _as_argv(entry.get("alive_cmd"))
        relaunch_cmd = _as_argv(entry.get("relaunch_cmd"))
        alert_after = _as_minutes(entry.get("alert_after_min"), DEFAULT_ALERT_AFTER_MIN)
        raw_relaunch = entry.get("relaunch_after_min", DEFAULT_RELAUNCH_AFTER_MIN)
        relaunch_after = None if raw_relaunch is None else _as_minutes(raw_relaunch, 0.0)
    except ValueError as exc:
        log(f"{where}: invalid config ({exc}); skipped", out)
        return None
    return {
        "name": name,
        "stamp": stamp,
        "alert_cmd": alert_cmd,
        "alive_cmd": alive_cmd,
        "relaunch_cmd": relaunch_cmd,
        "alert_after_min": alert_after,
        "relaunch_after_min": relaunch_after,
    }


def run_command(argv, message: str | None, out, tag: str) -> int | None:
    """Run *argv* verbatim (message appended as the last argument), 120 s cap.

    Returns the exit status, or ``None`` when the command could not be run.
    Never raises; output is echoed as a 200-char tail at most.
    """
    cmd = list(argv)
    if message:
        cmd.append(message)
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            timeout=COMMAND_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired:
        log(f"{tag}: command timed out after {COMMAND_TIMEOUT_S}s", out)
        return None
    except FileNotFoundError:
        log(f"{tag}: command not found: {cmd[0]}", out)
        return None
    except OSError as exc:
        log(f"{tag}: command could not start ({type(exc).__name__})", out)
        return None
    if proc.returncode != 0:
        log(f"{tag}: command exit {proc.returncode}; tail: {_tail(proc.stdout)}", out)
    return proc.returncode


def state_path(state_dir, name: str) -> Path:
    return Path(state_dir) / f"liveness-{name}.json"


def load_state(path, out) -> dict:
    """Per-target episode flags; a missing or unreadable state starts clean."""
    flags = {"alerted": False, "relaunched": False, "noted": False}
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return flags
    except OSError as exc:
        log(f"state unreadable ({type(exc).__name__}); starting clean", out)
        return flags
    try:
        parsed = json.loads(raw)
    except ValueError:
        log("state corrupt; starting clean", out)
        return flags
    if isinstance(parsed, dict):
        for key in flags:
            flags[key] = bool(parsed.get(key))
    else:
        log("state is not an object; starting clean", out)
    return flags


def save_state(path, flags: dict) -> None:
    atomic_write_json(path, flags)


def append_inbox(inbox, line: str, out) -> None:
    try:
        target = Path(inbox)
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
    except OSError as exc:
        log(f"inbox append failed ({type(exc).__name__})", out)


def check_target(target: dict, state_dir, inbox, apply: bool, now: float, out) -> None:
    """Evaluate one target: alert and/or relaunch at most once per episode."""
    name = target["name"]
    age, stale_reason = stamp_status(target["stamp"], now)
    age_min = age / MINUTE
    stale = age > target["alert_after_min"] * MINUTE

    alive_ok = True
    alive_rc: int | None = None
    alive_skipped = not apply and target["alive_cmd"] is not None
    if apply and target["alive_cmd"] is not None:
        alive_rc = run_command(target["alive_cmd"], None, out, f"{name}: alive")
        alive_ok = alive_rc == 0
        if not alive_ok:
            log(f"{name}: alive command reports not alive", out)

    over_age = (
        target["relaunch_after_min"] is not None
        and age > target["relaunch_after_min"] * MINUTE
    )
    # A stamp age only triggers when it reads as more than "0.0 min": a
    # just-beaten stamp at a zero-ish threshold is a fake age, and then only
    # the alive probe could make this target due -- a probe a dry run never
    # runs, so the dry run must say so instead of inventing a stale age.
    age_triggers = over_age and age_min >= 0.05
    only_alive_could_trigger = alive_skipped and not age_triggers

    state = state_path(state_dir, name)
    flags = load_state(state, out)

    if not stale and alive_ok:
        if only_alive_could_trigger:
            log(f"{name}: alive probe skipped in dry-run", out)
            return
        if any(flags.values()):
            if apply:
                log(f"{name}: healthy again (age {age_min:.1f} min); episode reset", out)
                save_state(
                    state,
                    {"alerted": False, "relaunched": False, "noted": False},
                )
            else:
                log(
                    f"{name}: healthy again (age {age_min:.1f} min); "
                    "would reset (not saved)",
                    out,
                )
        return

    if stale_reason is not None:
        age_text = f"stale forever ({stale_reason})"
    else:
        age_text = f"{age_min:.1f} min old"
    if alive_ok and not only_alive_could_trigger:
        trigger = f"stale age {age_min:.1f} min"
    elif alive_rc is not None:
        trigger = f"alive probe rc={alive_rc}"
    elif alive_skipped:
        trigger = "alive probe skipped in dry-run"
    else:
        trigger = "alive probe could not run"
    if only_alive_could_trigger:
        log(f"{name}: alive probe skipped in dry-run", out)
    changed = False

    if stale and not flags["alerted"]:
        message = (
            f"{PROG} alert: target {name} stamp is {age_text} "
            f"(alert_after_min {target['alert_after_min']:g}): {target['stamp']}"
        )
        if apply:
            log(f"{name}: alert ({age_text})", out)
            run_command(target["alert_cmd"], message, out, f"{name}: alert")
            if inbox:
                append_inbox(inbox, f"{_iso(now)} ALERT {name}: {message}", out)
        else:
            log(f"[dry-run] would alert: {message}", out)
        flags["alerted"] = True
        changed = True

    relaunch_allowed = (
        target["relaunch_after_min"] is not None
        and target["relaunch_cmd"] is not None
    )
    want_relaunch = over_age or not alive_ok

    if want_relaunch and not flags["relaunched"]:
        if relaunch_allowed:
            reason = (
                f"stamp is {age_text} "
                f"(relaunch_after_min {target['relaunch_after_min']:g})"
                if over_age
                else "alive command reports not alive"
            )
            message = f"{PROG} relaunch: target {name} {reason}: {target['stamp']}"
            if apply:
                log(f"{name}: relaunch ({age_text})", out)
                run_command(
                    target["relaunch_cmd"], message, out, f"{name}: relaunch"
                )
            else:
                log(f"[dry-run] would relaunch: {message}", out)
            flags["relaunched"] = True
            changed = True
        elif not flags["noted"]:
            reason = (
                "alert-only target (relaunch_after_min is null)"
                if target["relaunch_after_min"] is None
                else "alert-only target (no relaunch_cmd configured)"
            )
            log(f"{name}: relaunch due but skipped ({reason}); trigger {trigger}", out)
            flags["noted"] = True
            changed = True

    if changed and apply:
        save_state(state, flags)


def run_watchdog(config: dict, apply: bool = False, now: float | None = None,
                 out=None) -> int:
    """Evaluate every target of a parsed config; always returns 0 on a bad target."""
    stream = out if out is not None else sys.stdout
    moment = time.time() if now is None else float(now)
    state_dir = config.get("state_dir")
    inbox = config.get("inbox")
    targets = config.get("targets")
    if not isinstance(state_dir, str) or not state_dir:
        log("config error: state_dir is missing or not a string", stream)
        return 2
    if inbox is not None and not isinstance(inbox, str):
        log("config error: inbox is not a string", stream)
        return 2
    if not isinstance(targets, list):
        log("config error: targets is missing or not a list", stream)
        return 2
    if not apply:
        log("[dry-run] no command runs and nothing is written (pass --apply)", stream)
    for index, entry in enumerate(targets, 1):
        target = _target(entry, f"entry {index}", stream)
        if target is None:
            continue
        try:
            check_target(target, state_dir, inbox, apply, moment, stream)
        except Exception as exc:  # a watchdog pass must survive any one target
            log(f"target {target['name']}: unexpected {type(exc).__name__}; skipped",
                stream)
    return 0


def _run_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Generic external liveness watchdog (dry run without --apply).",
    )
    parser.add_argument("--config", required=True, metavar="<toml|json>",
                        help="watchdog config file")
    parser.add_argument("--apply", action="store_true",
                        help="run the configured commands and write state "
                             "(default: dry run, print only)")
    return parser


def _stamp_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=f"{PROG} stamp",
        description="Atomically write a liveness stamp file.",
    )
    parser.add_argument("path", help="stamp file to write")
    return parser


def main(argv=None, now: float | None = None) -> int:
    """CLI entry point; *now* injects a clock for tests."""
    args_list = list(sys.argv[1:] if argv is None else argv)
    try:
        if args_list and args_list[0] == "stamp":
            parsed = _stamp_parser().parse_args(args_list[1:])
            payload = write_stamp(parsed.path, now=now)
            log(f"stamp written: {parsed.path} ts={payload['ts']:g}")
            return 0
        parsed = _run_parser().parse_args(args_list)
    except SystemExit as exc:  # argparse already printed the usage
        return int(exc.code or 0)
    try:
        config = load_config(parsed.config)
    except ConfigError as exc:
        log(f"config error: {exc} ({parsed.config})")
        return 2
    return run_watchdog(config, apply=parsed.apply, now=now)


if __name__ == "__main__":
    raise SystemExit(main())
