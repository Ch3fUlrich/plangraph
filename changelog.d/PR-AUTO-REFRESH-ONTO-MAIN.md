Added a Rust batch-ready PR refresh sweep to pg-repo-sync, usable after main
updates or through opt-in refresh flags on a dedicated sync checkout. Scratch
worktrees preserve Git union merges and installed hooks; real conflicts receive
needs-update and an orchestrator alert without a push. Rust fixtures cover union
pushes, conflicts, automatic sync integration, idempotence and push-hook refusal.
Main-update event listener/timer deployment remains separate.
