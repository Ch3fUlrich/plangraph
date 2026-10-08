# keeper-proxy (PG-PROXY-ACCESS)

Design: `docs/proposals/2026-10-08-pg-proxy-access.md`. Same pattern as omnigraph.

## Going live (each step needs its parked router item; D-759 option A = mutual TLS on the LAN hop)
0. Operator (shared "ohje internal hop CA", also used by the omnigraph hop):
   `mint-certs.sh ca <offline-ca-dir>` once; `mint-certs.sh server <ca-dir> deploy/keeper-proxy/tls coding.vm
   192.168.178.73`; `mint-certs.sh client <ca-dir> <edge-dir>` once for the Caddy edge. `tls/` is git-ignored.
   Copy `ca.crt client.crt client.key` to the Caddy host at `/usr/local/etc/caddy/internal-ca/` (root-only).
1. PG-PROXY-LISTENER: `docker compose -f deploy/keeper-proxy/docker-compose.yml up -d` on coding.vm, then
   `curl -s http://127.0.0.1:8789/healthz` → `ok`, and
   `curl --cacert tls/ca.crt --cert tls/client.crt --key tls/client.key https://coding.vm:8788/v1/whoami` → 401
   (TLS ok, no bearer), while `curl --cacert tls/ca.crt https://coding.vm:8788/v1/whoami` fails the handshake.
2. PG-PROXY-VHOST (prox, Server repo `server/network/opnsense/caddy.d/`):
   ```
   keeper.ohje.ooguy.com {
       import secure_headers
       @outside not remote_ip 192.168.0.0/16 100.64.0.0/10 fd00::/8
       respond @outside 403
       reverse_proxy https://coding.vm:8788 {
           transport http {
               # Caddy >= 2.9: tls_trust_pool file <ca>; older Caddy: tls_trusted_ca_certs <ca>
               tls_trust_pool file /usr/local/etc/caddy/internal-ca/ca.crt
               tls_server_name coding.vm
               tls_client_auth /usr/local/etc/caddy/internal-ca/client.crt /usr/local/etc/caddy/internal-ca/client.key
           }
       }
   }
   ```
   Never `tls_insecure_skip_verify`: the sidecar cert is checked against the keeper-proxy CA only (not the
   system pool) and must name `coding.vm`.
   No Authelia (it would block MCP clients). Auth = the keeper's per-principal bearer, inside TLS.
   The Caddy -> sidecar hop is mutual TLS (D-759 A); the sidecar also accepts only the edge IP (192.168.178.76,
   check it) and 127.0.0.1. Fallback if the operator declines the CA: option B (plain hop, as omnigraph, plus an
   OPNsense rule allowing 8788 only from the edge).
3. Check from each remote L1: MCP env `PG_URL=https://keeper.ohje.ooguy.com`,
   `PG_TOKEN_FILE=~/.config/plangraph/<name>.token` → `whoami` answers with its principal.
4. PG-PROXY-CUTOVER: `tailscale serve --https=8787 off`; remove the ssh relay forced command for prox-L1.
