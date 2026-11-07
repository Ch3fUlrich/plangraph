"""C4 authenticated SQL adapter; all placement value policy remains in Rust."""
import json

from . import auth
from .errors import Refused
from .placement_rules_bridge import call_rules
from .placement_rules_report import bounded_snapshot, build_report


def _invalid():
    raise Refused("rules_invalid", "invalid placement rules request")


class PlacementRulesMixin:
    def placement_rules_report(self, principal):
        def read(now):
            self._rules_authorize(principal)
            with self._write():
                self._rules_authorize(principal)
                snapshot = bounded_snapshot(self._placement_rules_snapshot())
            return build_report(snapshot)
        return self._run(principal, "placement_rules_report", None, read)

    def placement_rule_specs(self, principal):
        def read(now):
            self._rules_authorize(principal)
            with self._write():
                self._rules_authorize(principal)
            return call_rules({"op": "specs"})["specs"]
        return self._run(principal, "placement_rule_specs", None, read)

    def _placement_rules_projection(self):
        """Internal snapshot.fleet hook: caller owns transaction and auth."""
        return build_report(bounded_snapshot(self._placement_rules_snapshot()))

    def _rules_authorize(self, principal, router=False):
        if (isinstance(principal, (auth.RunPrincipal, auth.OversightPrincipal))
                or principal.role == "system"):
            raise Refused("forbidden", "persistent placement rules identity required")
        self._check_principal(principal)
        if router:
            self._require_role(principal, "router")

    def placement_rules(self, principal):
        def read(now):
            self._rules_authorize(principal)
            with self._write():
                self._rules_authorize(principal)
                return self._placement_rules_snapshot()
        return self._run(principal, "placement_rules", None, read)

    def edit_placement_rule(self, principal, request):
        def edit(now):
            self._rules_authorize(principal, router=True)
            return self._edit_placement_rule(principal, request, now)
        return self._run(principal, "edit_placement_rule", None, edit)

    def placement_rule_audit(self, principal, since=0, limit=100):
        def read(now):
            self._rules_authorize(principal, router=True)
            if (type(since) is not int or not 0 <= since <= 2**63 - 1
                    or type(limit) is not int or not 1 <= limit <= 100):
                _invalid()
            with self._write():
                self._rules_authorize(principal, router=True)
                return self._placement_rule_audit(since, limit)
        return self._run(principal, "placement_rule_audit", None, read)

    def _placement_rules_snapshot(self):
        row = self.conn.execute(
            "SELECT revision FROM placement_rule_revision WHERE id=1").fetchone()
        snapshot = dict(revision=row["revision"] if row else 0, fleet={}, hosts={})
        for row in self.conn.execute(
                "SELECT scope,host,key,value FROM placement_rule_values").fetchall():
            layer = (snapshot["fleet"] if row["scope"] == "fleet" else
                     snapshot["hosts"].setdefault(row["host"], {}))
            layer[row["key"]] = row["value"]
        return snapshot

    @staticmethod
    def _rule_event(row):
        return dict(revision=row["revision"], actor=row["actor"],
                    edit=json.loads(row["edit"]), previous=row["previous"])

    def _placement_rule_audit(self, since, limit):
        rows = self.conn.execute(
            "SELECT revision,actor,edit,previous FROM placement_rule_audit "
            "WHERE revision>? ORDER BY revision ASC LIMIT ?", (since, limit)).fetchall()
        events = [self._rule_event(row) for row in rows]
        return dict(events=events, next_revision=events[-1]["revision"] if events else since)

    def _edit_placement_rule(self, principal, request, now):
        with self._write():
            self._rules_authorize(principal, router=True)
            # Option fields are mandatory on this adapter even though serde accepts omission.
            if (type(request) is not dict or set(request) != {
                    "scope", "key", "value", "expected_revision", "rollback_of"}):
                _invalid()
            try:
                encoded = json.dumps(request, allow_nan=False, sort_keys=True, separators=(",", ":"))
                request = json.loads(encoded)
            except (ValueError, TypeError, RecursionError):
                _invalid()
            call_rules({"op": "validate", "edit": request})
            scope = "fleet" if request["scope"] == "Fleet" else "host"
            host = "" if scope == "fleet" else request["scope"]["Host"]
            if scope == "host":
                bounded_snapshot(dict(hosts={host: {}}))
            key, value = request["key"], request["value"]
            self.conn.execute("INSERT INTO placement_rule_revision(id,revision) VALUES(1,0) "
                              "ON CONFLICT(id) DO NOTHING")
            updated = self.conn.execute(
                "UPDATE placement_rule_revision SET revision=revision+1 "
                "WHERE id=1 AND revision=? AND revision<?",
                (request["expected_revision"], 2**63 - 1))
            if updated.rowcount != 1:
                raise Refused("rules_conflict", "placement rules revision changed")
            row = self.conn.execute(
                "SELECT value FROM placement_rule_values WHERE scope=? AND host=? AND key=?",
                (scope, host, key)).fetchone()
            previous = row["value"] if row else None
            if value is None and row is None:
                _invalid()
            if request["rollback_of"] is not None:
                target = self.conn.execute(
                    "SELECT revision,actor,edit,previous FROM placement_rule_audit "
                    "WHERE revision=?", (request["rollback_of"],)).fetchone()
                if target is None:
                    _invalid()
                event = self._rule_event(target)
                if (event["edit"]["scope"] != request["scope"]
                        or event["edit"]["key"] != key or event["previous"] != value):
                    _invalid()
            if value is None:
                self.conn.execute(
                    "DELETE FROM placement_rule_values WHERE scope=? AND host=? AND key=?",
                    (scope, host, key))
            else:
                self.conn.execute(
                    "INSERT INTO placement_rule_values(scope,host,key,value) VALUES(?,?,?,?) "
                    "ON CONFLICT(scope,host,key) DO UPDATE SET value=excluded.value",
                    (scope, host, key, value))
            revision = request["expected_revision"] + 1
            bounded_snapshot(self._placement_rules_snapshot())
            self.conn.execute(
                "INSERT INTO placement_rule_audit(revision,actor,edit,previous,ts) "
                "VALUES(?,?,?,?,?)", (revision, principal.name, encoded, previous, self._iso(now)))
            result = dict(revision=revision, actor=principal.name, edit=request, previous=previous)
        return result
