# keeper-proxy (PG-PROXY-ACCESS)

Design: `docs/proposals/2026-10-08-pg-proxy-access.md`. Same pattern as omnigraph.

The 192.0.2.x placeholder addresses in `nginx.conf` fail closed (no real peer matches them); substitute the actual LAN/edge addresses before go-live.

## Going live (each step needs its parked router item; D-759 option A = mutual TLS on the LAN hop)
0. Operator (shared "internal hop CA", also used by the omnigraph hop):
   `mint-certs.sh ca <offline-ca-dir>` once; `mint-certs.sh server <ca-dir> deploy/keeper-proxy/tls keeper.example.invalid
   192.0.2.10`; `mint-certs.sh client <ca-dir> <edge-dir>` once for the Caddy edge. `tls/` is git-ignored.
   Copy `ca.crt client.crt client.key` to the Caddy host at `/usr/local/etc/caddy/internal-ca/` (root-only).
1. PG-PROXY-LISTENER: `docker compose -f deploy/keeper-proxy/docker-compose.yml up -d` on keeper.example.invalid, then
   `curl -s http://127.0.0.1:8789/healthz` → `ok`, and
   `curl --cacert tls/ca.crt --cert tls/client.crt --key tls/client.key https://keeper.example.invalid:8788/v1/whoami` → 401
   (TLS ok, no bearer), while `curl --cacert tls/ca.crt https://keeper.example.invalid:8788/v1/whoami` fails the handshake.
2. PG-PROXY-VHOST (site router; the vhost file goes in the Server repo's Caddy vhost directory, `<caddy-vhost-dir>`):
   ```
   [DOMAIN] {
       import secure_headers
       @outside not remote_ip 192.0.2.0/24 198.51.100.0/24 <LAN IPv6 prefix>
       respond @outside 403
       reverse_proxy https://keeper.example.invalid:8788 {
           transport http {
               # Caddy >= 2.9: tls_trust_pool file <ca>; older Caddy: tls_trusted_ca_certs <ca>
               tls_trust_pool file /usr/local/etc/caddy/internal-ca/ca.crt
               tls_server_name keeper.example.invalid
               tls_client_auth /usr/local/etc/caddy/internal-ca/client.crt /usr/local/etc/caddy/internal-ca/client.key
           }
       }
   }
   ```
   The address blocks here are documentation ones (RFC 5737 for IPv4, RFC 3849 for the IPv6 prefix):
   swap in the real LAN ranges at go-live.
   Never `tls_insecure_skip_verify`: the sidecar cert is checked against the keeper-proxy CA only (not the
   system pool) and must name `keeper.example.invalid`.
   No Authelia (it would block MCP clients). Auth = the keeper's per-principal bearer, inside TLS.
   The Caddy -> sidecar hop is mutual TLS (D-759 A); the sidecar also accepts only the edge IP (192.0.2.11,
   check it) and 127.0.0.1. Fallback if the operator declines the CA: option B (plain hop, as omnigraph, plus an
   OPNsense rule allowing 8788 only from the edge).
3. Check from each remote L1: MCP env `PG_URL=https://[DOMAIN]`,
   `PG_TOKEN_FILE=~/.config/plangraph/<name>.token` → `whoami` answers with its principal.
4. PG-PROXY-CUTOVER: retire the legacy serve endpoint (`tailscale serve --https=<legacy-port> off`); remove the ssh relay forced command for `<site>-L1`.
