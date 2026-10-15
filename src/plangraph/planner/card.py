"""Planner card schema, validation and import from batch YAML.

A card is the 1:1 execution brief of an item (see ``Store.put_card`` in
``plangraph.keeper.store_cards``).  This module validates a card dict the same way the
store does, but collects *all* problems before refusing, normalises path entries and
adds the batch glue:

* :class:`Card` / :func:`parse_card`: frozen card plus a collecting validator
  (:class:`CardError` carries ``key`` and ``problems``).
* :func:`cards_from_batch`: read cards from a loaded batch document, either from a
  top-level ``cards:`` mapping or from inline ``card:`` mappings on items.
* :func:`strip_cards`: copy of the document without any card, so the item batch stays
  importable (the keeper importer refuses unknown item fields).
* :func:`load_cards_yaml` / :func:`apply_cards`: file loader and the store writer.
"""
from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from plangraph.keeper.schema import RISK_TAGS, WINDOW_CLASSES

__all__ = ["Card", "CardError", "parse_card", "cards_from_batch", "strip_cards",
           "load_cards_yaml", "apply_cards"]

_TEXT_CAP = 2000
_LIST_CAP = 500
_ITEM_CAP = 500
_LISTS = ("read_set", "write_set", "exports", "forbid_paths", "smoke_checks")
_PATH_LISTS = ("read_set", "write_set", "forbid_paths")
_TEXTS = ("check_cmd", "heldout_check")
_FIELDS = frozenset(("outcome", "window_class", "risk_tags", "estimate_tokens",
                     *_LISTS, *_TEXTS))
_DRIVE_RE = re.compile(r"[A-Za-z]:")


class CardError(ValueError):
    """A card (or batch of cards) failed validation; ``problems`` lists every cause."""

    def __init__(self, problems: list[str], key: str | None = None) -> None:
        self.key = key
        self.problems = list(problems)
        prefix = f"{key}: " if key else ""
        super().__init__(prefix + "; ".join(self.problems))


@dataclass(frozen=True)
class Card:
    """One validated card; list fields are tuples so the card is hashable and frozen."""

    outcome: str
    read_set: tuple[str, ...] = ()
    write_set: tuple[str, ...] = ()
    exports: tuple[str, ...] = ()
    forbid_paths: tuple[str, ...] = ()
    smoke_checks: tuple[str, ...] = ()
    check_cmd: str | None = None
    heldout_check: str | None = None
    window_class: str | None = None
    risk_tags: tuple[str, ...] = ()
    estimate_tokens: int | None = None

    def to_store(self) -> dict[str, Any]:
        """The card dict ``Store.put_card`` accepts (lists as lists, every key present)."""
        return {
            "outcome": self.outcome,
            "read_set": list(self.read_set),
            "write_set": list(self.write_set),
            "exports": list(self.exports),
            "forbid_paths": list(self.forbid_paths),
            "smoke_checks": list(self.smoke_checks),
            "check_cmd": self.check_cmd,
            "heldout_check": self.heldout_check,
            "window_class": self.window_class,
            "risk_tags": list(self.risk_tags),
            "estimate_tokens": self.estimate_tokens,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Card":
        """Validate ``raw`` (see :func:`parse_card`) and build the card."""
        return parse_card(raw)


def _normalise_path(value: str) -> str | None:
    """Strip, use forward slashes, drop leading ``./``; ``None`` if nothing is left."""
    path = value.strip().replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return path or None


def _path_problem(name: str, original: str, path: str | None) -> str | None:
    if path is None:
        return f"{name} entry {original!r} is empty after normalisation"
    if path.startswith("/") or _DRIVE_RE.match(path):
        return f"{name} entry {original!r} must be a relative path"
    if ".." in path.split("/"):
        return f"{name} entry {original!r} must not contain '..'"
    return None


def _check_list(name: str, value: Any, problems: list[str], paths: bool = False,
                allowed: tuple[str, ...] | None = None) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)) or len(value) > _LIST_CAP:
        problems.append(f"{name} must be a list of at most {_LIST_CAP} strings")
        return ()
    out: list[str] = []
    for entry in value:
        if not isinstance(entry, str) or not entry.strip() or len(entry) > _ITEM_CAP:
            problems.append(f"{name} entries must be non-empty strings of at most"
                            f" {_ITEM_CAP} characters")
            continue
        if allowed is not None and entry not in allowed:
            problems.append(f"{name} entry {entry!r} is not one of {list(allowed)}")
            continue
        if paths:
            normal = _normalise_path(entry)
            issue = _path_problem(name, entry, normal)
            if issue:
                problems.append(issue)
                continue
            entry = normal  # type: ignore[assignment]
        if entry not in out:
            out.append(entry)
    return tuple(out)


def _check_text(name: str, value: Any, problems: list[str],
                required: bool = False) -> str | None:
    if value is None:
        if required:
            problems.append(f"{name} is required")
        return None
    if not isinstance(value, str) or (required and not value.strip()):
        problems.append(f"{name} must be a non-empty string" if required
                        else f"{name} must be a string or null")
        return None
    if len(value) > _TEXT_CAP:
        problems.append(f"{name} exceeds {_TEXT_CAP} characters")
        return None
    return value


def parse_card(raw: Any, key: str | None = None) -> Card:
    """Validate ``raw`` like the store does and return a :class:`Card`.

    Raises :class:`CardError` listing *every* problem found (not just the first).
    """
    if not isinstance(raw, dict):
        raise CardError(["card must be an object"], key)
    problems: list[str] = []
    unknown = sorted(str(k) for k in set(raw) - _FIELDS)
    if unknown:
        problems.append(f"unknown card fields: {unknown}")
    window = raw.get("window_class")
    if window is not None and (not isinstance(window, str) or window not in WINDOW_CLASSES):
        problems.append(f"window_class must be one of {list(WINDOW_CLASSES)}")
        window = None
    tokens = raw.get("estimate_tokens")
    if tokens is not None and (isinstance(tokens, bool) or not isinstance(tokens, int)
                               or tokens < 0):
        problems.append("estimate_tokens must be a non-negative integer or null")
        tokens = None
    values: dict[str, Any] = {
        "outcome": _check_text("outcome", raw.get("outcome"), problems, required=True),
        "window_class": window,
        "estimate_tokens": tokens,
        "risk_tags": _check_list("risk_tags", raw.get("risk_tags"), problems,
                                 allowed=RISK_TAGS),
    }
    for name in _TEXTS:
        values[name] = _check_text(name, raw.get(name), problems)
    for name in _LISTS:
        values[name] = _check_list(name, raw.get(name), problems, paths=name in _PATH_LISTS)
    if problems:
        raise CardError(problems, key)
    return Card(**values)


# ------------------------------------------------------------------ batch glue
def _item_lists(doc: Any) -> Iterator[list]:
    """Yield every list in ``doc`` that holds item mappings (all accepted shapes)."""
    if isinstance(doc, list):
        yield doc
        return
    if not isinstance(doc, dict):
        return
    if isinstance(doc.get("items"), list):
        yield doc["items"]
    ops = doc.get("ops")
    if isinstance(ops, list):
        for op in ops:
            if isinstance(op, dict):
                item = op.get("item")
                if isinstance(item, dict):
                    yield [item]
                elif isinstance(item, list):
                    yield item


def _iter_items(doc: Any) -> Iterator[dict]:
    for items in _item_lists(doc):
        for item in items:
            if isinstance(item, dict):
                yield item


def _qualify(ident: str, project: str | None, where: str) -> str:
    if ":" in ident:
        return ident
    if not project:
        raise CardError([f"{where}: bare id {ident!r} has no project"], ident)
    return f"{project}:{ident}"


def cards_from_batch(doc: Any, default_project: str | None = None) -> dict[str, Card]:
    """Collect the cards of a loaded batch document, keyed ``project:id``.

    Accepts a top-level ``cards:`` mapping (keys ``project:id`` or bare ``id``, the bare
    form using ``default_project`` or the batch ``project``) and inline ``card:``
    mappings on items (found under ``ops[].item``, ``items:`` or a bare item list).
    A key carrying two cards raises :class:`CardError`.
    """
    if not isinstance(doc, (dict, list)):
        raise CardError(["batch document must be a mapping or a list of items"])
    project = default_project
    if project is None and isinstance(doc, dict) and isinstance(doc.get("project"), str):
        project = doc["project"] or None
    cards: dict[str, Card] = {}

    def add(key: str, raw: Any) -> None:
        if key in cards:
            raise CardError(["duplicate card"], key)
        cards[key] = parse_card(raw, key)

    block = doc.get("cards") if isinstance(doc, dict) else None
    if block is not None:
        if not isinstance(block, dict):
            raise CardError(["cards must be a mapping of key to card"])
        for ident, raw in block.items():
            if not isinstance(ident, str) or not ident.strip():
                raise CardError([f"card key {ident!r} must be a non-empty string"])
            add(_qualify(ident.strip(), project, "cards"), raw)
    for item in _iter_items(doc):
        if "card" not in item:
            continue
        key = item.get("key")
        if not isinstance(key, str) or not key:
            ident, proj = item.get("id"), item.get("project", project)
            if not isinstance(ident, str) or not ident:
                raise CardError(["item with an inline card needs a key or an id"])
            key = _qualify(ident, proj if isinstance(proj, str) else None, "item")
        add(key, item["card"])
    return cards


def strip_cards(doc: Any) -> Any:
    """Deep copy of ``doc`` without inline ``card:`` keys and the top-level ``cards:``.

    The input is not mutated; the result is what the keeper importer accepts.
    """
    out = copy.deepcopy(doc)
    if isinstance(out, dict):
        out.pop("cards", None)
    for item in _iter_items(out):
        item.pop("card", None)
    return out


def load_cards_yaml(path: str | Path) -> dict[str, Card]:
    """Load a batch YAML file and return its cards (see :func:`cards_from_batch`)."""
    import yaml

    with open(path, encoding="utf-8") as handle:
        doc = yaml.safe_load(handle)
    return cards_from_batch(doc)


def apply_cards(store: Any, principal: Any, cards: dict[str, Card]) -> list[str]:
    """``store.put_card`` every card in sorted key order; return the keys written."""
    written: list[str] = []
    for key in sorted(cards):
        store.put_card(principal, key, cards[key].to_store())
        written.append(key)
    return written
