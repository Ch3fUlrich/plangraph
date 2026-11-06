# OmniRoute setup home

This directory houses combos, overrides, custom models, clamp patches, probe
logic and reapply scripts as the base for default setup scripts.
Source: public Ch3fUlrich/AutoOS repository, sha `ff2f7f97`,
`configuration/omniroute`, imported without history (D-3203, D-4351).
All dated probe records were omitted: no copied script reads them.
The vendor documentation domain in custom-models.json was scrubbed to a placeholder.
Generic loopback defaults are not deployment addresses.

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
before ` model table` with `<vendor-model-documentation>`. All other imported
bytes initially matched the source. The complete subsequent byte diff follows
(including the PowerShell BOM, preserved unchanged). New README, exporter and
combo-set files are additions, not source replacements.

The consumers named in AUTOOS-INTO-PLANGRAPH continue using their AutoOS paths:
this lane neither moves nor deletes the source tree, repoints live consumers,
nor imports their external dependencies. The explicit root override below
prevents the relocated scripts from mistaking PlanGraph for AutoOS.

```diff
diff --git a/deploy/omniroute/apply-capability-overrides.ps1 b/deploy/omniroute/apply-capability-overrides.ps1
index 2b49a784..1b375bb8 100644
--- a/deploy/omniroute/apply-capability-overrides.ps1
+++ b/deploy/omniroute/apply-capability-overrides.ps1
@@ -1,7 +1,7 @@
 ﻿#Requires -Version 5.1
 <#
 .SYNOPSIS
-  Apply configuration/omniroute/capability-overrides.json to a running local OmniRoute gateway.
+  Apply ./capability-overrides.json to a running local OmniRoute gateway.
 
 .DESCRIPTION
   Companion to context-overrides.json / apply.ps1. Both write the same management route
@@ -46,7 +46,8 @@ $ErrorActionPreference = 'Stop'
 Set-StrictMode -Version Latest
 
 $Here = Split-Path -Parent $MyInvocation.MyCommand.Path
-$Root = Split-Path -Parent (Split-Path -Parent $Here)
+if (-not $env:PLANGRAPH_AUTOOS_ROOT) { throw 'Set PLANGRAPH_AUTOOS_ROOT to the read-only AutoOS checkout' }
+$Root = $env:PLANGRAPH_AUTOOS_ROOT
 $OverridesFile = Join-Path $Here 'capability-overrides.json'
 
 # --- Fleet rule D-825: a live gateway change runs only on an explicit GO ---
@@ -97,7 +98,7 @@ if (-not $DryRun) {
 # The management API accepts the machine loopback token the local CLI sends
 # (omniroute docs/security/CLI_TOKEN.md): HMAC-SHA256 of the machine id, keyed by that
 # id, over the salt "omniroute-cli-auth-v1", sent as the x-omniroute-cli-token header.
-# Same derivation as configuration/omniroute/apply.ps1 Get-AutoOSManageToken.
+# Same derivation as ./apply.ps1 Get-AutoOSManageToken.
 function Get-AutoOSManageToken {
     try {
         $guid = (Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Cryptography' -ErrorAction Stop).MachineGuid
diff --git a/deploy/omniroute/apply-custom-models.sh b/deploy/omniroute/apply-custom-models.sh
index 52567e4b..12bd01f5 100755
--- a/deploy/omniroute/apply-custom-models.sh
+++ b/deploy/omniroute/apply-custom-models.sh
@@ -1,7 +1,7 @@
 #!/usr/bin/env bash
-# Declare custom (manual) gateway models from configuration/omniroute/custom-models.json.
+# Declare custom (manual) gateway models from ./custom-models.json.
 #
-#   ./configuration/omniroute/apply-custom-models.sh [--dry-run] \
+#   ./apply-custom-models.sh [--dry-run] \
 #                                      [--go <ref> --go-sha <sha>] [--go-offline]
 #
 # For each row it reads GET /api/provider-models?provider=<provider> and POSTs
@@ -19,7 +19,7 @@
 set -euo pipefail
 
 HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
-ROOT="$(cd "$HERE/../.." && pwd)"
+ROOT="$(cd "${PLANGRAPH_AUTOOS_ROOT:?set PLANGRAPH_AUTOOS_ROOT to the read-only AutoOS checkout}" && pwd)"
 GATEWAY="${AUTOOS_OMNIROUTE_URL:-http://127.0.0.1:20128}"
 MODELS_FILE="${AUTOOS_CUSTOM_MODELS_FILE:-$HERE/custom-models.json}"
 DRY=0
diff --git a/deploy/omniroute/apply.ps1 b/deploy/omniroute/apply.ps1
index e3b0a860..2cbc3d2f 100644
--- a/deploy/omniroute/apply.ps1
+++ b/deploy/omniroute/apply.ps1
@@ -6,11 +6,11 @@
   1. Registers every provider key found in configuration/api-keys.yml.
   2. Refreshes the gateway's model catalog for what it just registered
      (omniroute models <provider>), so one run is enough for a fresh machine.
-  3. Declares the custom (manual) models of configuration/omniroute/
+  3. Declares the custom (manual) models of ./
      custom-models.json through POST /api/provider-models, before the catalog is
      read for combo validation — a provider whose model list the gateway image
      bakes has no live discovery, so its new ids exist only once declared.
-  4. (Re)creates the tier combos from configuration/omniroute/combos.json.
+  4. (Re)creates the tier combos from ./combos.json.
   5. Prunes the combos listed there as "retired" or "omitted" from the store
      (only those).
 
@@ -28,16 +28,16 @@
   renamed upstream model degrades one tier leg instead of breaking the run.
 
 .EXAMPLE
-  .\configuration\omniroute\apply.ps1
+  .\apply.ps1
 .EXAMPLE
-  .\configuration\omniroute\apply.ps1 -DryRun
+  .\apply.ps1 -DryRun
 .EXAMPLE
-  .\configuration\omniroute\apply.ps1 -Probe   # one tiny request per combo
+  .\apply.ps1 -Probe   # one tiny request per combo
 .EXAMPLE
   # A live (non -DryRun) run needs a judge GO naming this checkout's HEAD (fleet rule D-825):
-  .\configuration\omniroute\apply.ps1 -Go D-825 -GoSha (git rev-parse HEAD)
+  .\apply.ps1 -Go D-825 -GoSha (git rev-parse HEAD)
   # The GO must name an artefact that exists; from a machine that cannot read it:
-  .\configuration\omniroute\apply.ps1 -Go D-825 -GoSha (git rev-parse HEAD) -GoOffline
+  .\apply.ps1 -Go D-825 -GoSha (git rev-parse HEAD) -GoOffline
 #>
 [CmdletBinding()]
 param(
@@ -56,7 +56,8 @@ Set-StrictMode -Version Latest
 $ErrorActionPreference = 'Continue'
 
 $Here      = Split-Path -Parent $MyInvocation.MyCommand.Path
-$Root      = Split-Path -Parent (Split-Path -Parent $Here)
+if (-not $env:PLANGRAPH_AUTOOS_ROOT) { throw 'Set PLANGRAPH_AUTOOS_ROOT to the read-only AutoOS checkout' }
+$Root = $env:PLANGRAPH_AUTOOS_ROOT
 
 # --- Fleet rule D-825: a live gateway change runs only on an explicit GO ---
 # 2026-10-09 06:27Z an apply real run began before any judge GO existed and
@@ -610,7 +611,7 @@ foreach ($keyName in $ProviderMap.Keys) {
 # to validate against, so its leg was dropped as "catalog does not know" and
 # only appeared on the SECOND apply run (L0 2026-09-27T19:07:39Z, free-ai/qwen7b
 # on a fresh machine). Order is therefore register -> refresh -> read -> combos,
-# the same order configuration/omniroute/apply.sh uses.
+# the same order ./apply.sh uses.
 Write-Host 'Catalog:'
 if ($DryRun) {
     foreach ($providerId in $RegisteredNow) {
@@ -634,7 +635,7 @@ if ($DryRun) {
 # the operator's declared override, registered through
 # POST /api/provider-models {provider, modelId, modelName, source: "manual",
 # max_input_tokens, max_output_tokens, supportsVision} - the one body shape
-# configuration/omniroute/apply-custom-models.sh (Linux) sends. Idempotent: an
+# ./apply-custom-models.sh (Linux) sends. Idempotent: an
 # id the gateway already holds is reported, not rewritten. Runs BEFORE the live
 # catalog read below, or a combo leg naming a custom id the store does not hold
 # yet is dropped as "catalog does not know" until the second run.
diff --git a/deploy/omniroute/apply.sh b/deploy/omniroute/apply.sh
index b6de6f26..0ccfa50e 100755
--- a/deploy/omniroute/apply.sh
+++ b/deploy/omniroute/apply.sh
@@ -7,11 +7,11 @@
 #      alone has nothing to attach to)
 #   2. refreshes the gateway's model catalog for what it just registered
 #      (omniroute models <provider>), so one run is enough on a fresh machine
-#   3. declares the custom (manual) models of configuration/omniroute/
+#   3. declares the custom (manual) models of ./
 #      custom-models.json through apply-custom-models.sh, before the catalog is
 #      read for combo validation — a provider whose model list the gateway image
 #      bakes has no live discovery, so its new ids exist only once declared
-#   4. (re)creates the tier combos from configuration/omniroute/combos.json
+#   4. (re)creates the tier combos from ./combos.json
 #   5. prunes the combos listed there as "retired" from the store (only those)
 #
 # B2-VERTEX 2026-10-01: vertex authenticates from the GCP service-account JSON
@@ -35,7 +35,7 @@
 # whole, reported lost rather than restored short — never reported as untouched
 # (see the Combos loop).
 #
-#   ./configuration/omniroute/apply.sh [--dry-run] [--probe] [--drift [--drift-against registry]] \
+#   ./apply.sh [--dry-run] [--probe] [--drift [--drift-against registry]] \
 #                                      [--allow-drift <name>] [--go <ref> --go-sha <sha>] [--go-offline]
 #
 # --probe sends one tiny request per combo and reports what answered
@@ -67,7 +67,7 @@
 set -euo pipefail
 
 HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
-ROOT="$(cd "$HERE/../.." && pwd)"
+ROOT="$(cd "${PLANGRAPH_AUTOOS_ROOT:?set PLANGRAPH_AUTOOS_ROOT to the read-only AutoOS checkout}" && pwd)"
 # AUTOOS_OMNIROUTE_URL points the run (and the CLI) at another gateway; the
 # tests aim it at a closed port so a dry run never reads the live one.
 GATEWAY="${AUTOOS_OMNIROUTE_URL:-http://127.0.0.1:20128}"
diff --git a/deploy/omniroute/qwen-clamp-reapply-README.md b/deploy/omniroute/qwen-clamp-reapply-README.md
index 77ad51d1..d9a61e30 100644
--- a/deploy/omniroute/qwen-clamp-reapply-README.md
+++ b/deploy/omniroute/qwen-clamp-reapply-README.md
@@ -93,13 +93,13 @@ reproduced exactly.
 
 ```powershell
 # From the AutoOS repository root:
-pwsh -File configuration/omniroute/qwen-clamp-reapply.ps1
+pwsh -File ./qwen-clamp-reapply.ps1
 
 # Preview without writing:
-pwsh -File configuration/omniroute/qwen-clamp-reapply.ps1 -DryRun
+pwsh -File ./qwen-clamp-reapply.ps1 -DryRun
 
 # Override the package directory:
-pwsh -File configuration/omniroute/qwen-clamp-reapply.ps1 -PackageDir C:\dev\omniroute
+pwsh -File ./qwen-clamp-reapply.ps1 -PackageDir C:\dev\omniroute
 ```
 
 The script backs each changed file up to `<file>.autoos-backup-<timestamp>`, is
diff --git a/deploy/omniroute/reason-fix-README.md b/deploy/omniroute/reason-fix-README.md
index e8cbc3f9..45843575 100644
--- a/deploy/omniroute/reason-fix-README.md
+++ b/deploy/omniroute/reason-fix-README.md
@@ -103,7 +103,7 @@ loaded at runtime:
 
 ```powershell
 # From the AutoOS repository root:
-pwsh -File configuration/omniroute/reason-fix-reapply.ps1
+pwsh -File ./reason-fix-reapply.ps1
 ```
 
 The script:
@@ -115,7 +115,7 @@ The script:
 Override the package directory for a local/dev install:
 
 ```powershell
-pwsh -File configuration/omniroute/reason-fix-reapply.ps1 -PackageDir C:\dev\omniroute
+pwsh -File ./reason-fix-reapply.ps1 -PackageDir C:\dev\omniroute
 ```
 
 ## After `npm update omniroute`
diff --git a/deploy/omniroute/reason-fix-reapply.ps1 b/deploy/omniroute/reason-fix-reapply.ps1
index d94fe151..f2e1b604 100644
--- a/deploy/omniroute/reason-fix-reapply.ps1
+++ b/deploy/omniroute/reason-fix-reapply.ps1
@@ -171,7 +171,7 @@ return (
   $findComment = ' * isInternalReasoningPlaceholder(), so it never re-poisons cache or history.'
   $replaceComment = @'
  * isInternalReasoningPlaceholder(), so it never re-poisons cache or history.
- * AutoOS patch 2026-09-30: added deepseek (see configuration/omniroute/reason-fix-README.md).
+ * AutoOS patch 2026-09-30: added deepseek (see ./reason-fix-README.md).
 '@
   if ($c.IndexOf($replaceComment) -ge 0) {
     $results.Add("SKIP  index.ts patch marker (already present)")
@@ -243,7 +243,7 @@ const reasoning = getReadableReasoningValue(msg).trim();
         // miss the replay layer injects a placeholder into reasoning_content,
         // but the check above skips it as internal. Re-inject it here so the
         // Responses API input carries a reasoning item before function_call.
-        // (AutoOS patch 2026-09-30: see configuration/omniroute/reason-fix-README.md)
+        // (AutoOS patch 2026-09-30: see ./reason-fix-README.md)
         (reasoningIsPlaceholder || !reasoning) &&
         responsesProviderRequiresReasoningPresence(credentialRecord._provider, model) &&
         Array.isArray(msg.tool_calls) &&
diff --git a/deploy/omniroute/reasoning-defense.md b/deploy/omniroute/reasoning-defense.md
index c83a997c..a13d25ec 100644
--- a/deploy/omniroute/reasoning-defense.md
+++ b/deploy/omniroute/reasoning-defense.md
@@ -1,7 +1,7 @@
 # DeepSeek Reasoning Defense — Configuration Note
 
 **Date:** 2026-09-30  
-**Scope:** `configuration/omniroute/` — documents the gateway's reasoning replay
+**Scope:** `./` — documents the gateway's reasoning replay
 defense for DeepSeek thinking-mode models, the root cause of the 400 error, and
 the config-level mitigations available to operators.
 
diff --git a/deploy/omniroute/verify-clamp.mjs b/deploy/omniroute/verify-clamp.mjs
index 8f337ba1..7c7c6a48 100644
--- a/deploy/omniroute/verify-clamp.mjs
+++ b/deploy/omniroute/verify-clamp.mjs
@@ -1,4 +1,4 @@
-// Unit proof for configuration/omniroute/patches/0001-clamp-max-tokens.patch.
+// Unit proof for ./patches/0001-clamp-max-tokens.patch.
 //
 //   node verify-clamp.mjs [path-to-server.js]
 //
```

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
| `deploy/omniroute/README.md` | high-entropy | `configuration/omniroute/patches/0001-clamp-max-tokens` | Public model identifier or repository-relative source path or branch quoted in the audit; not a credential or site value |
| `deploy/omniroute/README.md` | high-entropy | `docs/handoff/2026-09-30-lanePatchFix` | Public model identifier or repository-relative source path or branch quoted in the audit; not a credential or site value |
| `deploy/omniroute/README.md` | high-entropy | `docs/handoff/2026-09-30-lanePatchIntegrity` | Public model identifier or repository-relative source path or branch quoted in the audit; not a credential or site value |
| `deploy/omniroute/README.md` | high-entropy | `logs/briefs/evidence/vertex-capture-d908` | Public model identifier or repository-relative source path or branch quoted in the audit; not a credential or site value |
| `deploy/omniroute/README.md` | high-entropy | `ovh/Qwen3-Coder-30B-A3B-Instruct` | Public model identifier or repository-relative source path or branch quoted in the audit; not a credential or site value |
| `deploy/omniroute/README.md` | high-entropy | `scaleway/qwen3-235b-a22b-instruct-2507` | Public model identifier or repository-relative source path or branch quoted in the audit; not a credential or site value |
| `deploy/omniroute/combo-set/combos.json` | high-entropy | `ovh/Qwen3-Coder-30B-A3B-Instruct` | Public model identifier or repository-relative source path or branch quoted in the audit; not a credential or site value |
| `deploy/omniroute/README.md` | credential-shape | `evidence/vertex` | Relative evidence directory fragment, not a credential |
