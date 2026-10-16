# Lane I: Docker setup (plangraph-L2-su-docker), the LAST item

STANDING RULES: `docs/plans/orch-setup/briefs/COMMON.md` (binding, read first). Design section 5 (same env file for Docker).
Item: `plangraph:SU-DOCKER`. It starts only after `routing:PH-RETHINK`, `plangraph:PH-ORCH-SETUP` and
`plangraph:PH-ORCH-GUARDS` are done (operator S13: after the whole rethink). workst-L1-autoos refreshes this brief at launch:
the Rust keeper, the SvelteKit UI and the control-plane layout will have changed by then.

## Scope (to confirm at launch)
- A compose file that runs the plangraph services of a local all-in-one setup (keeper, UI, and anything the end-state design
  adds), reading the ONE env file via `env_file:`.
- Default host networking so the localhost URLs work unchanged.
- `plangraph setup check --docker` reports drift between the env file and the running services.
- Fresh-setup CI runs once inside Docker.
- Production server deployments stay in the Server repo; this item is the local/dev-machine Docker path only.
