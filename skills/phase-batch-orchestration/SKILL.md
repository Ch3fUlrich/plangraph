---
name: phase-batch-orchestration
description: Place and start authorized parallel work packages after deterministic host readiness and resource checks.
---

Use the existing host registry and the package's host-specific checkout paths.
Before every package start:

1. Run `pg-host-readiness --project PATH --scratch-root DIR --sandbox MODE`
   on each candidate worker host, locally or over its configured SSH route.
   Use the actual package checkout, filesystem and chosen sandbox mode.
   Retain each result; only `READY` is eligible. Missing hook trust is a failed
   check, not permission to disable hooks or change trust settings.
2. Call `pg-package-host` with `--exclude-host ID` for every failed preflight
   (repeatable), plus `--allow-workstation` only when workstation use is allowed.
   The command supplies host and reason from fleet samples, with SSH refresh
   after 120 seconds. It honors host RAM floors and role reservations.
   Do not ask a model to choose a host or substitute a manual placement.
3. Start the authorized package with `pg-package-host --spawn REQUEST.json`,
   retaining the same exclusions and permission flag. Set `--order ID1,ID2,ID3`
   from the established placement order, or use registry order. The request is
   the existing spawn tool's arguments; checkout paths must match the target.
   The command calls existing spawner shims, tries the next admitting host on
   explicit refusal, and privately records the receipt and all refusals.
4. Exit 5 means every host refused: leave the package for the orchestrator's
   queue. Exit 1 means uncertain launch state: inspect the recorded run before
   retrying. An acknowledged run must not be duplicated after a connection loss.

The probes and placement use no model calls. Deployment, merge and review
authority continue to follow the package brief and repository role skill.
Command details and timer setup: [Rust host tools](../../docs/host-tools.md).
