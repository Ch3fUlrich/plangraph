# OmniRoute setup home

This directory houses combos, overrides, custom models, clamp patches, probe
logic and reapply scripts as the base for default setup scripts.
Source: the AutoOS repository, sha `ff2f7f97`,
`configuration/omniroute`, imported without history (D-3203, D-4351).
All dated probe records were omitted: no copied script reads them.
The vendor documentation domain in custom-models.json was scrubbed to a placeholder.
Set `AUTOOS_OMNIROUTE_URL` or `OMNIROUTE_PORT` (loopback URL with port `<port>`).
There is no default gateway port; local startup requires `OMNIROUTE_PORT`.

**Live gateways still apply from AutoOS.** Server drift playbooks,
routing-registry and AutoOS tools are not repointed by this change.

## still read from AutoOS until PG-INSTALL-OMNIROUTE

Set `PLANGRAPH_AUTOOS_ROOT` to the read-only AutoOS checkout when invoking apply
scripts from here; local JSON and sibling scripts resolve from their own directory.
External dependencies retain AutoOS behaviour: `configuration/api-keys.yml`,
`configuration/docker/ai-stack/ai-stack.sh`, `catalog/ai-registry.json`,
`lib/linux/install.sh`, `lib/windows/AutoOS.Install.psm1`,
`tools/combo-contract.py`, `tools/autoos_gateway_key.py`,
`infra/mcp-servers/scripts/_go_gate.py` and
`infra/mcp-servers/scripts/invoke-go-gate.ps1`.
Other reapply documentation refers to AutoOS tools, tests, evidence and Dockerfile
paths; those were not imported. All `AUTOOS_*` overrides retain their meaning,
including routing/worker records, key files, registry, gateway, custom-model file
and drift snapshot overrides. GO checks still cover the AutoOS checkout.

## Refresh the reusable combo set

`combo-set/combos.json` is the allow-listed export of `live/server-combos.json`,
exported 2026-10-17: 34 name-to-ordered-members entries, no source strategy,
so **no strategy keys**; never infer them from the separate `combos.json` setup plan.
Names, model members, order and supplied strategy are public (D-2359).

For an authorized refresh run `python3 deploy/omniroute/export-combos.py
--gateway "$GATEWAY" --key-file "$KEY_FILE"`; add `--check` to compare without
writing (exit 1 for a difference). Only the API is read, never gateway storage.
The key file contains one nonempty token; it is sent only in Authorization.
Plain HTTP is restricted to numeric loopback; redirects are refused, proxies are
disabled and failures emit only a fixed message. Review public fields before
committing. The exporter discards all fields except name, ordered members and an
explicit strategy. Account identifiers and per-site endpoints are not exported.

## Source differences and consumer compatibility

The imported file list is the `COPIED` list in `tests/test_omniroute_home.py`.
Relative to `configuration/omniroute` at `ff2f7f97`, dated probe records
are omitted; `custom-models.json` replaces the vendor documentation domain
before ` model table` with `<vendor-model-documentation>`. The complete
byte-difference inventory follows
(including the PowerShell BOM, preserved unchanged). New README, exporter and
combo-set files are additions, not source replacements.

The consumers named in AUTOOS-INTO-PLANGRAPH continue using their AutoOS paths:
this lane neither moves nor deletes the source tree, repoints live consumers,
nor imports their external dependencies. The explicit root override
prevents the relocated scripts from mistaking PlanGraph for AutoOS.

Byte-difference inventory (all other copied bytes remain unchanged):

| File | Differences from the source |
| --- | --- |
| `apply.sh` | Local documentation paths; explicit read-only root; require gateway URL or port, export CLI URL, use configured launch port and neutral diagnostics; placeholder glob changes from `REPLACE_WITH_*` to `"REPLACE_WITH_"*`; neutral provider-failure comment. |
| `apply-custom-models.sh` | Local documentation paths; explicit read-only root; require gateway URL or port with no default. |
| `apply.ps1` | Local documentation paths; explicit read-only root; require gateway URL or port, export CLI URL, use configured launch port and neutral diagnostics. |
| `apply-capability-overrides.ps1` | Local documentation paths; explicit read-only root; replace Gateway parameter/default with required environment URL or port; preserve BOM. |
| `live/server-combos.json` | Replace source metadata with “read-only export of 2026-10-17”. |
| `vertex-failover-knobs.json` | Replace site restart port with `<port>`. |
| `qwen-clamp-reapply-README.md` | Three invocation paths use local sibling scripts, with usage from this directory; diagnostic origin becomes an untracked diagnostic script; example package directory becomes `<omniroute-package-dir>`. |
| `qwen-clamp-reapply.ps1` | Diagnostic origin becomes an untracked diagnostic script; example package directory becomes `<omniroute-package-dir>`. |
| `reason-fix-README.md` | Two invocation paths use local sibling scripts; repository-root wording changes from “From the AutoOS repository root” to “From this directory” (correcting the intermediate “From the plangraph repository root”); isolated-instance and shared-gateway verification ports both become `<port>`; example package directory becomes `<omniroute-package-dir>`. |
| `reason-fix-reapply.ps1` | Two patch comments refer to the local README; example package directory becomes `<omniroute-package-dir>`. |
| `reasoning-defense.md` | Scope becomes `./`. |
| `verify-clamp.mjs` | Patch documentation path becomes local. |
| `custom-models.json` | Vendor documentation domain becomes `<vendor-model-documentation>`; old wording named a site-specific host; new wording is “the vendor's token-plan host”. |


## Scrub false-positive audit

Each ledger entry matches exactly one path, rule and value hash; repeated
occurrences share that entry. Duplicate and stale entries were removed.

| Path | Rule | Matched text | Why false positive |
| --- | --- | --- | --- |
| `deploy/omniroute/apply.ps1`: `configuration/vertex`; `deploy/omniroute/apply.sh`: `configuration/vertex` | credential-shape | Exact texts paired with their paths in the first column | Repository-relative filename or source branch, not a credential or deployment value |
| `deploy/omniroute/capability-overrides.json`: `scaleway/qwen3-235b-a22b-instruct-2507`; `deploy/omniroute/live/server-combos.json`: `ovh/Qwen3-Coder-30B-A3B-Instruct`; `deploy/omniroute/verify-clamp.mjs`: `scaleway/qwen3-235b-a22b-instruct-2507` | high-entropy | Exact texts paired with their paths in the first column | Public provider/model identifier, not a credential |
| `deploy/omniroute/qwen-clamp-reapply-README.md`: `L1-backlog/ws-fixes-20260930`; `deploy/omniroute/qwen-clamp-reapply-README.md`: `docs/handoff/2026-09-30-lanePatchIntegrity`; `deploy/omniroute/qwen-clamp-reapply-README.md`: `L1-backlog/ws-qwenclamp-20260930`; `deploy/omniroute/qwen-clamp-reapply.ps1`: `docs/handoff/2026-09-30-lanePatchIntegrity`; `deploy/omniroute/qwen-clamp-reapply.ps1`: `L1-backlog/ws-fixes-20260930`; `deploy/omniroute/reason-fix-README.md`: `L1-backlog/ws-fixes-20260930`; `deploy/omniroute/reason-fix-README.md`: `docs/handoff/2026-09-30-lanePatchIntegrity`; `deploy/omniroute/reason-fix-reapply.ps1`: `docs/handoff/2026-09-30-lanePatchFix`; `deploy/omniroute/reason-fix-reapply.ps1`: `L1-backlog/ws-fixes-20260930`; `deploy/omniroute/vertex-trailing-turn-README.md`: `logs/briefs/evidence/vertex-capture-d908` | high-entropy | Exact texts paired with their paths in the first column | Repository-relative filename or source branch, not a credential or deployment value |
| `deploy/omniroute/README.md` | credential-shape | `configuration/vertex` | Public model identifier or repository-relative source path or branch quoted in the audit; not a credential or site value |
| `deploy/omniroute/README.md` | high-entropy | `L1-backlog/ws-fixes-20260930` | Public model identifier or repository-relative source path or branch quoted in the audit; not a credential or site value |
| `deploy/omniroute/README.md` | high-entropy | `L1-backlog/ws-qwenclamp-20260930` | Public model identifier or repository-relative source path or branch quoted in the audit; not a credential or site value |
| `deploy/omniroute/README.md` | high-entropy | `docs/handoff/2026-09-30-lanePatchFix` | Public model identifier or repository-relative source path or branch quoted in the audit; not a credential or site value |
| `deploy/omniroute/README.md` | high-entropy | `docs/handoff/2026-09-30-lanePatchIntegrity` | Public model identifier or repository-relative source path or branch quoted in the audit; not a credential or site value |
| `deploy/omniroute/README.md` | high-entropy | `logs/briefs/evidence/vertex-capture-d908` | Public model identifier or repository-relative source path or branch quoted in the audit; not a credential or site value |
| `deploy/omniroute/README.md` | high-entropy | `ovh/Qwen3-Coder-30B-A3B-Instruct` | Public model identifier or repository-relative source path or branch quoted in the audit; not a credential or site value |
| `deploy/omniroute/README.md` | high-entropy | `scaleway/qwen3-235b-a22b-instruct-2507` | Public model identifier or repository-relative source path or branch quoted in the audit; not a credential or site value |
| `deploy/omniroute/combo-set/combos.json` | high-entropy | `ovh/Qwen3-Coder-30B-A3B-Instruct` | Public model identifier or repository-relative source path or branch quoted in the audit; not a credential or site value |
| `deploy/omniroute/README.md` | credential-shape | `evidence/vertex` | Relative evidence directory fragment, not a credential |
