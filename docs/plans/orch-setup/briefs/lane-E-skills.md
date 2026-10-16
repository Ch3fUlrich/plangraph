# Lane E: skills + harness client config (plangraph-L2-su-skills)

STANDING RULES: `docs/plans/orch-setup/briefs/COMMON.md` (binding, read first). Design sections: 2, 6 (Skills, Harness client
config).
Items, in order:
- `plangraph:SU-SKILLS` (starts once SU-CORE is merged);
- `plangraph:SU-HARNESS-CFG` (waits for SU-ENV and `plangraph:RT-P1-WORKER`);
- `autoos:SU-SKILLS-CALLER` (waits for `autoos:SU-CATALOG`).

## READ (AutoOS at origin/main)
- `AGENTS.md` section 8 (per-client skill dirs).
- `lib/windows/AutoOS.Install.psm1`: `Install-AutoOSAgentSkills` (about line 1440), `Get-AutoOSSkillsSource` (about line
  2182), `Sync-AutoOSSkillDirs` (about line 2746), `Sync-AutoOSAgentSkillTargets` (about line 2915).
- `lib/linux/install.sh`: `install_agent_skill_links` (about line 5103), `link_skill_dirs` (about line 6009),
  `agent_skill_links_current`.
- `lib/agent_harness.py` + `catalog/agent-harness.json` (opencode config generation).
- Tests: `run-tests.ps1` around line 1944 and the Linux skill-link tests.

## WRITE SCOPE
- plangraph: `rust/plangraph-cli/src/setup/{skills,harness}.rs` + tests + `[skills]` and `[harness]` manifest sections.
- AutoOS: the four skill functions above become callers of `plangraph setup skills` (keep their names and exit semantics),
  plus `lib/agent_harness.py`'s opencode output path (only for SU-HARNESS-CFG, if the worker adapters need it).

## SU-SKILLS spec
- Targets from the manifest:
  - Claude Code `~/.claude/skills` (+ project `.claude/skills` via `setup repo`);
  - Antigravity `~/.gemini/config/skills`;
  - gemini/qoder/qwen `~/.agents/skills`, qwen also `~/.qwen/skills`;
  - codex `~/.codex/skills` (only if codex is installed);
  - OpenHands `~/.agents/skills` + `~/.openhands/skills`.
- Source: `PLANGRAPH_SKILLS_SRC`.
- One link per skill. Rules as `link_skill_dirs`: absent -> link; ours or dangling -> repoint; anything else -> keep and report
  `refused foreign`.
- Windows junctions (no admin), Linux symlinks. `check` = drift.

## SU-HARNESS-CFG spec
For each gateway harness in the manifest (opencode first), merge the provider block pointing at `OMNIROUTE_URL`, with the key
by file path only. Model choice per project/card stays keeper-side routing (base design section 5); setup writes connection
only. Covered by drift check and merge tests.

## SU-SKILLS-CALLER spec
AutoOS functions call the binary (path from the catalog install); their behaviour and tests stay green; no logic is left
duplicated in AutoOS. Public repo: no site strings.
