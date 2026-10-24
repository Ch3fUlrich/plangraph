# Keeper bus discovery

`plangraph.fleet_bus.discovery.Registry` is keeper-owned state. Provision `Host`
records with a logical host, stable tailscale status Peer map node ID, key ID,
and local key-file path. Agent aliases map to those logical hosts. Discovery
never enrolls nodes: unknown peers remain visible in `peers` but cannot resolve
or authenticate, even when they copy a registered HostName.

Each record carries endpoint, last_seen (last successful health probe), health,
and key_id. The HOST-SAMPLES-API design is absent from this snapshot; this is a
minimal registry, not an implementation of a sample store.

Call `registry.run(stop_event, probe)` from the keeper lifecycle. Supply a
deadline-bound, authenticated transport health probe. Every iteration retries
all provisioned online nodes without config edits; errors revoke availability.
Call `registry.resolve(agent_or_host)` for the current endpoint. The transport
must use `registry.validate_post(host, ...)` rather than the shared-key contract
adapter. Sender names are logical hosts, not untrusted display names. Keys are
raw files (at least 32 bytes), never environment variables or log output.

MagicDNS is preferred; HostName is the fallback. Discovery is not authentication:
the transport must authenticate health responses and all writes. Refresh and
request handling must be serialized by the keeper, like the wire contract state.
