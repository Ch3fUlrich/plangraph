# PG-PROXY-ACCESS — keeper through the reverse proxy (D-713), 2026-10-08

Status: PROPOSAL (plangraph-L1). Host side = prox. Going live = parked router item (new route + listener).

## Today (interim, D-713)
- Keeper `plangraph-keeper.service` listens on 127.0.0.1:8787 only.
- `tailscale serve --https=8787` exposes it on the tailnet (100.82.253.38 / fd7a:…:8787).
- prox-L1 reaches it through an ssh forced-command relay (`~/.ssh/authorized_keys`).

## How omnigraph does it (the pattern to reuse)
`Server/server/network/opnsense/caddy.d/10-services.conf` 601-614: the Caddy vhost `omnigraph.ohje.ooguy.com`
imports `secure_headers`, answers 403 outside `192.168.0.0/16 100.64.0.0/10 fd00::/8`, and runs
`reverse_proxy coding.vm:8080`. There is no Authelia, because SSO would block MCP clients. The only auth is
`Authorization: Bearer`, checked by the app. TLS is terminated at Caddy; that is the "encrypted header" in the
item text: the bearer travels only inside TLS.

## Target
```
client (L1/L2 MCP, PG_URL=https://keeper.ohje.ooguy.com, PG_TOKEN_FILE=<principal>.token)
  --TLS--> OPNsense Caddy  keeper.ohje.ooguy.com   (secure_headers, IP allowlist as omnigraph, no Authelia)
  --LAN--> coding.vm:8788  nginx sidecar "plangraph-keeper-proxy" (host network, proxy_pass 127.0.0.1:8787)
  -------> keeper 127.0.0.1:8787 (unchanged; per-principal bearer, role checks, audit)
```
- The keeper keeps its loopback-only bind. Only the sidecar listens on the LAN, on one port (8788).
- The sidecar sets `client_max_body_size 1m`, `proxy_read_timeout 60s` (fetch-on-demand can take 20 s), and
  drops `X-Forwarded-*` from clients. The keeper does not trust them; the audit log stays principal-based.
- No new tokens: each principal keeps `~/.config/plangraph/<name>.token`. Remote hosts (workst, prox) get theirs as
  today (Taildrop / prox placement, 5.3).
- After `whoami` works from workst-L1 + prox-L1 through the proxy: `tailscale serve --https=8787 off` and remove
  the ssh relay forced command.

## Lane split
| part | owner | needs approval |
|---|---|---|
| sidecar compose `deploy/keeper-proxy/docker-compose.yml` + `nginx.conf`, ops doc, MCP config examples, test that `HttpKeeper` works with an https base URL + path prefix | plangraph-L1 (lane) | no (files only) |
| start the sidecar on coding.vm:8788 (new LAN listener) | plangraph-L1 | YES, parked router item |
| Caddy vhost `keeper.ohje.ooguy.com` + DNS | prox (Server repo) | YES, parked router item |
| tailscale serve off + ssh relay removal | plangraph-L1 + prox | YES, after both whoami checks |

## Open questions (for routing/operator)
1. Name: `keeper.ohje.ooguy.com` (proposed) or a path under `plan.ohje` (the page vhost may sit behind Authelia,
   which would block MCP clients) → proposed: separate vhost.
2. IP allowlist the same as omnigraph (LAN + tailnet). Off-tailnet clients stay unsupported, as with omnigraph.
