#!/usr/bin/env python3
"""Turn a rules markdown file into a keeper batch of decision items.

The source is a rules document: ``## N. Title`` section headings and rule lines
that start at column 0 with an optional ``**``, a number such as ``3.2`` or
``0.2a`` and text.  The bold span may start BEFORE the number (``**0.2 TEXT**
(note).``) or after it (``0.1 **TITLE.** text``); in both cases the title is the
bold phrase without the number.  With no bold phrase the title is the first
sentence (max 120 chars); a rule with no title at all falls back to the first
60 chars of its text and is never an error.  A rule's text may continue on the
following non-blank lines until the next rule line, heading or blank line.
Group rules (``0.2`` followed by ``0.2a``..``0.2h``) are separate rules.  Tags
appear as backtick groups (``[keep]``, ``[gate: path/verb]``, ``[gate-todo
GT3]``, ``[temporary]``; a tag may name a second class after ``;``); ``D-<digits>``
ids are collected; references (the words rule, rules, DECISIONS, see, per or the
section sign followed by numbers, also lists such as ``3.2/3.3`` or ``3.2 and
3.4``) become ``depends_on`` edges.

The batch YAML satisfies tools/apply_batch.py: top-level keys only ``batch``,
``principal``, ``source``, ``source_sha256``, ``ops``; ``source`` is the source
rules file's path relative to the --out file's directory (os.path.relpath) and
``source_sha256`` is the sha256 hex of the source file bytes; each op is
``{op: create, item: [ {...} ]}``.  Reference cycles are broken by dropping the
edge that closes each cycle (reported); unknown references are reported, never
edged.  Exits 1 only on duplicate item ids.  ``--report`` prints parse counts
including the rule-line count per section.
"""
import argparse
import hashlib
import os
import re
import sys

import yaml

RULE_RE = re.compile(r"^(\*{0,2})(\d+\.\d+[A-Za-z]?)(?:[ \t]+(.*))?$")
HEADING_RE = re.compile(r"^#{1,6}[ \t]")
BOLD_RE = re.compile(r"\*\*([^*]+?)\*\*")
SENTENCE_RE = re.compile(r"(.*?[.!?])(?:\s|$)")
LEAD_NUM_RE = re.compile(r"^\d+\.\d+[A-Za-z]?\s*")
TAG_RE = re.compile(r"`\[([^\]]*)\]`")
DID_RE = re.compile(r"D-\d+")
NUM = r"\d+\.\d+[A-Za-z]?"
NUM_RE = re.compile(NUM)
REF_KEYWORD_RE = re.compile(
    r"(?:\brules?\b|\bdecisions\b|\bsee\b|\bper\b|\u00a7)[ \t]*", re.IGNORECASE)
REF_LIST_RE = re.compile(
    NUM + r"(?:[ \t]*(?:[/,;&+]|\band\b|\bor\b)[ \t]*" + NUM + r")*")


class BuildError(Exception):
    """A source problem that stops the batch from being written."""


def find_refs(text):
    """Rule numbers named after rule/rules/DECISIONS/see/per/section sign."""
    refs = []
    for keyword in REF_KEYWORD_RE.finditer(text):
        match = REF_LIST_RE.match(text, keyword.end())
        if match:
            refs.extend(NUM_RE.findall(match.group(0)))
    return refs


def title_of(body):
    """Bold phrase without the number; else first sentence (<= 120 chars);
    a rule with no title at all gets the first 60 chars of its text."""
    bold = BOLD_RE.search(body)
    if bold:
        phrase = LEAD_NUM_RE.sub("", bold.group(1), count=1).strip()
        if phrase:
            return phrase
    sentence = SENTENCE_RE.match(body)
    if sentence and sentence.group(1).strip():
        return sentence.group(1).strip()[:120]
    return body.strip()[:60]


def parse_rules(text):
    """Split the source into rule dicts (number, text, title, tags, dids, refs)."""
    rules = []
    current = None
    section = "(none)"
    for raw in text.splitlines():
        if HEADING_RE.match(raw):
            section = raw.lstrip("#").strip()
            current = None
            continue
        if not raw.strip():
            current = None
            continue
        match = RULE_RE.match(raw)
        if match:
            stars, number, rest = match.group(1), match.group(2), match.group(3) or ""
            if stars and "**" in rest:
                rest = stars + rest  # keep the bold span that wraps the number
            current = {"number": number, "parts": [rest], "section": section}
            rules.append(current)
        elif current is not None:
            current["parts"].append(raw)
    for rule in rules:
        body = " ".join(" ".join(rule.pop("parts")).split())
        rule["text"] = body
        rule["title"] = title_of(body)
        rule["tags"] = [part.strip() for group in TAG_RE.findall(body)
                        for part in group.split(";") if part.strip()]
        rule["dids"] = sorted(set(DID_RE.findall(body)), key=lambda d: int(d[2:]))
        rule["refs"] = find_refs(body)
    return rules


def tag_class(tag):
    """First word of a tag: keep, temporary, gate or gate-todo."""
    head = tag.split(":", 1)[0].split()
    return head[0].lower() if head else ""


def priority_of(tags):
    """Gate-tagged rules rank 5, keep 7, temporary 9 (gate > keep > temporary)."""
    classes = {tag_class(tag) for tag in tags}
    if classes & {"gate", "gate-todo"}:
        return 5
    if "keep" in classes:
        return 7
    if "temporary" in classes:
        return 9
    return 7


def item_id(prefix, number):
    """RULE-3-2 / RULE-0-2A: dots become dashes, the letter is uppercased."""
    return f"{prefix}-{number.replace('.', '-').upper()}"


def drop_cycle_edges(adj, order):
    """DFS in order; report every edge that closes a cycle (a back edge)."""
    dropped = []
    state = {}
    for start in order:
        if state.get(start):
            continue
        state[start] = 1
        stack = [(start, iter(adj[start]))]
        while stack:
            node, steps = stack[-1]
            for nxt in steps:
                if state.get(nxt) == 1:
                    dropped.append((node, nxt))
                elif not state.get(nxt):
                    state[nxt] = 1
                    stack.append((nxt, iter(adj[nxt])))
                    break
            else:
                state[node] = 2
                stack.pop()
    return dropped


def verify_text_dag(rules, prefix):
    """Assert each description 'Depends on:' line equals its depends_on list."""
    number_of = {rule["id"]: rule["number"].lower() for rule in rules}
    id_re = re.compile(re.escape(prefix) + r"-\d+-\d+[A-Z]?")
    for rule in rules:
        tail = rule["description"].rsplit("Depends on: ", 1)[1]
        text_side = {number_of.get(tok, tok) for tok in id_re.findall(tail)}
        dag_side = {number_of[key.split(":", 1)[1]] for key in rule["depends_on"]}
        assert text_side == dag_side, (
            f"rule {rule['number']}: Depends on line {sorted(text_side)} "
            f"!= depends_on {sorted(dag_side)}")


def build(rules, opts):
    """Batch mapping, dropped cycle edges, unknown ref pairs. Raises BuildError."""
    seen = {}
    for rule in rules:
        rule["id"] = item_id(opts.prefix, rule["number"])
        if rule["id"] in seen:
            raise BuildError(f"duplicate id {rule['id']} (rules "
                             f"{seen[rule['id']]} and {rule['number']})")
        seen[rule["id"]] = rule["number"]
    by_key = {rule["number"].lower(): rule for rule in rules}
    adj = {rule["id"]: set() for rule in rules}
    unknown = []
    for rule in rules:
        for ref in rule["refs"]:
            if ref.lower() == rule["number"].lower():
                continue  # self-reference is ignored
            target = by_key.get(ref.lower())
            if target is None:
                pair = (rule["number"], ref)
                if pair not in unknown:
                    unknown.append(pair)
            elif target["id"] != rule["id"]:
                adj[rule["id"]].add(target["id"])
    adj = {key: sorted(value) for key, value in adj.items()}
    dropped = []
    for src, tgt in drop_cycle_edges(adj, [rule["id"] for rule in rules]):
        adj[src].remove(tgt)
        dropped.append((seen[src], seen[tgt]))
    for rule in rules:
        deps = adj[rule["id"]]
        rule["depends_on"] = [f"{opts.project}:{tid}" for tid in deps]
        rule["description"] = (
            rule["text"] + "\n"
            + f"Tags: {', '.join(rule['tags'])}; "
            + f"Decisions: {', '.join(rule['dids'])}; "
            + f"Depends on: {' '.join(deps)}")
    verify_text_dag(rules, opts.prefix)
    ops = []
    for rule in rules:
        item = {
            "project": opts.project,
            "id": rule["id"],
            "kind": "decision",
            "owner": "operator",
            "track": opts.track,
            "priority": priority_of(rule["tags"]),
            "review_tier": "none",
            "evidence_kinds": ["decision"],
            "depends_on": rule["depends_on"],
            "title": f"{opts.prefix} {rule['number']}: {rule['title']}".strip()[:120],
            "description": rule["description"],
        }
        ops.append({"op": "create", "item": [item]})
    batch = {
        "batch": opts.batch,
        "principal": opts.principal,
        "source": os.path.relpath(os.path.abspath(opts.source),
                                  os.path.dirname(os.path.abspath(opts.out))),
        "source_sha256": opts.source_sha256,
        "ops": ops,
    }
    return batch, dropped, unknown


def write_report(out, rules, batch, dropped, unknown):
    """Parse counts, rule lines per section, dropped cycles, unknown refs."""
    edges = sum(len(op["item"][0]["depends_on"]) for op in batch["ops"])
    dids = {did for rule in rules for did in rule["dids"]}
    sections = {}
    for rule in rules:
        name = rule.get("section") or "(none)"
        sections[name] = sections.get(name, 0) + 1
    out.write(f"rules {len(rules)}\n")
    out.write(f"items {len(batch['ops'])}\n")
    out.write(f"d-ids {len(dids)}\n")
    out.write(f"edges {edges}\n")
    for name, count in sections.items():
        out.write(f"section {name}: {count} rules\n")
    out.write("dropped cycle edges: "
              + (", ".join(f"{src}->{tgt}" for src, tgt in dropped) or "none")
              + "\n")
    out.write("unknown refs: "
              + (", ".join(f"{src}->{tgt}" for src, tgt in unknown) or "none")
              + "\n")


def main(argv=None, out=None, err=None):
    args_in = list(sys.argv[1:] if argv is None else argv)
    out = out or sys.stdout
    err = err or sys.stderr
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--source", required=True, help="rules markdown file")
    parser.add_argument("--out", required=True, help="batch YAML to write")
    parser.add_argument("--project", default="plangraph")
    parser.add_argument("--track", default="ROUTING")
    parser.add_argument("--prefix", default="RULE")
    parser.add_argument("--batch", default=None)
    parser.add_argument("--principal", default="router")
    parser.add_argument("--report", action="store_true")
    try:
        opts = parser.parse_args(args_in)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2
    if opts.batch is None:
        opts.batch = os.path.splitext(os.path.basename(opts.source))[0] \
            or "decisions"
    try:
        with open(opts.source, "rb") as handle:
            raw = handle.read()
        text = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        err.write(f"error: cannot read source: {exc.__class__.__name__}\n")
        return 1
    opts.source_sha256 = hashlib.sha256(raw).hexdigest()
    rules = parse_rules(text)
    if not rules:
        err.write("error: no rules found in source\n")
        return 1
    try:
        batch, dropped, unknown = build(rules, opts)
    except BuildError as exc:
        err.write(f"error: {exc}\n")
        return 1
    data = yaml.safe_dump(batch, sort_keys=False, default_flow_style=False,
                          allow_unicode=True, width=100)
    try:
        with open(opts.out, "w", encoding="utf-8") as handle:
            handle.write(data)
    except OSError as exc:
        err.write(f"error: cannot write batch: {exc.__class__.__name__}\n")
        return 1
    if opts.report:
        write_report(out, rules, batch, dropped, unknown)
    return 0


if __name__ == "__main__":
    sys.exit(main())
