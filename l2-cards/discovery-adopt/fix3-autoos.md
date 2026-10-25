# Fix round 3 (AutoOS tools/fleet_bus.py), Haiku seat findings. Same rules as writer-common.md (red test first, one commit, trailers). Nothing else.
1. check_post: "ts": 1e400 -> int(float(inf)) raises OverflowError, escapes do_POST. Treat OverflowError (and non-finite ts) as the same 400/401 refusal as other bad ts. Red test.
2. _read_json: negative Content-Length passes the BODY_MAX check and rfile.read(-n) blocks to EOF. Refuse length < 0 with 400. Red test.
3. fetch_keeper_hosts: build_opener keeps the default ProxyHandler, an http(s)_proxy env var would route the Bearer token through a proxy. Build the opener with ProxyHandler({}) (no proxies) plus the redirect refusal; close the HTTPError fp in _RefuseRedirects. Test: with http_proxy set to an unreachable local port in env, the keeper read still goes direct.
Run tests/test_fleet_bus.py; report sha + result.
