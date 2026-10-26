"""DECISIONS.md <-> keeper rule items (PG-EFFICIENCY-GATES slice 2, operator D-2746).

The keeper holds the rules (``keeper.store_rules``); ``DECISIONS.md`` is a view.

* :func:`parse` splits the file into a preamble, numbered sections (``## <n>. ...``)
  and rule lines (``<n>.<m>[a-z] ...``).  A section is kept as a template: its
  own text with one ``{{<rule number>}}`` line per rule.
* :func:`to_rules` turns that into ``rule_set`` objects: ``PREAMBLE``,
  ``SECTION-<n>`` and ``RULE-<n>-<m>[A]`` items, each rule with its tag, D-ids
  and ``depends_on`` edges (rule numbers cited in its text, plus the §0.2
  pipeline 0.2a -> 0.2b -> 0.2c -> 0.2d -> 0.2e).
* :func:`render` rebuilds the file from keeper items (snapshot rows).  Retired
  rules drop out; a rule with no placeholder is appended to its section.
  ``render(rule items of parse(text)) == text`` byte for byte (invariant I7).
* :func:`tree` walks the rule tree from a rule to everything it depends on.
"""
from __future__ import annotations

import re

__all__ = ["parse", "to_rules", "render", "tree", "rule_id", "rule_number", "PIPELINE"]

_SECTION = re.compile(r"^## (\d+)\. ")
_RULE = re.compile(r"^(\d+)\.(\d+)([a-z]?) ")
_PLACEHOLDER = re.compile(r"^\{\{(\d+\.\d+[a-z]?)\}\}$")
_TAG = re.compile(r"`\[([^\]]+)\]`\s*$")
_DID = re.compile(r"\bD-\d+\b")
_TITLE = re.compile(r"\*\*(.+?)\*\*")
_REF = re.compile(r"(?<![\w.-])(\d+\.\d+[a-z]?)(?!\w|\.\d|-\w)")
#: A version number, not a rule: ``Opus 5.5``, ``qwen 3.8``, ``v2.6``.
_VERSIONED = re.compile(r"(?:opus|haiku|sonnet|fable|claude|gpt|qwen|gemini|glm|mimo|kimi|python|v)\W*$", re.I)
#: §0.2: plan gate -> slice -> first review -> fixer -> round cap (operator D-2746 example).
PIPELINE = ("0.2a", "0.2b", "0.2c", "0.2d", "0.2e")


def rule_id(number):
    """``0.2a`` -> ``RULE-0-2A``."""
    major, minor, letter = re.fullmatch(r"(\d+)\.(\d+)([a-z]?)", number).groups()
    return f"RULE-{major}-{minor}{letter.upper()}"


def rule_number(rid):
    """``RULE-0-2A`` -> ``0.2a``; None for a non-rule id."""
    m = re.fullmatch(r"RULE-(\d+)-(\d+)([A-Z]?)", rid or "")
    return f"{m.group(1)}.{m.group(2)}{m.group(3).lower()}" if m else None


def parse(text):
    """``{"preamble", "sections": [{n, template}], "rules": [{number, line, section}]}``."""
    lines = text.split("\n")
    first = next((i for i, line in enumerate(lines) if _SECTION.match(line)), None)
    if not first:
        raise ValueError("DECISIONS.md needs a preamble and at least one '## <n>. ' section")
    sections, rules, current = [], [], None
    for line in lines[first:]:
        head = _SECTION.match(line)
        if head:
            current = {"n": int(head.group(1)), "lines": []}
            sections.append(current)
        m = _RULE.match(line)
        if m and not head:
            number = line.split(" ", 1)[0]
            if int(m.group(1)) != current["n"]:
                raise ValueError(f"rule {number} sits in section {current['n']}")
            rules.append({"number": number, "line": line, "section": current["n"]})
            line = "{{" + number + "}}"
        current["lines"].append(line)
    numbers = [r["number"] for r in rules]
    if len(set(numbers)) != len(numbers):
        raise ValueError("a rule number appears twice")
    if len({s["n"] for s in sections}) != len(sections):
        raise ValueError("a section number appears twice")
    return {"preamble": "\n".join(lines[:first]),
            "sections": [{"n": s["n"], "template": "\n".join(s["lines"])} for s in sections],
            "rules": rules}


def _refs(line, own, known):
    body = line[len(own):]
    out = []
    for m in _REF.finditer(body):
        number = m.group(1)
        if number == own or number not in known or _VERSIONED.search(body[:m.start()]):
            continue
        if number not in out:
            out.append(number)
    return out


def to_rules(parsed):
    """``rule_set`` objects for every block of *parsed* (all in force)."""
    known = {r["number"] for r in parsed["rules"]}
    out = [{"id": "PREAMBLE", "title": "DECISIONS.md preamble",
            "description": parsed["preamble"], "note": "order=0"}]
    for section in parsed["sections"]:
        out.append({"id": f"SECTION-{section['n']}", "title": f"DECISIONS.md section {section['n']}",
                    "description": section["template"], "note": f"order={section['n'] + 1}"})
    for rule in parsed["rules"]:
        number, line = rule["number"], rule["line"]
        deps = _refs(line, number, known)
        if number in PIPELINE and PIPELINE.index(number) > 0:
            prev = PIPELINE[PIPELINE.index(number) - 1]
            if prev in known and prev not in deps:
                deps.insert(0, prev)
        tag = _TAG.search(line)
        title = _TITLE.search(line)
        d_ids = list(dict.fromkeys(_DID.findall(line)))
        note = f"tag={tag.group(1) if tag else '-'}; d={','.join(d_ids) or '-'}"
        out.append({"id": rule_id(number),
                    "title": f"{number} {(title.group(1) if title else '').strip(' .:')}"[:200],
                    "description": line, "source": f"DECISIONS.md section {rule['section']}",
                    "note": note[:500], "depends_on": [rule_id(d) for d in deps]})
    return out


def _order(item):
    m = re.search(r"\border=(\d+)", item.get("note") or "")
    return int(m.group(1)) if m else 10 ** 6


def render(items):
    """DECISIONS.md text from keeper rule items (snapshot rows or ``to_rules`` objects).

    Only items in force count: a snapshot row's ``state`` must be ``done``; a
    ``to_rules`` object (no ``state``) counts as in force.
    """
    live = [i for i in items if i.get("state", "done") == "done"]
    by_id = {i["id"]: i for i in live}
    if "PREAMBLE" not in by_id:
        raise ValueError("no PREAMBLE item in force")
    rules = {rule_number(i["id"]): i["description"] for i in live if rule_number(i["id"])}
    placed = set()
    blocks = [by_id["PREAMBLE"]["description"]]
    sections = sorted((i for i in live if i["id"].startswith("SECTION-")), key=_order)
    for section in sections:
        n = int(section["id"].split("-", 1)[1])
        lines = []
        for line in section["description"].split("\n"):
            m = _PLACEHOLDER.match(line)
            if not m:
                lines.append(line)
            elif m.group(1) in rules:
                lines.append(rules[m.group(1)])
                placed.add(m.group(1))
        extra = sorted((num for num in rules if num not in placed and int(num.split(".")[0]) == n),
                       key=lambda num: [int(x) if x.isdigit() else x
                                        for x in re.findall(r"\d+|[a-z]", num)])
        if extra:
            end = len(lines)
            while end and not lines[end - 1].strip():
                end -= 1
            lines[end:end] = [rules[num] for num in extra]
            placed.update(extra)
        blocks.append("\n".join(lines))
    orphans = sorted(set(rules) - placed)
    if orphans:
        raise ValueError(f"rules without a section: {orphans}")
    return "\n".join(blocks)


def tree(items, start):
    """Rule ids reachable from *start* over ``depends_on`` (depth first, start first)."""
    deps = {i["id"]: [d.split(":", 1)[-1] for d in i.get("depends_on") or []] for i in items}
    seen, order = set(), []

    def walk(rid):
        if rid in seen:
            return
        seen.add(rid)
        order.append(rid)
        for dep in deps.get(rid, []):
            walk(dep)

    walk(start)
    return order
