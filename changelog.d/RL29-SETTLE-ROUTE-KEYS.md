### Fixed

- Reconciliation settles real Python-spawner orphans as RUN_ORPHANED and writes
  their outcome row, deriving leg_key using spawner.leg_probe.key(). The attempt
  writer in SpawnService._spawn (spawner/service.py) writes asdict(chosen), not a
  route identity, so route_key is null when absent; settlement never requires it.
  Such unattributed rows remain in the run record, outside the attributed evidence
  adapter. Explicit keys remain intact; malformed legs/keys remain invalid.
  Missing leg identity records its skip reason through the shared private LiveLog
  with run/attempt context. Shared full-Leg fixtures check Python/Rust key parity
  without requiring Python in cargo tests.
