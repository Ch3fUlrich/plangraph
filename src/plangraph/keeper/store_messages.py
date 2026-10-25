"""Durable bus rows; notifications are hints, never the source of truth."""
import hashlib
import json

from .errors import Refused


class MessagesMixin:
    def post_message(self, principal, **fields):
        """Commit a hash-linked message before returning its receipt."""
        def work(now):
            self._require_role(principal, "router", "l1", "worker", "system")
            allowed = {"from", "to", "body", "trace_id", "key", "run_id",
                       "attempt_n", "ack_state", "ts"}
            if set(fields) - allowed:
                raise ValueError("unexpected message fields")
            row = dict(fields)
            for name in ("from", "to", "body"):
                if name not in row:
                    raise ValueError("missing message field")
            if row["to"] not in {"keeper", "relay", "router", "worker", "l1", "a", "b"}:
                raise ValueError("unknown recipient")
            for name in ("from", "to", "trace_id", "key", "run_id"):
                row.setdefault(name, "")
                if not isinstance(row[name], str):
                    raise ValueError("message identifier must be text")
            row.setdefault("attempt_n", 0)
            row.setdefault("ts", int(now * 1000))
            row.setdefault("ack_state", "pending")
            for name in ("attempt_n", "ts"):
                if type(row[name]) is not int or not 0 <= row[name] <= 2**63 - 1:
                    raise ValueError("invalid message integer")
            if row["ack_state"] not in {"pending", "delivered", "failed"}:
                raise ValueError("invalid delivery state")
            json.dumps(row["body"], allow_nan=False)
            with self._write():
                return self._insert_message(**row)
        return self._run(principal, "post_message", fields.get("key"), work)

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
        allowed = {"trace_id", "key", "run_id", "ts_min", "ts_max", "since"}
        if set(filters) - allowed:
            raise Refused("validation", "unknown message filter")
        clauses, params = [], []
        for name, value in filters.items():
            column, op = {"ts_min": ("ts", ">="), "ts_max": ("ts", "<="),
                          "since": ("seq", ">")}.get(name, (name, "="))
            if name in {"ts_min", "ts_max", "since"}:
                try:
                    value = int(value)
                except (TypeError, ValueError):
                    raise Refused("validation", "invalid message cursor") from None
                if value < 0 or value > 2**63 - 1:
                    raise Refused("validation", "invalid message cursor")
            clauses.append(f'"{column}" {op} ?')
            params.append(value)
        sql = "SELECT * FROM messages" + (" WHERE " + " AND ".join(clauses) if clauses else "")
        rows = []
        for record in self.conn.execute(sql + " ORDER BY seq LIMIT 1000", params).fetchall():
            row = dict(record)
            row["body"] = json.loads(row["body"])
            rows.append(row)
        return rows
