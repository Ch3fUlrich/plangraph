Add the standalone Rust plangraph CLI core with user paths, environment loading,
atomic private writes, and an idempotent environment setup manifest. Keep later
AUTOOS-OUT commands out of this slice. Refuse inline PAT/KEY credentials and
correct the setup documentation's Windows CI claim.
