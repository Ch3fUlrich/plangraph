Add the standalone Rust plangraph CLI core with user paths, environment loading,
atomic private writes, and an idempotent environment setup manifest. Keep later
AUTOOS-OUT commands out of this slice. Refuse inline PAT/KEY credentials and
correct the setup documentation's Windows CI claim.

Allow symlinked ancestors above the selected setup home while refusing links at
or below it. Correct the Windows CI scope and use a scrub-safe dummy key path.
