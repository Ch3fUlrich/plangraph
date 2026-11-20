Pg-jail's escape test starts its expected-to-fail systemd scopes with `--collect` and asserts none stays failed, so `cargo test -p pg-jail` no longer leaves failed `run-u*.scope` units behind.
