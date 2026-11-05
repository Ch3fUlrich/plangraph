# Placement rule loader (R8B A)

`setup/placement-rules.schema.toml` is the sole metadata authority, embedded at
compile time. `schema()` fails closed with `rules_schema`; it never substitutes
defaults after a parse failure. The parser accepts only version 1, rule records,
single-line quoted strings (no escapes), unsigned decimal integers, string
arrays, and inline string-to-integer tables. Blank and full-line comment lines
are accepted; other TOML features are unsupported.

UI consumers serialize `RuleSpec` and `RuleReport`, not a second schema.
All override maps use derived `PG_RULE_<UPPER_SNAKE_NAME>_<UNIT>` keys.
Resolution tries process, env_file, keeper_host, keeper_fleet, then default.
Disallowed sources and invalid values are reported without supplied values,
then fall through. Unknown PG_RULE keys are refused and reported with a redacted
key marker; unrelated environment keys are ignored.

`parse_env_file` accepts trimmed KEY=VALUE and blank/comment lines, never shell
execution, export, expansion or interpolation. Duplicate keys fail the whole
file: callers report `env_file_invalid` and use an empty env-file layer.
Error Display codes are `rules_schema`, `rules_io`, and `rules_override`.

Memory floor defaults to 6144 MiB; 3000 MiB is alarm metadata, not an admission
floor. Worker cap defaults to 6, with semantic dispatch_vm=4 and prox=5 seeds.
Only a trusted host-registry adapter may supply the semantic class. These policy
defaults cannot widen explicit hosts `Caps`; admission integration is deferred.

B owns keeper edits, audit events, rollback as a forward event, last-known-good
cache, runtime wiring, and UI projection integration. R8 routing integration
comes later; role rollout remains off/shadow/on and gated on R7. No sampler,
ingest, retention or metrics payload is introduced here (HOST neighbours own it).
