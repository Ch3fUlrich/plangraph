# FLEET-AGENT-VIEW Windows test cleanup

- agent_view tests retry temp-dir removal briefly instead of failing on a held Windows handle (rust-windows flaked on #688, #692 and #695 at the cleanup line).
