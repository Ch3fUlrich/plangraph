# mTLS for the Temporal connection

`rust/worker` and `rust/spikes/temporal-spike` (worker + driver) read the same four env vars
(`src/tls.rs` in each crate, identical copies):

| Variable | Meaning |
|---|---|
| `TEMPORAL_TLS_CERT` | client certificate, PEM file path |
| `TEMPORAL_TLS_KEY` | client private key, PEM file path |
| `TEMPORAL_TLS_CA` | CA that signed the server certificate, PEM file path |
| `TEMPORAL_TLS_SERVER_NAME` | optional: DNS name to verify the server cert against (default: host of the address) |

- None set (empty = unset): plain connect, unchanged.
- Any of CERT/KEY/CA set: all three are required; a partial set, or SERVER_NAME alone, is refused at startup naming the missing variables. An unreadable file is refused naming the variable and path (never contents).
- The address must be `https://...` (`PG_TEMPORAL_ADDR` for the worker, `SPIKE_ADDR` for the spike), e.g. the nginx sidecar's mTLS port. nginx does not pin a server_name, so set SERVER_NAME to a SAN of the server cert when connecting by IP.

Checks: `cargo test --lib tls` (env -> options unit tests); `rust/tls-handshake-check.sh` (WSL/Linux: throwaway CA, openssl mTLS server, shows the client cert is presented and verified, and that https without TLS env is refused). The full gRPC round trip against the real frontend is the manual check for TEMPORAL-CHECK on the Temporal host.
