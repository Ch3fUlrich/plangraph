"""Regenerate the public Windows record; refuse unsafe or oversized output."""
import os
import re
import subprocess
import sys
from pathlib import Path
MARKER = "<!-- generated Windows baseline -->"
PATHS = r"(?i)(?:[a-z]:[\\/](?:Users|home)[\\/]|/(?:[a-z]/Users|home|Users)/)[^\s/\\]+"
def generate(log, sha, output=Path("tests/windows_baseline.md")):
    text = Path(log).read_text(encoding="utf-8", errors="replace")
    old = output.read_text(encoding="utf-8") if output.exists() else subprocess.check_output(
        ["git", "show", "85980b70:tests/windows_baseline.md"]).decode()
    narrative = old.split(MARKER)[0].split("## Remaining failures/errors")[0]
    totals = re.findall(r"^.*\d+ failed,.* in [\d.]+s.*$", text, re.M)[-1].strip("= \r")
    narrative = re.sub(r"Base:.*?;", f"Base: {sha};", narrative)
    narrative = re.sub(r"Totals:.*", "Totals: " + totals + ".", narrative)
    narrative = re.sub(r"No hang reproduced.*?\(446.16s, 398.05s\)\.",
        "no hang in 3 runs, no bound added (439 s, 446 s, 398 s).", narrative, flags=re.S)
    narrative = re.sub(r"This run:.*\n", "", narrative)
    narrative = narrative.replace("- 40 cases: fake_ownership", "- 40 cases in this selection: fake_ownership")
    if "additional cases repo-wide" not in narrative:
        narrative = narrative.replace("  Excluded suites:", "  47 additional cases repo-wide (collection verified).\n  Excluded suites:")
    skipped = int(re.search(r"(\d+) skipped", totals)[1])
    narrative = re.sub(r"remaining \d+ skipped", f"remaining {skipped - 219} skipped", narrative)
    names = sorted(set(re.findall(r"^(?:FAILED|ERROR) (tests/.*?)(?: - .*|\r?)$", text, re.M)))
    names = [re.sub(r"\[.{200,}\]", "[parameter elided: oversized ID]", n) for n in names]
    result = narrative.rstrip() + f"\n\nThis run: finished; {totals}.\n\n" + MARKER + f"\nRecorded-at: {sha}\nTotals: {totals}\n"
    result += f"{len(names)} unique failure/error names; oversized parameter IDs are elided.\n"
    result += "".join(f"- {n} | owner: " + ("plangraph:SPAWNER-WIN-CANCEL-CHILD" if n.split("::")[0] in (
        "tests/test_spawn_cancel_d3505.py", "tests/test_spawner_cancel_cross_process.py") else "none yet") + "\n" for n in names)
    secrets = [os.environ.get(k, "") for k in ("USERNAME", "USER", "COMPUTERNAME", "HOSTNAME")]
    result = re.sub(PATHS, "<home>", result)
    for secret in filter(None, secrets):
        result = re.sub(re.escape(secret), "<redacted>", result, flags=re.I)
    if re.search(PATHS, result) or any(s.lower() in result.lower() for s in filter(None, secrets)) or len(result.encode()) > 40960:
        raise ValueError("unsafe or oversized baseline")
    output.write_bytes(result.encode("utf-8"))
if __name__ == "__main__":
    generate(*sys.argv[1:3], *([Path(sys.argv[3])] if len(sys.argv) > 3 else []))
