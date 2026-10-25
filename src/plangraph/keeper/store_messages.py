"""Durable bus rows; notifications are hints, never the source of truth."""
import hashlib
import json
import re
from pathlib import Path

from ..fleet_bus.contract import BODY_MAX, BusError, canonical_post, new_state
from .errors import Refused

_AGENT_TO_RE = re.compile(r'^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,62}[A-Za-z0-9])?$')


class MessagesMixin:
    def post_message(self, principal, registry=None, fields=None, **extra_fields):
        """Commit a hash-linked message before returning its receipt."""
        fields = dict(fields) if fields is not None else dict(extra_fields)
        registry = registry or fields.pop("registry", None) or getattr(self, "bus_registry", None)

        def work(now):
            self._require_role(principal, "router", "l1", "worker", "system")
            allowed = {"from", "to", "body", "trace_id", "key", "run_id",
                       "attempt_n", "ack_state", "ts", "envelope", "msg_id"}
            if set(fields) - allowed:
                raise Refused("validation", "unexpected message fields")
            row = dict(fields)
            for name in ("from", "to", "body"):
                if name not in row:
                    raise Refused("validation", "missing message field")
            if not isinstance(row["to"], str) or not _AGENT_TO_RE.fullmatch(row["to"]):
                raise Refused("validation", "unknown recipient")
            for name in ("from", "to", "trace_id", "key", "run_id"):
                row.setdefault(name, "")
                if not isinstance(row[name], str):
                    raise Refused("validation", "message identifier must be text")
            row.setdefault("attempt_n", 0)
            row.setdefault("ts", int(now * 1000))
            row.setdefault("ack_state", "pending")
            for name in ("attempt_n", "ts"):
                if type(row[name]) is not int or not 0 <= row[name] <= 2**63 - 1:
                    raise Refused("validation", "invalid message integer")
            if row["ack_state"] not in {"pending", "delivered", "failed"}:
                raise Refused("validation", "invalid delivery state")
            try:
                body_str = json.dumps(row["body"], allow_nan=False)
            except (ValueError, TypeError):
                raise Refused("validation", "message body must be JSON serializable") from None
            if len(body_str.encode("utf-8")) > BODY_MAX:
                raise Refused("too_large", f"message body exceeds {BODY_MAX} bytes")

            envelope = fields.get("envelope")
            msg_id = fields.get("msg_id")

            if msg_id is not None:
                if not isinstance(msg_id, str):
                    raise Refused("bad_request", "msg_id must be a string")

            if envelope is not None:
                if not isinstance(envelope, dict):
                    raise Refused("bad_request", "envelope must be a JSON object")
                for req in ("host", "sig", "nonce", "ts", "msg_id"):
                    if req not in envelope:
                        raise Refused("bad_request", f"envelope missing field {req!r}")
                    if req in ("host", "sig", "nonce", "msg_id") and not isinstance(envelope[req], str):
                        raise Refused("bad_request", f"invalid type for envelope field {req!r}")
                    if req == "ts" and not isinstance(envelope[req], (int, float, str)):
                        raise Refused("bad_request", f"invalid type for envelope field {req!r}")
                if msg_id and msg_id != envelope["msg_id"]:
                    raise Refused("bad_request", "envelope msg_id mismatch")
                msg_id = str(envelope["msg_id"])

                # G1-1: Signed envelope requires body == {"text": <str>} and verifies against that str.
                if not isinstance(row["body"], dict) or list(row["body"].keys()) != ["text"] or not isinstance(row["body"]["text"], str):
                    raise Refused("validation", "signed envelope requires body {'text': <str>}")

                # V2-3: 'from' must equal signed host
                if row["from"] != envelope["host"]:
                    raise Refused("forbidden", "from does not match signed host")

                if registry is None:
                    raise Refused("unknown_kid", f"no registry configured for host {envelope['host']!r}")
                host = registry.hosts.get(envelope["host"])
                if host is None:
                    raise Refused("unknown_kid", f"unknown host {envelope['host']!r}")

                # V2-7: refuse an envelope whose kid differs from host key id; do not take it from envelope
                if "kid" in envelope and envelope["kid"] is not None and envelope["kid"] != host.key_id:
                    raise Refused("unknown_kid", "envelope kid does not match host key id")

                wire = {
                    "from": envelope["host"],
                    "to": row["to"],
                    "ts": envelope["ts"],
                    "nonce": envelope["nonce"],
                    "msg_id": envelope["msg_id"],
                    "kid": host.key_id,
                    "text": row["body"]["text"],
                }
                if row.get("attempt_n"):
                    wire["attempt_n"] = row["attempt_n"]
                wire_bytes = json.dumps(wire).encode("utf-8")

                bus_state = getattr(self, "bus_state", None)
                if bus_state is None:
                    bus_state = self.bus_state = new_state()
                try:
                    registry.validate_post(envelope["host"], wire_bytes, envelope["sig"], now, bus_state)
                except BusError as exc:
                    raise Refused(exc.code, str(exc)) from exc
            else:
                # G1-6: Unsigned post: router/system may set 'from' only to their own name or a registered host
                if principal.role in ("router", "system"):
                    is_self = (row["from"] == principal.name)
                    is_host = bool(registry and row["from"] in registry.hosts)
                    is_relay = (row["from"] == "relay")
                    if not (is_self or is_host or is_relay):
                        raise Refused("forbidden", "from must be own name or a registered host")
                else:
                    if row["from"] != principal.name:
                        raise Refused("forbidden", "from does not match Bearer principal")

            # Envelope cryptographic validation is deliberately performed before acquiring self._write()
            # so that CPU-bound HMAC verification does not hold the database write lock.
            # Nonce uniqueness, idempotency, and row insertion are then executed atomically inside the write lock.
            with self._write():
                if envelope is not None:
                    seen = self.conn.execute("SELECT nonce FROM message_nonces WHERE nonce = ?", (envelope["nonce"],)).fetchone()
                    if seen:
                        raise Refused("replayed_nonce", "nonce already seen")

                body_hash = None
                if msg_id:
                    content_obj = {
                        "from": row["from"],
                        "to": row["to"],
                        "body": row["body"],
                    }
                    for extra in ("trace_id", "key", "run_id", "attempt_n"):
                        if extra in row and row[extra]:
                            content_obj[extra] = row[extra]
                    body_hash = hashlib.sha256(json.dumps(content_obj, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()

                    existing = self.conn.execute("SELECT seq, body_hash FROM message_envelopes WHERE msg_id = ?", (msg_id,)).fetchone()
                    if existing:
                        if existing["body_hash"] == body_hash:
                            # V2-4: record nonce on idempotent retry path too
                            if envelope is not None:
                                self.conn.execute(
                                    'INSERT INTO message_nonces (nonce, host, ts) VALUES (?, ?, ?)',
                                    (envelope["nonce"], envelope["host"], int(now * 1000)),
                                )
                            msg_row = self.conn.execute("SELECT * FROM messages WHERE seq = ?", (existing["seq"],)).fetchone()
                            ret = dict(msg_row)
                            ret["body"] = json.loads(ret["body"])
                            ret["msg_id"] = msg_id
                            return ret
                        # V2-4: record nonce on conflict path too before raising
                        if envelope is not None:
                            self.conn.execute(
                                'INSERT INTO message_nonces (nonce, host, ts) VALUES (?, ?, ?)',
                                (envelope["nonce"], envelope["host"], int(now * 1000)),
                            )
                            self.conn.execute("COMMIT")
                            self.conn.execute("BEGIN IMMEDIATE")
                        raise Refused("conflict", "msg_id reused with different content")

                msg_fields = {k: v for k, v in row.items() if k not in ("envelope", "msg_id")}
                msg_fields["posted_by"] = principal.name
                inserted = self._insert_message(**msg_fields)

                if msg_id:
                    host_name = envelope["host"] if envelope else row["from"]
                    self.conn.execute(
                        'INSERT INTO message_envelopes (msg_id, seq, body_hash, host, ts) VALUES (?, ?, ?, ?, ?)',
                        (msg_id, inserted["seq"], body_hash, host_name, int(now * 1000)),
                    )
                    inserted["msg_id"] = msg_id

                if envelope is not None:
                    self.conn.execute(
                        'INSERT INTO message_nonces (nonce, host, ts) VALUES (?, ?, ?)',
                        (envelope["nonce"], envelope["host"], int(now * 1000)),
                    )

                self._write_outbox_metadata(inserted)
                return inserted

        return self._run(principal, "post_message", fields.get("key"), work)

    def _write_outbox_metadata(self, inserted):
        try:
            db_file = None
            for row in self.conn.execute("PRAGMA database_list").fetchall():
                if row[1] == "main" and row[2]:
                    db_file = Path(row[2])
                    break
            if db_file:
                outbox_path = db_file.parent / "outbox.jsonl"
                meta = {k: v for k, v in inserted.items() if k != "body"}
                with open(outbox_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(meta, sort_keys=True) + "\n")
        except Exception:
            pass

    def _insert_message(self, **row):
        last = self.conn.execute('SELECT seq,hash FROM messages ORDER BY seq DESC LIMIT 1').fetchone()
        row["seq"] = last["seq"] + 1 if last else 1
        row["prev_hash"] = last["hash"] if last else "0" * 64
        payload = json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        row["hash"] = hashlib.sha256((row["prev_hash"] + payload).encode()).hexdigest()
        names = list(row)
        columns = ",".join('"' + n + '"' for n in names)
        values = [json.dumps(row[n], allow_nan=False) if n == "body" else row[n] for n in names]
        self.conn.execute(f'INSERT INTO messages ({columns}) VALUES ({",".join("?" for _ in names)})', values)
        return row

    def messages(self, principal, **filters):
        """Read ordered rows using parameterized cursor and linkage filters."""
        self._check_principal(principal)
        allowed = {"trace_id", "key", "run_id", "ts_min", "ts_max", "since", "msg_id"}
        if set(filters) - allowed:
            raise Refused("validation", "unknown message filter")
        clauses, params = [], []
        for name, value in filters.items():
            if name == "msg_id":
                clauses.append('e.msg_id = ?')
                params.append(value)
                continue
            column, op = {"ts_min": ("ts", ">="), "ts_max": ("ts", "<="),
                          "since": ("seq", ">")}.get(name, (name, "="))
            if name in {"ts_min", "ts_max", "since"}:
                try:
                    value = int(value)
                except (TypeError, ValueError):
                    raise Refused("validation", "invalid message cursor") from None
                if value < 0 or value > 2**63 - 1:
                    raise Refused("validation", "invalid message cursor")
            clauses.append(f'm."{column}" {op} ?')
            params.append(value)
        sql = """
            SELECT m.*, e.msg_id
            FROM messages m
            LEFT JOIN message_envelopes e ON m.seq = e.seq
        """ + (" WHERE " + " AND ".join(clauses) if clauses else "")
        rows = []
        for record in self.conn.execute(sql + " ORDER BY m.seq LIMIT 1000", params).fetchall():
            row = dict(record)
            row["body"] = json.loads(row["body"])
            if row.get("msg_id") is None:
                row.pop("msg_id", None)
            rows.append(row)
        return rows
