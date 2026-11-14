- Keeper deploy tool `tools/keeper_deploy.py`: live smoke within 60 s after restart (health, probe claim+release, close_merged route); a failed smoke restores the previous drop-in set, restarts and exits non-zero.
- Smoke accepts the expected router-only refusal after claimant checks, or an explicit authenticated close_merged dry-run result, bounds all calls under one deadline, and restores on unexpected exceptions. CLI --dry-run only prints the drop-in.

- Keeper deployment accepts the expected router-role refusal after claimant smoke, replaces stale managed drop-ins deterministically, and verifies the effective working tree and Python path before smoke; mismatches restore the prior deployment.
