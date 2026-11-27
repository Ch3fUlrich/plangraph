# Writer feeder

`plangraph feed --once` fills available native Codex writer slots on the current
host. Run the supplied `deploy/systemd/pg-feeder.service` and `.timer` every two
minutes after configuring the host; these units are examples and are not installed
by the CLI. `KillMode=process` lets detached supervisors and writers outlive the
oneshot tick. Launches disable cross-host overflow so admission and writer caps
apply to the host that will execute them. The spawner can still queue/refuse if its
local checks are stricter than the feeder policy. The service runs from the main
repository checkout. It needs the
built Rust `plangraph` binary at `~/.local/bin/plangraph`, Python's existing
`plangraph.spawner.cli` and quota reader, and `curl` on PATH. When the selected
checkout contains `src/plangraph`, that source is prepended to the child module
path so an older editable installation cannot silently change adapter contracts;
additional caller module paths are preserved.

`--dry-run` prints one JSON decision and never claims, spawns, writes a prompt,
creates feeder state, or appends an event. Normal ticks append one JSON line to
`~/.local/state/plangraph/feeder/events.jsonl`, including refusals and adapter
errors. Decisions are `idle`, `refused`, `would_spawn`, `pending` or `spawned`.
Routine admission refusals exit 0 so the timer stays healthy; adapter/envelope/
start errors before the first launch carry `error: true` and exit 2.
Each batch consumes one slot and one run's RAM. The next batch avoids all files
held by earlier batches in the tick. A dry run lists each batch, card and brief
under `would_start`. A live tick lists accepted launches under `started`.
Both lists report `slots` and the `stopped` reason. A later refusal preserves the
successful decision and its earlier launches.
There is no LLM in scheduling. Claims never update priority.

## Activity boundaries

The CLI crate exports `feeder::pick`, `admit`, `brief`, and `spawn`. The first
three functions are pure. `spawn` necessarily performs effects through the
`Launch` trait: claim, start, and on failure release using the returned claim
epoch. These boundaries can become Temporal activities without changing the
selection algorithm. Rust fixture tests exercise the trait without network or
external processes. `dispatch` makes dry-run stop before the first trait call.

Selection consumes keeper `/v1/ready?fmt=json&all=1&agent=1&project=plangraph`
and current spawner `ps` JSON. It never uses a cached keeper snapshot. The current
slice targets the plangraph repository; `--repo` requires an absolute checkout path and changes that path,
not the keeper project. The ready response's optional `needs_reconcile` and
`in_progress` arrays may contain item objects or key strings; these keys are
excluded even if duplicated in `items`. The current keeper does not yet emit those
groups: legacy `context.newer_decisions`, and merged `context.work_exists.commits`
without open lanes, are excluded too. An old "untouched" timestamp alone does not
exclude work. Explicit `in_merge_batch`/`merge_batch` metadata also excludes it.
Non-open item states are excluded.
Any nonterminal spawner run excludes its identical `item_key`, regardless of role.
Known terminal states include `stalled`, `terminated_after_answer`, `timeout` and review `timed_out`; queued, unknown, and
waiting-for-merge-batch statuses remain active. Review/research runs do not block
other items by overlap or consume writer capacity.

Order is: explicit PR metadata (`pr`, `pr_url`, `pr_number`, `pull_request` or
`open_pr`), then `ci_fix`, kind `ci-fix`/`ci_fix`, or a key containing `CI-FIX`,
then descending numeric priority, then oldest RFC3339 `created_at`, then key.
Missing/invalid ages sort last. A PR waiting in a merge batch belongs in
`in_progress` or an active run; the feeder leaves it alone and considers other
work. Keeper reconciliation groups are authoritative; the feeder never edits
state or derives completion from a claim, an idle process, or a PR mention in prose.

Current keeper metadata uses its existing unknown-field fold. Creating an item
with `files`, `pr_url` or `ci_fix` preserves those fields as labeled `note` lines:

```json
{"note": "ci_fix: true\nfiles: [\"src/example.rs\"]\npr_url: https://example.invalid/pulls/123"}
```

The feeder decodes only these explicit labels (JSON for lists/booleans/numbers,
plain values for strings; `files: src/a.rs` is also a single named path).
No PR or file inference is made from prose. Later
note labels win; future top-level fields take precedence. Zero/empty PR values
and explicitly closed/merged PRs are not promoted; negative words such as `no`
and `none` do not promote, and `CI-FIX` must be separate hyphen-delimited key
components. Malformed candidate items are isolated in `skipped` with their key
and diagnostic so others can start. Malformed running-writer scope still refuses
the tick. The fixtures use this actual keeper wire shape.

Overlap is deliberately conservative: project plus the first hyphen-delimited
key component is a subsystem family (`plangraph:KEEPER-ONE` and
`plangraph:KEEPER-TWO` conflict). Families always conflict, even when files are
specified. Additionally, identical named repo-relative files or directory/child
paths conflict across families. Use canonical repo-relative paths with `/`
separators. Projects with hyphens retain their complete project prefix, and
repo-relative files in different projects do not conflict. The adapter obtains
running-writer scope through the live keeper `snapshot` API (which includes full
notes for claimed items); `show` omits notes. It does not use an on-disk snapshot.
Errors refuse the tick. When a run lacks `item_key`, the adapter uses its card
`item_key`, then its feeder receipt if available. A writer still lacking identity
consumes one slot on its own host and has no inferred item/file overlap; it does
not refuse the whole tick or block selection on other hosts.
A host-local advisory lock covers live reads through spawn; concurrent non-dry
invocations refuse. Keeper claim conflict prevents two hosts launching one item.
The updated remote PS endpoint and privacy projection retain `item_key`, and
admission queue records carry their card identity, host, role and start time.
Deploy this compatibility fix on every queried host. Host-health diagnostic rows
without a run ID are not running writers and do not block selection; actual
anonymous writers consume their host slot. Remote scope uses observed run records;
unreachable-host diagnostics do not prove a writer exists.
Fleet-launch writers also consume a slot on their host and hold every plangraph
item key on their card. The feeder matches launcher records to spawner rows by
run ID and counts each run once. A recent working job record, an active Claude
roster entry or a live PID establishes liveness for sessions absent from ps.
Whole item-key tokens in fleet-session rows carry the same scope before the
launcher's session record appears.
An item-level claim refusal records its key/reason in `skipped` and tries the next
eligible item in the same tick, at most once per item. Authentication, unavailable
keeper/decisions, and start/compensation errors stop the tick. A lost claim response
stops immediately and is audited with its item key: without an acknowledged
owned epoch the feeder cannot safely issue a release. The claim lease bounds
that uncertainty; no further items are claimed in that tick. The item-refusal
loop is bounded to one attempt per ready key. This avoids a stale
head item monopolizing every timer tick without overriding the keeper guard.

## Configuration and facts

The CLI reads the same `PG_URL` and `PG_TOKEN_FILE` as the keeper CLI, from the
process or `~/.config/plangraph/plangraph.env` (`PLANGRAPH_ENV_FILE` selects another
file). Use a dedicated feeder L1/worker principal token with access to plangraph;
the HTTP API authenticates the principal from that token. The feeder never mints
tokens and never prints their contents. HTTP headers travel to curl on stdin.

`--policy PATH` defaults to `deploy/feeder/policy.toml`. Defaults are 3 writers on
the `coding` host alias, 5 on `dell`, 8 on `workstation`, 4 GB available RAM per additional run, a 10 GB disk floor,
and provider quota strictly above 10%. Byte units are decimal. Existing writers
started before the sample are already reflected in free RAM. Reserve one run's
estimate for the next launch, plus a full estimate for each local writer whose
start is at/after the sample (or unknown). This prevents repeated two-minute ticks
from reusing a pre-launch measurement to over-admit.
Unknown hosts refuse. The facts host must be the spawner's configured host name.
Policy lookup tries that full name, then its first DNS label, so fully qualified
coding host names use the public `coding` alias. Keep private full-name overrides
in a local policy file. Counting uses the same case-insensitive DNS/alias identity
as admission. Registry IDs/aliases from `~/.config/plangraph/hosts.json` and optional
policy `[host_aliases]` mappings resolve host names; explicit policy aliases win.
A facts host inconsistent with registry `self` or `PLANGRAPH_HOST_NAME` refuses;
active writers with no host refuse instead of silently counting zero.

`--live-facts` is the feeder service's Linux and WSL mode. It samples available
memory, one-minute load, effective CPU count, and disk space on the run-state
and existing Cargo target filesystems. It uses the spawner host identity from
`PLANGRAPH_HOST_NAME` or the host registry.

On WSL, the available RAM sample is the lower of WSL's MemAvailable and Windows'
free physical memory after the Windows floor (6 GB by default). The PowerShell
read has a 15-second limit. After a failed read, the feeder uses a cached value
for at most one facts window (300 seconds by default). Without a fresh value it
refuses as `windows_ram_unreadable`. Dry runs can read the cache.

Live hosts admit by free RAM (D-5122). Free RAM above `ram_floor_bytes`, divided by
`ram_per_run_bytes`, is `slots_by_ram`; the host's writer limit is its running writers
plus `new_writers_allowed`. CPU pressure above `cpu_pressure_max_percent` (50% by
default) refuses new writers and reports the measured `some avg300` and the threshold.
Facts files can supply `cpu_pressure_avg300`. Where the kernel has no
`/proc/pressure/cpu` file (only a missing file counts; an unreadable or malformed one
still belongs to a PSI kernel) and load1 is at or above the cpu count, the host gets
one new writer per tick (`load_paced`). If the load average is unreadable, the
RAM budget sets the slots. The decision records `psi`, `psi_unreadable` or
`load_fallback` as its CPU pressure source.
Every decision with an available sample, including an idle tick, records its capacity
line: host, cpus, `load1` (null when unavailable), running writers, free RAM, floor,
RAM per writer, `slots_by_ram`, `new_writers_allowed`, `load_paced`, `writer_limit`
(null for a host the policy does not list), disk and quota. It also records both
RAM views and the Windows reading's source on WSL.

The policy lists the hosts that run writers under `[max_writers]` (`coding`, `dell`,
`workstation`). A host it does not list refuses as `unknown_host:<name>` under both
`--live-facts` and `--facts`; the live budget never writes a limit for it. Under
`--facts` the configured cap is the limit.

`--facts PATH` defaults to `~/.config/plangraph/feeder-facts.json` when live facts
are disabled. Supply current
host measurements through your host facts collector at least every two minutes
(write by atomic replacement):

```json
{
  "host": "dell",
  "free_ram_bytes": 12000000000,
  "free_disk_bytes": 30000000000,
  "quota_remaining_percent": 75.0,
  "observed_at": 1794700000
}
```

Replace `observed_at` with the current Unix seconds, not the example timestamp.
Facts older than 300 seconds, future timestamps, missing measurements, or malformed
JSON refuse. Runtime overwrites `now`, `running_writers` and `kill_switch` from
the actual clock, live runs and `~/.config/plangraph/feeder.off` existence. Touching
that file stops admission; removing it permits subsequent ticks. Capacity uses
active writers on the facts host; overlap and duplicate detection use all runs.

Quota prefers the existing read-only
`plangraph.routing_support.quota_snapshots.fetch_snapshot_rows` reader, including
`QUOTA_SNAPSHOT_DB` and its gateway configuration discovery. Latest codex
account/window observations are combined by the lowest remaining window per
account, then the best account. Numeric/ISO timestamps, `remainingPct`,
`provider:connection` identities and bool/numeric/string exhaustion flags match the
existing reader's formats. Reader observations older than `quota_max_age_seconds`
(default 10800 seconds, matching the store's hourly cadence) or from the future
make that account unavailable. A malformed window disables its account, while
independent healthy accounts remain usable. Exhausted accounts contribute zero.
Hosts with their own Codex observations keep the local reader. A live host without
those observations reads the center's `quota-facts.json` in its plangraph config
directory. Set `PLANGRAPH_QUOTA_FACTS` to use another path. The file holds an
`observed_at` Unix timestamp in seconds and a `rows` array in the quota reader's
format. The worker applies its own `quota_facts_max_age_seconds` (default 900) to
the envelope and `quota_max_age_seconds` to the observations. A missing, unreadable,
expired or unusable snapshot refuses as `quota_stale` with both ages when available
and retains the live capacity line. Explicit facts keep their supplied quota
fallback when the local reader is unavailable. Invalid or exhausted quota refuses.

Only items with a spec reach selection: the feeder reads `docs/specs/<ID>.card.yaml` on its
checkout and gives the writer that card's `write_set` as the brief's files (keeper `files` are
not used). An item without a card, or with an empty write set, is left out and counted in the
decision line as `needs_spec` (`needs_spec_first` lists ten keys); the planning session writes
its spec from the plan the item belongs to before the feeder takes it. Each host checks its own
feeder runs (cards pushing to `lane/feeder-`) before a launch: while `no_commit_stop` (3) of its
last `no_commit_window` (5) ended runs added no commit it refuses as `no_commit_stop`, and with
`pace_by_commit` it starts its next writer only when every running feeder writer of the host has
made a commit (`warming: <run> has no commit yet`), so a fault shows on one run, not on many.
Rollout of a feeder or policy change follows rule 3.112: one host first until one live run there
commits. RAM, CPU pressure, disk and quota admission stay as above.

Policy `batch_max_items` defaults to 6 in both the shipped policy loaded by the feeder service
and the built-in policy. The range is 1..=50. Set it to 1 for single-item runs. A value outside
the range refuses the tick as `invalid_policy`. A batch holds the
first eligible item in launch order plus later eligible items of its family or sharing its
files, in launch order. Every item is claimed on its own, then checked against the claimed
items of every host (the keeper snapshot, queued runs included): a claim whose files overlap
another live claim outside the batch is released by its own epoch and skipped as
`files_reserved_by:<key>`, so two hosts that claim at once never both keep overlapping files
(both may yield; the next tick retries). A failed launch releases only its own batch's claims.
The card of a batch lists every key as `item_keys` and the union of the files. Its
`PLANGRAPH-BATCH-INPUT-V1` brief gives the batch-writer one `Plan-Item` trailer per key.
Each live launch reserves its own status file in the review directory. The batch brief's
`STATUS_FILE` and the recorded `writer_status_path` name that same file, so review briefs read
only the DONE lines of their own run.

`--house-rules PATH` defaults to `deploy/feeder/house-rules.md`. Brief generation
preserves the keeper title, description (including Why/How), done criteria and
files and notes/PR/CI metadata, rendering string done criteria as plain text and appending that template with `{{item_key}}` substituted. The card fixes
`run_kind` to `native-codex` and `base_ref` to `refs/heads/main`; it also includes
repo, item key, review directory, files and the policy token estimate. Successful prompts
and cards remain in private `launch-*` directories beside the event log for the
asynchronous spawner. Definitively failed launches remove their artifacts and
release the claim; failed compensation is reported explicitly for operator repair.
Receipts are parsed before exit codes: valid queued (exit 5) and unknown (exit 1)
receipts with a run ID retain the claim and produce `pending`. Empty, non-JSON,
unadmitted or definitively failed outputs release the claim. Accepted receipts
are atomically saved beside the card so
anonymous queued PS records can recover their item/files/host after a restart.
Unknown/queued `run.status` in a handoff event requires reconciliation; it does not
mean execution was witnessed. Unreadable receipt files are ignored rather than
halting the host; known terminal receipts are pruned only during live ticks.
Dry-run never prunes or writes receipts. Retain launch directories while their
runs are active.

Dry-run against the live keeper is read-only. After the scoped rollout GO,
deploy the CLI, policy, and feeder credentials to each approved Linux or WSL
checkout. In that checkout, install the user units with
`install -Dm644 deploy/systemd/pg-feeder.service ~/.config/systemd/user/pg-feeder.service`
and
`install -Dm644 deploy/systemd/pg-feeder.timer ~/.config/systemd/user/pg-feeder.timer`,
then run `systemctl --user daemon-reload` and
`systemctl --user enable --now pg-feeder.timer`.
Before the WSL workstation's feeder rollout is approved, keep the timer
disabled or set `feeder.off`. The host must show positive evidence of one of two states:
`systemctl --user is-enabled pg-feeder.timer` prints exactly `disabled` and
`systemctl --user is-active pg-feeder.timer` prints exactly `inactive` (any other
output, such as `enabled`, `enabled-runtime`, `active` or `activating`, does not count;
`systemctl --user disable --now pg-feeder.timer` reaches the state), or
`~/.config/plangraph/feeder.off` exists. Before enabling the timer, verify that
the deployed binary reports the Windows RAM sample in a dry run.
