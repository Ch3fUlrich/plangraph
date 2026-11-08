"""Card storage and project authorization."""
from .store_card_values import *
from .store_card_values import (_dump, _load_list, _text, _str_list, _count, _number, _exec_parts, _forward, _CARD_KEYS, _CARD_LISTS, _HAND_BACK_STATES)
from .store_card_questions import CardQuestionsMixin
from .store_card_updates import CardUpdatesMixin


class CardsMixin(ExecMixin, CardQuestionsMixin, CardUpdatesMixin):
    """The W2 verbs for :class:`plangraph.keeper.store.Store`."""

    # ------------------------------------------------------------- helpers
    def _may_write_for(self, principal, project):
        """router/system anywhere; an l1 only in its own project."""
        if principal.role in ("router", "system"):
            return
        self._may_manage(principal, project)

    def _may_act_in(self, principal, project):
        if principal.role in ("router", "system"):
            return
        self._require_role(principal, "l1", "worker", "spawner", "phase-worker")
        self._require_project(principal, project)

    def _iso_or_none(self, name, value):
        if value is None:
            return None
        if not isinstance(value, str) or self._parse_iso(value) is None:
            raise Refused("validation", f"{name} must be an ISO timestamp or null")
        return value

    # ---------------------------------------------------------------- cards
    def put_card(self, principal, key, card):
        return self._run(principal, "put_card", key,
                         lambda now: self._put_card(principal, key, card, now))

    def _put_card(self, principal, key, card, now):
        self._require_role(principal, "router", "l1", "system")
        if not isinstance(card, dict):
            raise Refused("validation", "card must be an object")
        unknown = set(card) - _CARD_KEYS
        if unknown:
            raise Refused("validation", f"unknown card fields: {sorted(unknown)}")
        window = card.get("window_class")
        if window is not None and window not in WINDOW_CLASSES:
            raise Refused("validation", f"window_class must be one of {list(WINDOW_CLASSES)}")
        check_kind = card.get("check_kind", "change")
        if check_kind not in ("change", "regression"):
            raise Refused("validation", "check_kind must be change or regression")
        values = {
            "check_kind": check_kind,
            "outcome": _text("outcome", card.get("outcome"), required=True, key=key),
            "check_cmd": _text("check_cmd", card.get("check_cmd"), key=key),
            "heldout_check": _text("heldout_check", card.get("heldout_check"), key=key),
            "window_class": window,
            "risk_tags": _dump(_str_list("risk_tags", card.get("risk_tags"),
                                         RISK_TAGS, key=key)),
            "estimate_tokens": _count("estimate_tokens", card.get("estimate_tokens")),
        }
        for name in _CARD_LISTS:
            entries = _str_list(name, card.get(name), key=key)
            if name == "check_reads":
                normalised = []
                for entry in entries:
                    path = normalise_path(entry)
                    problem = path_problem(name, entry, path)
                    if problem:
                        raise Refused("validation", problem)
                    if path not in normalised:
                        normalised.append(path)
                entries = normalised
            values[name] = _dump(entries)
        with self._write():
            item = self._get_or_refuse(key)
            self._may_write_for(principal, item["project"])
            self._assert_unlocked(item)
            cols = ["outcome", *_CARD_LISTS, "check_kind", "check_cmd", "heldout_check",
                    "window_class", "risk_tags", "estimate_tokens"]
            marks = ",".join("?" for _ in cols)
            sets = ",".join(f"{c}=excluded.{c}" for c in cols)
            self.conn.execute(
                f"INSERT INTO cards(key,{','.join(cols)},updated_at)"
                f" VALUES(?,{marks},?)"
                f" ON CONFLICT(key) DO UPDATE SET {sets},updated_at=excluded.updated_at",
                (key, *[values[c] for c in cols], self._iso(now)))
        return {"key": key}

    def get_card(self, principal, key):
        return self._run(principal, "get_card", key, lambda now: self._get_card(key))

    def _get_card(self, key):
        row = self.conn.execute("SELECT * FROM cards WHERE key=?", (key,)).fetchone()
        if row is None:
            raise Refused("not_found", f"no card for {key!r}")
        card = dict(row)
        for name in (*_CARD_LISTS, "risk_tags"):
            card[name] = _load_list(card[name])
        return card

