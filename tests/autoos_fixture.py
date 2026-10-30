#!/usr/bin/env python3
"""Vendored AutoOS data for the routing-registry skill guards.

The skill documents AutoOS code (tools/registry.py, the OmniRoute appliers)
that stays in AutoOS until AUTOOS-INTO-PLANGRAPH moves it. CI has no AutoOS
checkout, so the guards read a copy of exactly what they check:
tests/fixtures/autoos/routing_registry.json holds apply.sh's argument loop,
apply.ps1's param() block, every `registry.py render [<target>] --help` text,
and the skill's script paths that exist in AutoOS. With PLANGRAPH_AUTOOS_ROOT
set, the guards read the checkout instead and a drift test compares the two.

Refresh after the skill or the AutoOS tools change:

    PLANGRAPH_AUTOOS_ROOT=~/code/AutoOS python3 tests/autoos_fixture.py
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

FIXTURE = (Path(__file__).resolve().parent / "fixtures" / "autoos"
           / "routing_registry.json")
_SH_LOOP_RE = re.compile(r"(?ms)^while \[\[ \$# -gt 0 \]\]; do\n.*?^done$")


def sh_argloop(text):
    """apply.sh's top-level argument loop, verbatim ('' if reshaped)."""
    m = _SH_LOOP_RE.search(text)
    return m.group(0) if m else ""


def _ps1_code(text):
    """Per character of PowerShell `text`: True if it is code, False inside a
    '...'/"..." string (doubled quote and "`" escapes), an @'..'@ / @".."@
    here-string or a # / <# #> comment (so their parens never count)."""
    code, i, n = [True] * len(text), 0, len(text)
    while i < n:
        c, end = text[i], None
        if text.startswith("<#", i):
            end = text.find("#>", i + 2)
            end = n if end < 0 else end + 2
        elif text.startswith(("@'", '@"'), i):  # here-string, closes on \n'@
            end = text.find("\n" + text[i + 1] + "@", i + 2)
            end = n if end < 0 else end + 3
        elif c == "#":
            end = text.find("\n", i)
            end = n if end < 0 else end
        elif c in "'\"":
            j = i + 1
            while j < n:
                if c == '"' and text[j] == "`":
                    j += 2
                    continue
                if text[j] == c:
                    if text.startswith(c * 2, j):  # '' / "" is an escape
                        j += 2
                        continue
                    break
                j += 1
            end = min(j + 1, n)
        if end is None:
            i += 1
            continue
        code[i:end] = [False] * (end - i)
        i = end
    return code


def _code_find(text, code, needle, start=0):
    i = text.find(needle, start)
    while i >= 0 and not code[i]:
        i = text.find(needle, i + 1)
    return i


def strip_ps1(text):
    """`text` with strings and comments blanked (newlines kept)."""
    return "".join(c if ok or c == "\n" else " "
                   for c, ok in zip(text, _ps1_code(text)))


def ps1_param(text):
    """apply.ps1 from [CmdletBinding()] through its param() close, verbatim;
    parens inside strings and comments do not count ('' if reshaped)."""
    code = _ps1_code(text)
    head = _code_find(text, code, "[CmdletBinding()]")
    i = _code_find(text, code, "param(", head) if head >= 0 else -1
    if i < 0:
        return ""
    depth, i = 1, i + len("param(")
    while depth and i < len(text):
        if code[i]:
            depth += (text[i] == "(") - (text[i] == ")")
        i += 1
    return text[head:i] if depth == 0 else ""


def render_help(autoos, *args):
    """`registry.py render <args> --help` stdout from an AutoOS checkout."""
    run = subprocess.run(
        [sys.executable, "-B", str(autoos / "tools" / "registry.py"),
         "render", *args, "--help"],
        cwd=str(autoos), capture_output=True, encoding="utf-8",
        errors="replace", env=dict(os.environ, COLUMNS="80",
                                   PYTHONDONTWRITEBYTECODE="1"))
    # argparse defaults print absolute paths: keep the host's out of the copy
    return run.stdout.replace(str(autoos), "<autoos>") \
        if run.returncode == 0 else ""


def build_live(autoos, script_paths):
    """The fixture content as read from the AutoOS checkout `autoos`."""
    omni = autoos / "configuration" / "omniroute"
    top = render_help(autoos)
    m = re.search(r"\{([\w,-]+)\}", re.sub(r"\s+", " ", top))
    targets = m.group(1).split(",") if m else []
    helps = {"": top}
    helps.update({t: render_help(autoos, t) for t in targets})
    return {
        "autoos_sha": subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(autoos),
            capture_output=True, encoding="utf-8").stdout.strip(),
        "apply_sh_argloop": sh_argloop(
            (omni / "apply.sh").read_text(encoding="utf-8")),
        "apply_ps1_param": ps1_param(
            (omni / "apply.ps1").read_text(encoding="utf-8")),
        "render_help": helps,
        "script_paths": sorted(s for s in script_paths
                               if (autoos / s).is_file()),
    }


def load():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


if __name__ == "__main__":
    from routing_registry_skill_lib import AUTOOS, _script_paths, _skill_files
    if AUTOOS is None:
        sys.exit("set PLANGRAPH_AUTOOS_ROOT to an AutoOS checkout")
    named = {s for p in _skill_files() for s in _script_paths(p)}
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE.write_text(json.dumps(build_live(AUTOOS, named), indent=1,
                                  sort_keys=True) + "\n", encoding="utf-8")
    print("wrote %s" % FIXTURE)
