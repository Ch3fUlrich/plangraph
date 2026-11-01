#!/usr/bin/env python3
"""Render decision items back into the rules markdown they came from.

``skeleton`` copies a rules markdown source, replacing every rule block -
the rule line plus continuation lines up to the last non-blank line before
the next rule line or heading - with one ``{{ID}}`` placeholder line (rule
detection and group headings as in the companion importer module; group
headings and all non-rule lines stay literal).  ``render`` fills the
placeholders from items and writes the rules markdown back out; ``check``
renders and compares with a target, printing a unified diff head of at most
40 lines when they differ.  Items are a JSON list of ``{id, description}``
objects or a batch YAML of ``ops[].item[0]`` entries.  The rendered rule
line is ``<number> <text>``: the number is the item id minus its prefix,
the first dash a dot, any letter lowercased (``RULE-0-2A`` -> ``0.2a``);
the text is the description up to its last ``\\nTags:`` line (whole
description when there is none), whitespace-collapsed to one line.  A
placeholder with no item, or an item with no placeholder, is an error
naming the id (exit 1).  The output ends with exactly the skeleton's
trailing newline state.  argparse errors exit 2; any other problem exits 1.
"""
import argparse
import difflib
import json
import re
import sys

import yaml

from decisions_import import HEADING_RE, RULE_RE, item_id, parse_rules

PLACEHOLDER_RE = re.compile(r"^\{\{([A-Za-z0-9_.:-]+)\}\}$")
NUMBER_RE = re.compile(r"\d+\.\d+[a-z]?")
TAGS_MARK = "\nTags:"
DIFF_LINES = 40


class RenderError(Exception):
    """A skeleton/item mismatch, a malformed id or an unreadable file."""


def make_skeleton(text, prefix="RULE"):
    """Copy `text` with every rule block replaced by one ``{{id}}`` line."""
    groups = set(parse_rules(text).group_headings)
    lines = text.split("\n")
    out, i = [], 0
    while i < len(lines):
        match = RULE_RE.match(lines[i])
        if not match:
            out.append(lines[i])
            i += 1
            continue
        j = i + 1
        while (j < len(lines) and not RULE_RE.match(lines[j])
               and not HEADING_RE.match(lines[j])):
            j += 1
        while j > i + 1 and not lines[j - 1].strip():
            j -= 1
        if match.group(2) in groups:
            out.extend(lines[i:j])   # group headings stay literal
        else:
            out.append("{{%s}}" % item_id(prefix, match.group(2)))
        i = j
    return "\n".join(out)


def number_of(ident):
    """``RULE-0-2A`` -> ``0.2a``: drop the prefix, first dash to dot, lower."""
    head, dash, tail = ident.partition("-")
    number = tail.replace("-", ".", 1).lower() if dash else ""
    if not head or not NUMBER_RE.fullmatch(number):
        raise RenderError("malformed item id %s" % ident)
    return number


def rule_line(ident, description):
    """The one rule line for an item: ``<number> <text>``."""
    text = description.rsplit(TAGS_MARK, 1)[0] if TAGS_MARK in description \
        else description
    text = " ".join(text.split())
    number = number_of(ident)
    return number + (" " + text if text else "")


def load_items(path):
    """Items from a JSON list of ``{id, description}`` or a batch YAML ops list."""
    try:
        with open(path, encoding="utf-8") as handle:
            raw = handle.read()
    except (OSError, UnicodeDecodeError) as exc:
        raise RenderError("cannot read items: %s" % exc.__class__.__name__)
    try:
        data = json.loads(raw)
    except ValueError:
        try:
            data = yaml.safe_load(raw)
        except yaml.YAMLError as exc:
            raise RenderError("cannot parse items: %s" % exc.__class__.__name__)
    if isinstance(data, dict):
        try:
            data = [op["item"][0] for op in data["ops"]]
        except (KeyError, TypeError, IndexError):
            raise RenderError("batch items must be ops[].item[0]")
    if not isinstance(data, list):
        raise RenderError("items must be a JSON list or a batch with ops")
    if any(not isinstance(e, dict) or "id" not in e for e in data):
        raise RenderError("item entry without an id")
    return [{"id": str(e["id"]), "description": str(e.get("description") or "")}
            for e in data]


def render_text(skeleton, items):
    """Fill every ``{{id}}`` from `items`; the id sets must match exactly."""
    by_id = {item["id"]: item["description"] for item in items}
    used, out = set(), []
    for line in skeleton.split("\n"):
        match = PLACEHOLDER_RE.match(line)
        if not match:
            out.append(line)
            continue
        ident = match.group(1)
        if ident not in by_id:
            raise RenderError("placeholder %s has no item" % ident)
        used.add(ident)
        out.append(rule_line(ident, by_id[ident]))
    extra = sorted(set(by_id) - used)
    if extra:
        raise RenderError("item %s has no placeholder" % extra[0])
    return "\n".join(out)


def read_text(path, label):
    try:
        with open(path, "rb") as handle:
            return handle.read().decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise RenderError("cannot read %s: %s" % (label, exc.__class__.__name__))


def write_text(path, text):
    try:
        with open(path, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
    except OSError as exc:
        raise RenderError("cannot write output: %s" % exc.__class__.__name__)


def diff_head(target, rendered, name):
    """Unified diff of the two texts, at most DIFF_LINES lines of it."""
    lines = difflib.unified_diff(target.splitlines(keepends=True),
                                 rendered.splitlines(keepends=True),
                                 fromfile=name, tofile="render")
    return [ln.rstrip("\n") + "\n" for ln in list(lines)[:DIFF_LINES]]


def main(argv=None, out=None, err=None):
    out = out or sys.stdout
    err = err or sys.stderr
    parser = argparse.ArgumentParser(prog="decisions_render",
                                    description=__doc__.split("\n")[0])
    subs = parser.add_subparsers(dest="command", required=True)
    skel = subs.add_parser("skeleton", help="rule blocks -> {{id}} lines")
    skel.add_argument("--source", required=True, help="rules markdown file")
    skel.add_argument("--out", required=True, help="skeleton file to write")
    skel.add_argument("--prefix", default="RULE", help="item id prefix")
    for name in ("render", "check"):
        sub = subs.add_parser(name, help=name + " the skeleton against items")
        sub.add_argument("--skeleton", required=True, help="skeleton file")
        sub.add_argument("--items", required=True, help="JSON list or batch YAML")
        sub.add_argument("--out" if name == "render" else "--target",
                         required=True, help="file to write or verify")
    try:
        args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2
    try:
        if args.command == "skeleton":
            write_text(args.out, make_skeleton(read_text(args.source, "source"),
                                              args.prefix))
            return 0
        rendered = render_text(read_text(args.skeleton, "skeleton"),
                               load_items(args.items))
        if args.command == "render":
            write_text(args.out, rendered)
            return 0
        with open(args.target, "rb") as handle:
            target = handle.read()
        if target == rendered.encode("utf-8"):
            return 0
        for line in diff_head(target.decode("utf-8", "replace"), rendered,
                              args.target):
            err.write(line)
        return 1
    except (RenderError, OSError) as exc:
        err.write("error: %s\n" % exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
