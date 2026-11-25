# Lean review harness

Claude reviews preserve permissions; built-ins remain Read/Grep/Glob/Bash for
tools and inline-only modes. Only prompt-only uses an empty `--tools` set.
Strict MCP uses packaged empty configuration; slash commands are disabled.
The Claude fallback review leg defaults to prompt-only. Existing inline-only
legs retain their meaning. Unknown harness/model pairs default to tools.

Gateway prompt-only reviews launch OpenCode standalone with a one-step agent
(tools are removed on its final step), deny-all permissions, and policies
denying tools, MCP, skills and plugins. `run` is non-interactive and does not
dispatch slash commands. Non-review commands retain their existing behavior.
The explicit config overlay is stored in a private temporary JSON file, not
argv or an `env` command. A portable Python launcher sets `OPENCODE_CONFIG`,
removes inherited `OPENCODE_CONFIG_CONTENT`, forwards prompt stdin and exit status,
and deletes the file after exit; unlaunched commands are cleaned at parent exit.
Config size does not grow the command line, including on Windows.
Every native or legacy harness command uses stdin for task content, and child
environments discard known inherited or overlaid legacy task-prompt carriers before
launch-spec serialization, while retaining unrelated prompt-template/theme settings; they also drop the parent-only gateway management token variables.
The [v1 registry](prompt-transport-v1.md) stages shared transport/reason data
and preserves the existing imports; S01 changes no launch behavior.

`tools/judge_prompt.py` assembles offline findings, diff, CI and whole file
bodies, prioritizing cited files under the byte budget. Its default prompt-only
brief says cut files are unavailable. `--tool-mode tools` permits read-only
follow-up; `inline-only` reads only the spawner-inlined diff, with no tools.
That brief does not advertise Claude's retained inline-only built-ins.
The offline config-schema test uses `jsonschema>=4` from `requirements-ci.txt`.

Live startup-token savings: not measured. No live measurement run IDs exist in
the repository's records, so a fixed 25k saving is not claimed.
Open step: three-run before/after token probe with recorded run IDs and token
counts for each run. Runner structure is unchanged.

Gateway URL resolution is shared by child configuration and response-side witness
reads: OMNIROUTE_BASE_URL, then PLANGRAPH_GATEWAY_URL / the existing gateway_url
config mapping, then plangraph/omniroute-base-url in XDG_CONFIG_HOME (else
HOME/.config). This file fallback supports plain-shell spawn CLI runs without
launcher exports. File read failures mean unconfigured; no URL or key is logged.

The witness endpoint now uses the same effective child-base resolver, including
an existing `OPENCODE_CONFIG_CONTENT` omniroute settings baseURL. File-derived
endpoints use the shared URL validator (no userinfo, query or fragment; HTTPS
except for loopback HTTP) and require a hostname; invalid files stay
unconfigured; `fetch_rows` refuses them before reading the management key or
making a call-log request.

Before attaching the management Bearer token, `fetch_rows` uses that same validator for every endpoint source, including env, config mappings and child
overlays. Invalid endpoints are refused before reading the key; file validation
remains in the shared resolver as well.

Management call-log requests refuse redirects, so the Bearer token cannot be
forwarded to another endpoint after the initial URL validation.

Required-witness preflight applies the management endpoint policy before reading
the key or launching a seat, so unusable endpoints refuse before work starts.

Direct CLI command rendering refuses operator-retired Gemini Flash targets before
constructing argv; D-5903 enables Hydrogen high for implement and medium for read-only tools review (agy plan mode), both unwitnessed.

Native agy reviews use plan mode for every Hydrogen effort, Neon (`gemini-4-neon-medium`) and Argon low and medium.
Hydrogen reviews use a private AGY home seeded with account state so the CLI skips the operator’s global MCP configuration.
The command passes session-only settings with a permission allow list (`command(*)` and the
read tools): headless agy denies a tool without an allow rule and then answers nothing, so a
reviewer that runs `git diff` or the tests needs the command rule. Sonnet high uses
Claude model claude-sonnet-5-5 with --effort high and restricted review tools.

CLI spawner reviews may record PASS only when the rendered review names a
changed path from the requested diff or an explicit file:line. Without a diff
base, paths come from the reviewed HEAD commit. A weak positive verdict retains
its text with `verdict` and `review_status` set to `no_verdict`, reason
`weak_verdict`, and no passing seat evidence; process completion is unchanged.
The CLI prompt forbids background commands and requires the reviewer to wait
for commands to finish. Native writer review files apply the same citation rule
to ACCEPT, recording review status `no_verdict` and reason `weak_verdict` (exit 4).

Rust `pg-spawnd` cards carrying a nonempty `done_criteria` now receive a
pre-review self-check prefix. It includes the complete criteria and all four
finding classes, ordered by their counts in the last 30 days of `.txt`/`.md` verdict files in
`card.review_dir` (relative to the launch checkout, default `reviews`). The scan
is nonrecursive, uses file modification times, ignores symlinks and files over
1 MiB, and requires a `VERDICT: ` line. HIGH/MED/P1/P2 finding lines are counted
by test/coverage, documentation, contract/cross-language, and other keywords;
ties use that order. Missing history contributes no finding classes.

The writer copies each generated id into its status note as
`SELF-CHECK <id>: <evidence>`. The review request must carry the same criteria,
history directory and that note in `card.writer_status`. Before launching the
reviewer, Rust refuses missing, blank, duplicate or stale criterion answers and
includes the checklist and answers in the reviewer prompt; the error names the
missing line text. Ids hash only the criteria and the fixed class lines. An answer is evidence
for the reviewer to verify, not an automatic claim that a criterion is met.
History changes between writer and review only reorder the classes, never the
ids. Review seat lines such as `FINDING: MED path - text` and `1. **HIGH**` are
counted; classes match the text after ` - `, not the path. Dry runs perform the same validation.

This first slice covers standard Rust-spawner launches and their review quota
fallbacks. Cards without criteria retain their behavior. Native-Codex lifecycle
briefs, the Python spawner and the shared Python brief renderer still need this
integration; they do not enforce this gate yet.

The `codex` client launch (`codex exec`) takes a registered gpt leg model as its plain id plus a
reasoning effort: `omniroute/cx-gpt-6.2-sol-medium` and `gpt-6.2-sol-medium` both run as
`--model gpt-6.2-sol -c model_reasoning_effort=medium` (`clients.codex_model`). A caller's `writable_dirs` add `--sandbox workspace-write --add-dir <dir>` to an implement turn.
Native review launches look for the client in `PLANGRAPH_CODEX_BIN`, the user install,
then the PATH prepared by the shared harness resolver, which can include NVM directories.
A missing executable returns exit 10 and a `client_missing` diagnostic before review starts.

If the wait ends while a seat is live, `plangraph review` prints its run ID, expected end, and late review file path. It exits 3 while the review is pending, or exits 1 if a seat has already failed. Late files use `MISSING` for successful runs without a verdict and `ERROR` for failed runs or unusable verdicts. The foreground command issues approvals after all seats pass.
