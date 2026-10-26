"""Durable host registry rows (``bus_hosts``): the inventory state across restarts.

The in-keeper ``fleet_bus.discovery.Registry`` loop upserts one row per
provisioned host after every refresh (``api`` wires the hook); at start-up the
rows are loaded back into the Host objects (``Registry.restore``) before the
first probe.  ``key_file`` and every other key path stay out of the table: only
``host``, ``node_id``, ``key_id``, ``endpoint``, ``health``, ``last_seen`` and
``updated_ts`` are stored.

These are persistence helpers, not keeper verbs: no principal and no event row
(there is nothing to authorise -- the keeper itself writes its own discovery
state).  The SQL is portable and runs inside ``Store._write()`` like every other
write: no raw BEGIN/COMMIT/PRAGMA (which broke Postgres in G1).
"""
from __future__ import annotations

import math

from .errors import Refused

#: the only health values the schema's CHECK accepts.
HEALTHS = ("offline", "unhealthy", "healthy")

#: the stored columns, in schema order; ``key_file`` is deliberately absent.
COLUMNS = ("host", "node_id", "key_id", "endpoint", "health", "last_seen",
           "updated_ts")


class BusHostsMixin:
    """The ``bus_hosts`` persistence seam for :class:`plangraph.keeper.store.Store`."""

    def upsert_bus_hosts(self, rows):
        """Insert or update the host rows in one write transaction.

        *rows* are mappings (or ``fleet_bus.discovery.Host`` records); each may
        carry a ``key_file``/key path, which is dropped, never stored.  Returns
        ``{"count": n}``.
        """
        if isinstance(rows, dict) or hasattr(rows, "host") or isinstance(rows, str):
            rows = [rows]
        else:
            rows = list(rows)
        prepared = [self._bus_host_row(entry) for entry in rows]
        with self._write():
            for row in prepared:
                self.conn.execute(
                    "INSERT INTO bus_hosts(host,node_id,key_id,endpoint,health,"
                    "last_seen,updated_ts) VALUES(?,?,?,?,?,?,?)"
                    " ON CONFLICT(host) DO UPDATE SET node_id=excluded.node_id,"
                    " key_id=excluded.key_id, endpoint=excluded.endpoint,"
                    " health=excluded.health, last_seen=excluded.last_seen,"
                    " updated_ts=excluded.updated_ts",
                    (row["host"], row["node_id"], row["key_id"], row["endpoint"],
                     row["health"], row["last_seen"], row["updated_ts"]))
        return {"count": len(prepared)}

    def list_bus_hosts(self):
        """Every persisted host row (schema columns only), ordered by host."""
        rows = self.conn.execute(
            "SELECT host,node_id,key_id,endpoint,health,last_seen,updated_ts"
            " FROM bus_hosts ORDER BY host").fetchall()
        return [{name: record[name] for name in COLUMNS} for record in rows]

    # ------------------------------------------------------------- helpers
    def _bus_host_row(self, entry):
        """Validate one host record into the stored columns (no key material)."""
        if isinstance(entry, dict):
            data = dict(entry)
        else:                       # fleet_bus.discovery.Host or any host record
            data = {name: getattr(entry, name, None) for name in COLUMNS}
        for name in ("key_file", "key_path", "key_dir"):   # never stored
            data.pop(name, None)
        for name in ("host", "node_id", "key_id"):
            value = data.get(name)
            if not isinstance(value, str) or not value:
                raise Refused("validation", f"host row needs a {name}")
        endpoint = data.get("endpoint")
        if endpoint is not None and not isinstance(endpoint, str):
            raise Refused("validation", "host endpoint must be text or null")
        health = data.get("health")
        health = "offline" if health is None else health
        if health not in HEALTHS:
            raise Refused("validation",
                          "host health must be offline, unhealthy or healthy")
        last_seen = data.get("last_seen")
        if last_seen is not None:
            if (isinstance(last_seen, bool) or not isinstance(last_seen, (int, float))
                    or not math.isfinite(last_seen)):
                raise Refused("validation",
                              "host last_seen must be a finite number or null")
            last_seen = float(last_seen)
        updated_ts = data.get("updated_ts")
        if updated_ts is None:
            updated_ts = int(self.now() * 1000)
        elif (isinstance(updated_ts, bool) or not isinstance(updated_ts, int)
              or not 0 <= updated_ts <= 2**63 - 1):
            raise Refused("validation", "host updated_ts must be an integer")
        return {"host": data["host"], "node_id": data["node_id"],
                "key_id": data["key_id"], "endpoint": endpoint, "health": health,
                "last_seen": last_seen, "updated_ts": updated_ts}
