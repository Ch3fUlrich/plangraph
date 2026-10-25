# Fix round 1 (AutoOS tools/fleet_bus.py), from review seat HOLD. Same rules as writer-common.md (red test first, trailers, no secrets/IP literals).
1. BLOCKING: main() hands the discovered bind to FleetBusServer((bind, port), ...); an OSError (unresolvable/unowned address, IPv6 where server is AF_INET) is an uncaught traceback. Catch OSError around construction, print the one-line JSON error naming the address (not secrets) and return status 3 like the other discovery failures. Red test: bind to an unowned address (e.g. 192.0.2.1 placeholder) -> exit 3, one line, no traceback.
2. Refuse the wildcard: --bind / FLEET_BUS_BIND / discovered endpoint equal to 0.0.0.0, "::" or "" -> fail closed (status 2/3 as fits), because the read path is open. Test each.
3. Test that --bind short-circuits discovery at main() level: main(["--open","--bind","127.0.0.1","--port","0",...]) with empty env and no keeper settings starts without any keeper request (use ttl so it exits).
4. --secret-file together with --inventory: refuse with a one-line error (status 2). Test.
Do not change anything else. Run tests/test_fleet_bus.py; report sha + result line.
