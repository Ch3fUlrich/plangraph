# keeper-proxy (PG-PROXY-ACCESS)

Design: `docs/proposals/2026-10-08-pg-proxy-access.md`. Same pattern as omnigraph.

## Going live (each step needs its parked router item)
1. PG-PROXY-LISTENER: `docker compose -f deploy/keeper-proxy/docker-compose.yml up -d` on coding.vm, then
   `curl -s http://192.168.178.73:8788/healthz` → `ok`.
2. PG-PROXY-VHOST (prox, Server repo `server/network/opnsense/caddy.d/`):
   ```
   keeper.ohje.ooguy.com {
       import secure_headers
       @outside not remote_ip 192.168.0.0/16 100.64.0.0/10 fd00::/8
       respond @outside 403
       reverse_proxy coding.vm:8788
   }
   ```
   No Authelia (it would block MCP clients). Auth = the keeper's per-principal bearer, inside TLS.
3. Check from each remote L1: MCP env `PG_URL=https://keeper.ohje.ooguy.com`,
   `PG_TOKEN_FILE=~/.config/plangraph/<name>.token` → `whoami` answers with its principal.
4. PG-PROXY-CUTOVER: `tailscale serve --https=8787 off`; remove the ssh relay forced command for prox-L1.
