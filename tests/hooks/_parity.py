#!/usr/bin/env python3
"""Scenario table + runner behind the hook parity corpus (tests/hooks/).

The corpus in ``parity_payloads.json`` pins the ORIGINAL hooks' observable
contract - stdout, stderr and exit code - so the ported hooks in
``tools/hooks/`` (including the split ``bash_guard_rules*`` modules) can be
replayed against it byte for byte.  Every scenario runs in a throwaway HOME
whose state starts EMPTY: the machine's live state is never read, and the
only files that exist are the scenario's own recorded fixtures (transcripts,
one launch record, a seeded inbox) - explicit input data, written fresh per
run.

``{TMP}`` in ``stdin``, ``env`` values and fixture contents is substituted
with the run's temp HOME before the hook is executed, so a payload may point
a ``transcript_path`` at its own fixture.

Capture protocol (``python3 tests/hooks/_parity.py`` - only useful while the
AutoOS tree is present): each scenario runs TWICE - run A executes the
ORIGINAL hooks in place at the AutoOS ``tools/hooks`` tree (the read-only
checkout named by ``AUTOOS_HOME``), run B
executes copies of those originals from a second temp hook directory under a
second temp HOME - so time, pid, HOME and hook-location dependence all show
up as run A != run B.  Only byte-stable scenarios are recorded (expected
taken from run A, the originals in their own directory); anything unstable
goes to the corpus's ``skipped`` list with both runs kept for the record.
The emitting context-guard payloads embed the hook's own resolved path in
their additionalContext - host-dependent by construction - and are declared
skipped here rather than compared.

Replay (``python3 -m pytest tests/hooks/test_parity_payloads.py``) runs the
same scenarios against the PORT hooks in ``tools/hooks/`` and asserts the
three recorded fields byte for byte.
"""
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from _helpers import AUTOOS_HOME, FAKE_TOKEN

ROOT = Path(__file__).resolve().parents[2]
PORT_HOOKS = ROOT / "tools" / "hooks"
ORIGINAL_HOOKS = Path(AUTOOS_HOME + "/tools/hooks")
CORPUS = Path(__file__).resolve().parent / "parity_payloads.json"

J = json.dumps
FLEET_STATE = ".local/state/fleet"
LAUNCH = ".local/state/routing/launch/launch.json"
LAUNCH_RECORD = J({"session_id": "fake-child", "name": "fake-child",
                   "parent": "fake-parent"})
CTX_STDIN = J({"transcript_path": "{TMP}/t.jsonl", "session_id": "fake-session",
               "cwd": "/work"})


def _assistant(text=None, usage=None):
    """One transcript record line: assistant text and/or usage."""
    message = {"role": "assistant", "content": []}
    if text is not None:
        message["content"].append({"type": "text", "text": text})
    if usage is not None:
        message["usage"] = usage
    return J({"type": "assistant", "uuid": "turn-1", "message": message}) + "\n"


def _usage(input_tokens, extra=None):
    usage = {"input_tokens": input_tokens, "cache_read_input_tokens": 0,
             "cache_creation_input_tokens": 0}
    if extra:
        usage.update(extra)
    return usage


def _inbox_line(turn, status, line):
    return J({"ts": 1, "from": "fake-child", "session_id": "fake-child",
              "turn": turn, "status": status, "line": line}) + "\n"


_INBOX_A = _inbox_line("t1", "DONE", "DONE a")
_INBOX_B = _inbox_line("t2", "BLOCKED", "BLOCKED on " + FAKE_TOKEN + " review")
_INBOX_TWO = (_inbox_line("t1", "DONE", "DONE shipped the parser")
              + _inbox_line("t2", "BLOCKED", "BLOCKED on " + FAKE_TOKEN + " review"))
CG_EMIT = ("the additionalContext embeds str(Path(__file__).resolve()) of the "
           "hook script - the install location - so the output is "
           "host-dependent and cannot be compared byte for byte across hook "
           "locations; skipped per protocol")

B = J({"tool_name": "Bash", "tool_input": {"command": "ls -la"}})

SCENARIOS = [
    # ── bash_guard: allowed ────────────────────────────────────────────────
    dict(name="bg-allow-plain", hook="bash_guard", stdin=B),
    dict(name="bg-allow-quoted-heredoc", hook="bash_guard",
         stdin=J({"tool_name": "Bash", "tool_input": {"command": "cat <<'EOF'\n`id`\nEOF"}})),
    # ── bash_guard: blocked (exit 2 + DENY message) ───────────────────────
    dict(name="bg-block-unquoted-heredoc", hook="bash_guard",
         stdin=J({"tool_name": "Bash", "tool_input": {"command": "cat <<EOF\n`id`\nEOF"}})),
    dict(name="bg-block-claude-bg", hook="bash_guard",
         stdin=J({"tool_name": "Bash", "tool_input": {"command": 'claude --bg "run $(id) now"'}})),
    dict(name="bg-block-claude-print", hook="bash_guard",
         stdin=J({"tool_name": "Bash", "tool_input": {"command": 'claude -p "run `id` now"'}})),
    # ── bash_guard: empty / malformed / non-Bash ──────────────────────────
    dict(name="bg-empty-stdin", hook="bash_guard", stdin=""),
    dict(name="bg-malformed-brace", hook="bash_guard", stdin="{not json"),
    dict(name="bg-malformed-array", hook="bash_guard", stdin="[1, 2]"),
    dict(name="bg-malformed-null", hook="bash_guard", stdin="null"),
    dict(name="bg-non-bash-tool", hook="bash_guard",
         stdin=J({"tool_name": "Write", "tool_input": {"file_path": "x", "content": "y"}})),
    dict(name="bg-bash-missing-tool-input", hook="bash_guard",
         stdin=J({"tool_name": "Bash"})),
    dict(name="bg-bash-nonstring-command", hook="bash_guard",
         stdin=J({"tool_name": "Bash", "tool_input": {"command": 123}})),
    # ── fleet_report (Stop): silent on every path, state left to the side ─
    dict(name="fr-stop-no-launch-record", hook="fleet_report",
         files={"t.jsonl": _assistant("DONE x")},
         stdin=J({"session_id": "fake-child", "transcript_path": "{TMP}/t.jsonl"})),
    dict(name="fr-stop-files-status-line", hook="fleet_report",
         files={"t.jsonl": _assistant("DONE x"), LAUNCH: LAUNCH_RECORD},
         stdin=J({"session_id": "fake-child", "transcript_path": "{TMP}/t.jsonl"})),
    dict(name="fr-stop-no-status-keyword", hook="fleet_report",
         files={"t.jsonl": _assistant("still working on it"), LAUNCH: LAUNCH_RECORD},
         stdin=J({"session_id": "fake-child", "transcript_path": "{TMP}/t.jsonl"})),
    dict(name="fr-stop-malformed-stdin", hook="fleet_report", stdin="{not json"),
    dict(name="fr-stop-unsafe-session-id", hook="fleet_report",
         files={"t.jsonl": _assistant("DONE x")},
         stdin=J({"session_id": "../evil", "transcript_path": "{TMP}/t.jsonl"})),
    # ── fleet_inject (UserPromptSubmit / PostToolUse) ─────────────────────
    dict(name="fi-ups-empty-inbox", hook="fleet_inject",
         env={"FLEET_SESSION_NAME": "fake-parent"},
         stdin=J({"session_id": "fake-parent", "hook_event_name": "UserPromptSubmit"})),
    dict(name="fi-ptu-empty-inbox", hook="fleet_inject",
         env={"FLEET_SESSION_NAME": "fake-parent"},
         stdin=J({"session_id": "fake-parent", "hook_event_name": "PostToolUse"})),
    dict(name="fi-ups-seeded-inbox", hook="fleet_inject",
         env={"FLEET_SESSION_NAME": "fake-parent"},
         files={FLEET_STATE + "/inbox/fake-parent.jsonl": _INBOX_TWO},
         stdin=J({"session_id": "fake-parent", "hook_event_name": "UserPromptSubmit"})),
    dict(name="fi-ptu-cursor-offset", hook="fleet_inject",
         env={"FLEET_SESSION_NAME": "fake-parent"},
         files={FLEET_STATE + "/inbox/fake-parent.jsonl": _INBOX_A + _INBOX_B,
                FLEET_STATE + "/inbox/.cursor/fake-parent": str(len(_INBOX_A))},
         stdin=J({"session_id": "fake-parent", "hook_event_name": "PostToolUse"})),
    dict(name="fi-malformed-stdin", hook="fleet_inject",
         env={"FLEET_SESSION_NAME": "fake-parent"}, stdin="garbage"),
    dict(name="fi-unsafe-session-name", hook="fleet_inject",
         env={"FLEET_SESSION_NAME": "../evil"},
         stdin=J({"session_id": "../evil", "hook_event_name": "UserPromptSubmit"})),
    # ── context_guard (PostToolUse): silent fail-open paths ───────────────
    dict(name="cg-below-handoff-threshold", hook="context_guard",
         files={"t.jsonl": _assistant("hi", _usage(1000))}, stdin=CTX_STDIN),
    dict(name="cg-malformed-stdin", hook="context_guard", stdin="oops"),
    dict(name="cg-missing-transcript-path", hook="context_guard",
         stdin=J({"session_id": "fake-session", "cwd": "/work"})),
    dict(name="cg-no-usage-in-tail", hook="context_guard",
         files={"t.jsonl": _assistant("hi")}, stdin=CTX_STDIN),
    dict(name="cg-nonnumeric-usage", hook="context_guard",
         files={"t.jsonl": _assistant("hi", {"input_tokens": "x"})}, stdin=CTX_STDIN),
    dict(name="cg-unsafe-session-id", hook="context_guard",
         files={"t.jsonl": _assistant("hi", _usage(280000))},
         stdin=J({"transcript_path": "{TMP}/t.jsonl", "session_id": "../evil",
                  "cwd": "/work"})),
    # ── declared skipped: host-dependent output (see module docstring) ────
    dict(name="cg-handoff-emit", hook="context_guard",
         files={"t.jsonl": _assistant("hi", _usage(280000))}, stdin=CTX_STDIN,
         skip_reason=CG_EMIT),
    dict(name="cg-hard-emit", hook="context_guard",
         files={"t.jsonl": _assistant("hi", _usage(310000))}, stdin=CTX_STDIN,
         skip_reason=CG_EMIT),
]


def run_scenario(hook_dir, scenario, tmp):
    """`(stdout, stderr, exit)` of one scenario against `hook_dir`, fresh HOME.

    `tmp` is a scratch directory; the temp HOME (and therefore the state) is
    created empty inside it, then only the scenario's fixtures are written.
    """
    home = Path(tmp) / "home"
    home.mkdir(parents=True)

    def sub(text):
        return text.replace("{TMP}", str(home))

    for rel, content in sorted(scenario.get("files", {}).items()):
        target = home / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(sub(content), encoding="utf-8")
    env = {"HOME": str(home)}
    env.update({key: sub(value) for key, value in scenario.get("env", {}).items()})
    script = Path(hook_dir) / ("%s.py" % scenario["hook"])
    proc = subprocess.run([sys.executable, str(script)],
                          input=sub(scenario.get("stdin", "")).encode("utf-8"),
                          capture_output=True, env=env, timeout=30)
    return proc.stdout, proc.stderr, proc.returncode


def _result(stdout, stderr, code):
    return {"stdout": stdout.decode("utf-8"), "stderr": stderr.decode("utf-8"),
            "exit": code}


def _bare(scenario):
    """The scenario's identity and input fields, without any captured output."""
    return {key: scenario[key] for key in ("name", "hook", "env", "files", "stdin")
            if key in scenario}


def load_corpus():
    """Read ``parity_payloads.json`` with its scrub placeholders resolved.

    The committed corpus stores ``{TOK}`` / ``{HOME}`` in place of the literal
    fake token and AutoOS checkout path so the SOURCE carries no matched
    literal; the runtime values are restored here before replay, byte for byte.
    """
    text = CORPUS.read_text(encoding="utf-8")
    text = text.replace("{TOK}", FAKE_TOKEN).replace("{HOME}", AUTOOS_HOME)
    return json.loads(text)


def capture():
    """Regenerate `parity_payloads.json` from the ORIGINAL AutoOS hooks."""
    if not ORIGINAL_HOOKS.is_dir():
        raise SystemExit("original hooks not found at %s" % ORIGINAL_HOOKS)
    payloads, skipped = [], []
    for scenario in SCENARIOS:
        with tempfile.TemporaryDirectory() as tmp_a, tempfile.TemporaryDirectory() as tmp_b:
            copy_dir = Path(tmp_b) / "hooks"
            copy_dir.mkdir()
            for py in sorted(ORIGINAL_HOOKS.glob("*.py")):
                shutil.copy2(py, copy_dir / py.name)
            run_a = run_scenario(ORIGINAL_HOOKS, scenario, tmp_a)
            run_b = run_scenario(copy_dir, scenario, tmp_b)
        entry = _bare(scenario)
        if scenario.get("skip_reason"):
            entry["reason"] = scenario["skip_reason"]
            entry["run_a"] = _result(*run_a)
            entry["run_b"] = _result(*run_b)
            skipped.append(entry)
        elif run_a != run_b:
            entry["reason"] = ("output differs between two runs (time/pid/host "
                               "dependent); not comparable")
            entry["run_a"] = _result(*run_a)
            entry["run_b"] = _result(*run_b)
            skipped.append(entry)
        else:
            entry.update(_result(*run_a))
            payloads.append(entry)
    corpus = {
        "recorded_with": "the ORIGINAL hooks at %s, run in a temp HOME with "
                         "empty state (only the scenario fixtures present)" % ORIGINAL_HOOKS,
        "protocol": "each scenario ran twice (original hook dir and a copy in "
                    "a second temp dir, two temp HOMEs); only byte-stable runs "
                    "are recorded, expected output is run A",
        "placeholder": "{TMP} = the run's temp HOME directory",
        "payloads": payloads,
        "skipped": skipped,
    }
    CORPUS.write_text(json.dumps(corpus, indent=1).replace(FAKE_TOKEN, "{TOK}")
                      .replace(AUTOOS_HOME, "{HOME}") + "\n", encoding="utf-8")
    print("recorded %d payloads, skipped %d -> %s"
          % (len(payloads), len(skipped), CORPUS))


if __name__ == "__main__":
    capture()
