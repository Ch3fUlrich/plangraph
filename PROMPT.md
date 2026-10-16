You have NO tools and no repository access. The full material is inline below. Answer in plain text; never emit tool calls.

Review one small change to the plangraph repo (plan item PROJECTOR-RECONCILE-SCHED). Goal ("done means"): a systemd service + timer in deploy/systemd/ runs `python -m plangraph.projector reconcile` nightly on the keeper host (no agent cron); install/enable docs next to the existing units; a reconcile mismatch raises a row in projector_alarms; docs/projector.md "Operations" names the timer; a unit-file lint/parse test. Ships files and docs only, no live install. Context facts: the CLI takes --config BEFORE the subcommand (argparse global option); `reconcile` prints one JSON line and exits 0 even when alarms were raised, 1 on runtime failure, 2 on bad config; the existing keeper unit (not in diff) uses the same WorkingDirectory/PYTHONPATH/keeper.json paths; existing tests/test_projector_reconcile.py (not in diff) already covers raising/dedupe/clearing of projector_alarms rows.

Vectors:
V1 Unit correctness: will the service+timer pair actually work as a systemd USER unit pair (oneshot, After=, EnvironmentFile=-, TimeoutStartSec, OnCalendar syntax, Persistent, Unit=, Install target, %h specifier use)? Any directive that is invalid or misleading.
V2 CLI fit: does ExecStart match the real CLI shape and exit-code semantics described above; would a mismatch or a failure be surfaced correctly?
V3 Test quality: does tests/test_systemd_units.py actually fail when a unit regresses (wrong subcommand, flag order, missing Persistent, cron mention, home path), or can it pass vacuously? Any Windows/Linux portability trap?
V4 Docs accuracy: README.md and the docs/projector.md Operations bullet vs the units (times, names, commands, claims such as who reads projector_alarms).
V5 Public-repo hygiene: hosts, IPs, user names, home paths, token-shaped strings.

NOTE: this is a re-review. A previous pass found README.md truncated (unclosed fence) by an extraction bug; it is fixed in this head. Check it and the rest again.

Diff (git diff base..head):
diff --git a/deploy/systemd/README.md b/deploy/systemd/README.md
new file mode 100644
index 0000000..b161140
--- /dev/null
+++ b/deploy/systemd/README.md
@@ -0,0 +1,50 @@
+# plangraph systemd units (user units)
+
+All units are user units (`systemctl --user`), not system units.
+
+## 1. Files
+
+| File | Role | Host |
+| --- | --- | --- |
+| pg-snapshot-pull.service | reader-host local plan copy refresh every 5 min | reader host |
+| pg-snapshot-pull.timer | timer driving pg-snapshot-pull.service every 5 min | reader host |
+| plangraph-keeper.service | persistent keeper daemon | keeper host |
+| plangraph-projector-reconcile.service | one nightly run: reconcile open Temporal workflows vs keeper state | keeper host |
+| plangraph-projector-reconcile.timer | nightly timer (03:30) starting the reconcile service | keeper host |
+
+## 2. Install (keeper host)
+
+```sh
+mkdir -p ~/.config/systemd/user
+cp plangraph-projector-reconcile.service plangraph-projector-reconcile.timer ~/.config/systemd/user/
+systemctl --user daemon-reload
+systemctl --user enable --now plangraph-projector-reconcile.timer
+loginctl enable-linger "$USER"
+```
+
+Linger makes the timer fire with no login session. A user timer cannot wake a suspended host; `Persistent=true` runs a missed
+nightly pass at the next start.
+
+## 3. Verify
+
+```sh
+systemctl --user list-timers plangraph-projector-reconcile.timer
+systemctl --user start plangraph-projector-reconcile.service
+journalctl --user -u plangraph-projector-reconcile.service -n 20
+```
+
+The last stdout line is one JSON object with `raised`, `cleared`, `open_alarms`, `counts`.
+
+## 4. Paths
+
+- Edit `WorkingDirectory`/`PYTHONPATH` if the checkout lives elsewhere.
+- `keeper.json` is the same file the keeper daemon reads.
+- Optional `projector.env` (`%h/.config/plangraph/projector.env`) for `TEMPORAL_ADDRESS`/`TEMPORAL_NAMESPACE`.
+
+## 5. Alarms
+
+A mismatch becomes an open row in `projector_alarms` (`cleared_at IS NULL`); the next pass clears it when the condition is gone. See `docs/projector.md` "Operations".
+
+## 6. Deploy note
+
+This lane ships files only; install on the keeper host is a separate, gated deploy step.
diff --git a/deploy/systemd/plangraph-projector-reconcile.service b/deploy/systemd/plangraph-projector-reconcile.service
new file mode 100644
index 0000000..e0f40b7
--- /dev/null
+++ b/deploy/systemd/plangraph-projector-reconcile.service
@@ -0,0 +1,12 @@
+[Unit]
+Description=plangraph projector: nightly reconcile of open Temporal workflows vs keeper state (writes projector_alarms)
+After=network-online.target plangraph-keeper.service
+
+[Service]
+Type=oneshot
+WorkingDirectory=%h/code/plangraph/.claude/worktrees/proposal
+Environment=PYTHONPATH=%h/code/plangraph/.claude/worktrees/proposal/src
+EnvironmentFile=-%h/.config/plangraph/projector.env
+ExecStart=/usr/bin/python3 -m plangraph.projector --config %h/.local/share/plangraph/keeper.json reconcile
+TimeoutStartSec=15min
+# exit 0 even when alarms were raised (they are rows in projector_alarms); non-zero only on a runtime/config failure.
diff --git a/deploy/systemd/plangraph-projector-reconcile.timer b/deploy/systemd/plangraph-projector-reconcile.timer
new file mode 100644
index 0000000..95bdaf6
--- /dev/null
+++ b/deploy/systemd/plangraph-projector-reconcile.timer
@@ -0,0 +1,11 @@
+[Unit]
+Description=plangraph projector: nightly reconcile (systemd timer, D-1377)
+
+[Timer]
+OnCalendar=*-*-* 03:30:00
+RandomizedDelaySec=10min
+Persistent=true
+Unit=plangraph-projector-reconcile.service
+
+[Install]
+WantedBy=timers.target
diff --git a/docs/projector.md b/docs/projector.md
index 5961662..8639bf9 100644
--- a/docs/projector.md
+++ b/docs/projector.md
@@ -111,7 +111,12 @@ or PATH; skipped when missing) and runs real workflows. Migration test: upgrade
 
 * Exactly one projector process per keeper (`run` or a scheduled `run-once`). The ledger primary key makes a second process lose
   loudly (an integrity error or a `conflict` refusal) rather than corrupt rows; it is not a supported mode.
-* Nightly reconcile: schedule `python -m plangraph.projector reconcile` (cron on the keeper host or a Temporal schedule); the
-  Inbox reads open rows of `projector_alarms`. The projector never sets `done`/`cancelled`: closing a card stays a keeper verb.
+* Nightly reconcile runs as the systemd user timer `plangraph-projector-reconcile.timer` (03:30 local, up to 10 min random
+  delay, Persistent=true) which starts the oneshot `plangraph-projector-reconcile.service`; that runs
+  `python -m plangraph.projector --config <keeper.json> reconcile`. Units and install/enable steps: `deploy/systemd/`
+  (README.md there). No agent cron. The command exits 0 even when it raised alarms (a mismatch is an open row of
+  `projector_alarms`, `cleared_at IS NULL`, that the next pass clears when the condition is gone; read it with
+  `plangraph.projector.reconcile.open_alarms`); it exits 1 only on a runtime failure and 2 on bad config, which fails the
+  unit. The projector never sets `done`/`cancelled`: closing a card stays a keeper verb.
 * Attempts have no dedicated keeper event type; `state_set` events (written by `Store.set_state`) are the keeper events the
   projector produces, the attempt rows themselves are the append-only record of each attempt.
diff --git a/tests/test_systemd_units.py b/tests/test_systemd_units.py
new file mode 100644
index 0000000..2be27e6
--- /dev/null
+++ b/tests/test_systemd_units.py
@@ -0,0 +1,97 @@
+"""Lint deploy/systemd/plangraph-projector-reconcile units against the CLI and docs; see docs/projector.md Operations."""
+
+import configparser
+import re
+import shlex
+from pathlib import Path
+
+from plangraph.projector import cli
+
+ROOT = Path(__file__).resolve().parents[1]
+UNITS = ROOT / "deploy" / "systemd"
+SERVICE = UNITS / "plangraph-projector-reconcile.service"
+TIMER = UNITS / "plangraph-projector-reconcile.timer"
+IPV4 = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
+
+
+def read(path):
+    assert path.is_file(), f"missing file: {path}"
+    return path.read_text(encoding="utf-8")
+
+
+def parse(path):
+    parser = configparser.ConfigParser(strict=False, interpolation=None)
+    parser.optionxform = str
+    parser.read_string(read(path), source=str(path))
+    return parser
+
+
+def section(path, heading):
+    lines = read(path).splitlines()
+    start = next((i for i, line in enumerate(lines) if line.startswith(heading)), None)
+    assert start is not None, f"{path.name} lacks a {heading!r} heading"
+    stop = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
+    return "\n".join(lines[start + 1:stop])
+
+
+def test_unit_structure():
+    svc, tim = parse(SERVICE), parse(TIMER)
+    assert svc.get("Service", "Type", fallback="") == "oneshot", "service must be Type=oneshot"
+    assert not svc.has_section("Install"), "service must not ship an [Install] section"
+    assert tim.get("Install", "WantedBy", fallback="") == "timers.target", "timer must be WantedBy timers.target"
+
+
+def test_execstart_matches_cli():
+    lines = read(SERVICE).splitlines()
+    assert sum(line.startswith("ExecStart=") for line in lines) == 1, "exactly one ExecStart (strict=False keeps the last)"
+    tokens = shlex.split(parse(SERVICE).get("Service", "ExecStart"), posix=True)
+    assert tokens[0] == "/usr/bin/python3", tokens
+    assert tokens[1:3] == ["-m", "plangraph.projector"], tokens
+    assert "--history-dir" not in tokens, "unit must not pass --history-dir"
+    ns = cli._build_parser().parse_args(tokens[3:])
+    assert ns.command == "reconcile", ns
+    assert ns.config == "%h/.local/share/plangraph/keeper.json", ns
+
+
+def test_timer_schedule():
+    tim, oncal = parse(TIMER), parse(TIMER).get("Timer", "OnCalendar", fallback="")
+    assert tim.get("Timer", "Unit", fallback="") == SERVICE.name, "timer must activate the service unit"
+    assert oncal and "03:30" in oncal, f"OnCalendar must be non-empty and nightly: {oncal!r}"
+    assert tim.get("Timer", "Persistent", fallback="") == "true", "Persistent must be true"
+    assert tim.has_option("Timer", "RandomizedDelaySec"), "RandomizedDelaySec must be set"
+
+
+def test_no_machine_specific_paths():
+    for path in (SERVICE, TIMER):
+        cfg = parse(path)
+        for number, line in enumerate(read(path).splitlines(), 1):
+            where = f"{path.name}:{number}"
+            assert "/home/" not in line and "/Users/" not in line, f"{where} hard-codes a home directory"
+            for ip in IPV4.findall(line):
+                assert ip == "127.0.0.1", f"{where} hard-codes {ip}"
+            if not line.lstrip().startswith("#"):
+                assert "cron" not in line.lower(), f"{where} mentions cron"
+        for sec in cfg.sections():
+            for key in ("ExecStart", "WorkingDirectory", "Environment", "EnvironmentFile"):
+                if not cfg.has_option(sec, key):
+                    continue
+                value = cfg.get(sec, key).lstrip("-")
+                if value.startswith("PYTHONPATH="):
+                    value = value.split("=", 1)[1]
+                for token in shlex.split(value, posix=True):
+                    if token.startswith("/") and token != "/usr/bin/python3":
+                        assert token.startswith("%h"), f"{path.name} {key} leaks {token}"
+
+
+def test_docs_operations_section():
+    body = section(ROOT / "docs" / "projector.md", "## Operations")
+    assert TIMER.name in body, "Operations must document the timer unit"
+    assert "deploy/systemd" in body, "Operations must reference deploy/systemd"
+
+
+def test_systemd_readme():
+    body = read(UNITS / "README.md")
+    assert SERVICE.name in body, "README must mention the service unit"
+    assert TIMER.name in body, "README must mention the timer unit"
+    assert "enable --now" in body, "README must show enable --now"
+    assert body.count("```") % 2 == 0, "README has an unclosed code fence"


=== END OF MATERIAL. FINAL REMINDER ON ANSWER SHAPE
1. Answer each vector V1..Vn under its own heading with file:function evidence.
2. Then answer the standing question below as ONE plain line starting at column 0, no bold, no trailing period. For "no" the line MUST end with the literal text ": 0 hits" (the parser rejects any other ending), e.g.
TEST-GAMING: no - searched tests/ and src/ for test-only branches and env checks: 0 hits
3. The very last line is plain, no bold:
VERDICT: pass
(or VERDICT: fix - <findings by severity> / VERDICT: halt - <why>)
