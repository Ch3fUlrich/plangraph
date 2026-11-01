#!/usr/bin/env python3
"""Shared helpers for the ported bash-guard hook suites (tests/hooks/).

Split out of the two single-file originals (``tests/test_bash_guard.py`` and
``tests/test_opencode_bash_guard.py``) so every test file stays small: the
subprocess runners for the PreToolUse JSON contract, the per-row test factory
for the CASES tables, and the Node harness that drives the OpenCode bash-guard
plugin - every helper moved verbatim from the originals.

The hook is located relative to the repo root - two levels above this file
(``tests/hooks/_helpers.py``) - at ``tools/hooks/bash_guard.py``.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HOOK = ROOT / "tools" / "hooks" / "bash_guard.py"
PLUGIN_PATH = ROOT / "configuration" / "opencode" / "plugins" / "bash-guard" / "index.mjs"
NODE_BIN = shutil.which("node")

# the marker and the incident shape are the canary's contract, not a copy
sys.path.insert(0, str(ROOT / "tools"))
try:
    from oc_l1_canary import DENIED_MARKER, INCIDENT_COMMAND  # noqa: F401
except ImportError:
    # tools/oc_l1_canary.py is not in this repo yet; pin the same contract
    # values so the suites still collect. The real module wins when present.
    DENIED_MARKER = "bash-guard: DENIED"
    INCIDENT_COMMAND = "cat <<CANARY_EOF\ncanary `date`\nCANARY_EOF"


# --- PreToolUse hook subprocess runners (test_bash_guard.py) ---------------
def run_hook_raw(stdin_text):
    return subprocess.run([sys.executable, str(HOOK)], input=stdin_text,
                           capture_output=True, text=True)


def run_hook_bytes(stdin_bytes):
    return subprocess.run([sys.executable, str(HOOK)], input=stdin_bytes,
                           capture_output=True)


def run_hook(payload):
    return run_hook_raw(json.dumps(payload))


def run_bash(command):
    return run_hook({"tool_name": "Bash", "tool_input": {"command": command}})


# --- one generated test per CASES row (test_bash_guard.py) -----------------
def _make_case_test(command, expect_deny, expect_substr):
    def test(self):
        r = run_bash(command)
        if expect_deny:
            self.assertEqual(r.returncode, 2,
                              "command=%r stdout=%r stderr=%r"
                              % (command, r.stdout, r.stderr))
            if expect_substr:
                self.assertIn(expect_substr, r.stderr,
                               "stderr=%r" % r.stderr)
        else:
            self.assertEqual(r.returncode, 0,
                              "command=%r stdout=%r stderr=%r"
                              % (command, r.stdout, r.stderr))
            self.assertEqual(r.stderr, "",
                             "command=%r stderr=%r" % (command, r.stderr))
    return test


# --- OpenCode bash-guard plugin harness (test_opencode_bash_guard.py) ------
HARNESS_MJS = """\
import { pathToFileURL } from 'node:url';

let inputStr = '';
process.stdin.setEncoding('utf8');
for await (const chunk of process.stdin) {
  inputStr += chunk;
}

const req = JSON.parse(inputStr);
const pluginModule = await import(req.pluginUrl);
const plugin = pluginModule.default;

let handler = null;
const fakeCtx = {
  tool: {
    hook: async (hookName, fn) => {
      if (hookName === 'execute.before') {
        handler = fn;
      }
    }
  }
};

await plugin.setup(fakeCtx);

if (!handler) {
  process.stderr.write('bash-guard: no handler registered\\n');
  process.exit(10);
}

try {
  await handler(req.event);
  process.stdout.write(JSON.stringify({ allowed: true, error: null }));
} catch (err) {
  process.stdout.write(JSON.stringify({ allowed: false, error: err?.message || String(err) }));
}
"""


def run_plugin(event, env_overrides=None, plugin_path=None, timeout=20, keep_location=False,
               cwd=None):
    if not NODE_BIN:
        raise unittest.SkipTest("node is not installed")

    target_plugin = Path(plugin_path) if plugin_path else PLUGIN_PATH
    if not target_plugin.exists():
        # no subject to drive in this repo (yet): same skip shape as NODE_BIN
        raise unittest.SkipTest("bash-guard plugin missing at %s" % target_plugin)
    with tempfile.TemporaryDirectory() as tmpdir:
        # import a copy named .mjs: older node (18) refuses ES-module syntax in a .js file without a package.json type
        if keep_location:
            plugin_url = target_plugin.resolve().as_uri()
        else:
            plugin_copy = Path(tmpdir) / "bash-guard.mjs"
            plugin_copy.write_text(target_plugin.read_text(encoding="utf-8"), encoding="utf-8")
            plugin_url = plugin_copy.resolve().as_uri()
        harness_file = Path(tmpdir) / "harness.mjs"
        harness_file.write_text(HARNESS_MJS, encoding="utf-8")

        env = os.environ.copy()
        env["AUTOOS_REPO_ROOT"] = str(ROOT)  # the imported copy lives in a temp dir, so point at the real checkout
        if env_overrides:
            env.update(env_overrides)
            # a None value removes the key entirely (tests that must prove
            # behaviour with the variable unset, e.g. AUTOOS_GUARD_ROLE)
            for key in [k for k, v in env_overrides.items() if v is None]:
                env.pop(key, None)

        payload = json.dumps({
            "pluginUrl": plugin_url,
            "event": event,
        })

        t0 = time.time()
        proc = subprocess.run(
            [NODE_BIN, str(harness_file)],
            input=payload,
            capture_output=True,
            text=True,
            env=env,
            timeout=timeout,
            cwd=cwd,
        )
        elapsed = time.time() - t0

        try:
            data = json.loads(proc.stdout)
        except Exception:
            raise AssertionError(
                f"Harness did not produce valid JSON. code={proc.returncode}, "
                f"stdout={proc.stdout!r}, stderr={proc.stderr!r}"
            )

        return {
            "allowed": data["allowed"],
            "error": data.get("error"),
            "stderr": proc.stderr,
            "stdout": proc.stdout,
            "returncode": proc.returncode,
            "elapsed": elapsed,
        }


class L2RoleChecks:
    """AUTOOS_GUARD_ROLE=l2 role env and assert helpers, shared by the two
    TestL2ReadOnlyRole halves (test_opencode_bash_guard_b.py and _c.py);
    moved verbatim from the original class body."""

    L2_ENV = {"AUTOOS_GUARD_ROLE": "l2"}

    def _l2_denied(self, cmd, *needles):
        res = run_plugin({"tool": "shell", "input": {"command": cmd}},
                         env_overrides=dict(self.L2_ENV))
        self.assertFalse(res["allowed"], f"l2 role must deny: {cmd!r} (error={res['error']})")
        self.assertIsNotNone(res["error"])
        self.assertTrue(res["error"].strip().startswith(DENIED_MARKER),
                        "the canary matches the marker at the start only: %r" % res["error"])
        self.assertIn("l2 read-only", res["error"], cmd)
        for needle in needles:
            self.assertIn(needle, res["error"].lower(), cmd)

    def _l2_allowed(self, cmd):
        res = run_plugin({"tool": "shell", "input": {"command": cmd}},
                         env_overrides=dict(self.L2_ENV))
        self.assertTrue(res["allowed"], f"l2 role must allow: {cmd!r} (error={res['error']})")
        self.assertIsNone(res["error"])
