#!/usr/bin/env python3
"""Utility to check and list skill rule definitions.

Usage:
  skill-rules.py check [FILE...]
  skill-rules.py list [--topic TOPIC] [FILE...]
  skill-rules.py lint [DIR...]
  skill-rules.py all

A rule starts on one line: `R-<topic>-<nn>: <imperative>. (why: <=12 words; source: <test
or measurement>)`; a rule that grew past 200 characters wraps onto continuation lines
indented two spaces and the checks run on the joined rule. check exits 1 on a duplicate id,
a missing or empty source, a why over 12 words, a physical line over 200 characters, a
near-duplicate imperative (difflib ratio >= 0.85), a missing file or a file without rules.
lint exits 1 when any .md in a skill exceeds 500 physical lines, or a SKILL.md lacks
frontmatter with a non-empty name matching its directory and a non-empty description.
all runs lint, then check on every skills/*/cards/*.md.
"""
import argparse
import sys
import re
import difflib
from pathlib import Path

RULE_REGEX = re.compile(r"^(?:-\s+)?(R-[a-z]+-\d{2}):\s+(.+)$")
TAIL_REGEX = re.compile(r"\s*\(why:\s*(.+)\s*;\s*source:\s*([^;]*?)\s*\)\s*$")
MALFORMED_REGEX = re.compile(r"^(?:-\s+)?(R-\S*?):")

CONTINUATION_INDENT = "  "

MAX_MD_LINES = 500

def iter_rule_blocks(lines):
    """Yield (first line number, physical lines) for every rule in `lines`."""
    block = None
    for lineno, line in enumerate(lines, start=1):
        if block and line.startswith(CONTINUATION_INDENT) and line.strip():
            block[1].append(line)
            continue
        if block:
            yield block
            block = None
        if RULE_REGEX.match(line):
            block = (lineno, [line])
    if block:
        yield block

def rule_text(lines):
    """The logical rule: its physical lines joined into one line."""
    return " ".join(part.strip() for part in lines)

def parse_rule(line):
    m = RULE_REGEX.match(line)
    if not m:
        return None
    rule_id, rest = m.group(1), m.group(2)
    tail_match = TAIL_REGEX.search(rest)
    if not tail_match:
        return (rule_id, rest, None, None)
    why, source = tail_match.group(1), tail_match.group(2)
    imp = rest[: tail_match.start()].rstrip()
    return (rule_id, imp, why, source)

def check_files(files):
    total_rules = 0
    problems = []
    id_locations = {}
    rule_entries = []  # (file, lineno, id, imp)

    for f in files:
        path = Path(f)
        if not path.is_file():
            problems.append((f, 0, "", "file not found"))
            continue
        lines = path.read_text(encoding="utf-8").splitlines()
        file_rule_count = 0
        consumed = set()
        for lineno, parts in iter_rule_blocks(lines):
            consumed.update(range(lineno, lineno + len(parts)))
            rule_id, imp, why, source = parse_rule(rule_text(parts))
            file_rule_count += 1
            total_rules += 1
            rule_entries.append((f, lineno, rule_id, imp))
            if rule_id in id_locations:
                problems.append((f, lineno, rule_id, "duplicate id"))
            else:
                id_locations[rule_id] = (f, lineno)
            if why is None or source is None:
                problems.append((f, lineno, rule_id, "missing tail"))
                continue
            if not imp.strip():
                problems.append((f, lineno, rule_id, "empty imperative"))
            if source.strip() == "":
                problems.append((f, lineno, rule_id, "empty source"))
            if len(why.split()) > 12:
                problems.append((f, lineno, rule_id, "why too long"))
            for offset, part in enumerate(parts):
                stripped = part.lstrip('- ').strip('\n')
                if len(stripped) > 200:
                    problems.append((f, lineno + offset, rule_id, "line too long"))
        for lineno, line in enumerate(lines, start=1):
            if lineno in consumed or parse_rule(line):
                continue
            bad = MALFORMED_REGEX.match(line)
            if bad:
                problems.append((f, lineno, bad.group(1), "malformed id (want R-<topic>-<nn>)"))
        if file_rule_count == 0:
            problems.append((f, 0, "", "no rules found"))
    for i in range(len(rule_entries)):
        f1, l1, id1, imp1 = rule_entries[i]
        for j in range(i + 1, len(rule_entries)):
            f2, l2, id2, imp2 = rule_entries[j]
            seq = difflib.SequenceMatcher(None, imp1.lower(), imp2.lower())
            if seq.ratio() >= 0.85:
                problems.append((f1, l1, id1, f"near-duplicate with {id2}"))
    return total_rules, problems

def list_rules(files, topic=None):
    out_lines = []
    for f in files:
        path = Path(f)
        if not path.is_file():
            continue
        lines = path.read_text(encoding="utf-8").splitlines()
        for _lineno, parts in iter_rule_blocks(lines):
            text = rule_text(parts)
            parsed = parse_rule(text)
            if not parsed:
                continue
            rule_id, imp, why, source = parsed
            if topic:
                m = re.match(r"R-([a-z]+)-\d{2}", rule_id)
                if not m or m.group(1) != topic:
                    continue
            out_lines.append(text)
    return out_lines

def repo_root():
    return Path(__file__).resolve().parent.parent

def default_skill_dirs():
    return sorted(p for p in (repo_root() / "skills").glob("*/") if p.is_dir())

def default_cards():
    return sorted((repo_root() / "skills").glob("*/cards/*.md"))

def parse_frontmatter(text):
    """Return the frontmatter mapping, or None when there is no `---` block."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    for end, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            meta = {}
            for entry in lines[1:end]:
                if ":" in entry:
                    key, value = entry.split(":", 1)
                    meta[key.strip()] = value.strip()
            return meta
    return None

def lint_dirs(dirs):
    """Return (path, lineno, rule id, problem) for every lint failure."""
    problems = []
    for d in dirs:
        path = Path(d)
        if not path.is_dir():
            problems.append((str(path), 0, "", "not a directory"))
            continue
        for md in sorted(path.rglob("*.md")):
            count = len(md.read_text(encoding="utf-8").splitlines())
            if count > MAX_MD_LINES:
                problems.append((str(md), 0, "", f"{count} lines (max {MAX_MD_LINES})"))
        skill = path / "SKILL.md"
        if not skill.is_file():
            problems.append((str(skill), 0, "", "missing SKILL.md"))
            continue
        meta = parse_frontmatter(skill.read_text(encoding="utf-8"))
        if meta is None:
            problems.append((str(skill), 0, "", "missing frontmatter"))
            continue
        name = meta.get("name", "")
        if not name:
            problems.append((str(skill), 0, "", "missing name"))
        elif name != path.name:
            problems.append((str(skill), 0, "", f"name {name!r} != directory {path.name!r}"))
        if not meta.get("description", ""):
            problems.append((str(skill), 0, "", "missing description"))
    return problems

def print_problems(problems):
    for f, lineno, rid, prob in problems:
        location = f
        if lineno:
            location += f":{lineno}"
        print(f"{location}: {rid}: {prob}")

def main():
    parser = argparse.ArgumentParser(prog="skill-rules")
    subparsers = parser.add_subparsers(dest="cmd", required=True)

    check_parser = subparsers.add_parser("check", help="Validate rule files")
    check_parser.add_argument("files", nargs="*", default=["skills/unattended-orchestration/SKILL.md"])

    list_parser = subparsers.add_parser("list", help="List rule lines")
    list_parser.add_argument("--topic", help="Filter by rule topic")
    list_parser.add_argument("files", nargs="*", default=["skills/unattended-orchestration/SKILL.md"])

    lint_parser = subparsers.add_parser("lint", help="Validate skill markdown and frontmatter")
    lint_parser.add_argument("dirs", nargs="*")

    subparsers.add_parser("all", help="Run lint, then check every card")

    args = parser.parse_args()
    if args.cmd == "check":
        total, problems = check_files(args.files)
        if problems:
            print_problems(problems)
            sys.exit(1)
        else:
            print(f"ok: {total} rules")
            sys.exit(0)
    elif args.cmd == "lint":
        dirs = args.dirs or [str(p) for p in default_skill_dirs()]
        problems = lint_dirs(dirs)
        if problems:
            print_problems(problems)
            sys.exit(1)
        else:
            print(f"ok: {len(dirs)} skills")
            sys.exit(0)
    elif args.cmd == "all":
        lint_problems = lint_dirs([str(p) for p in default_skill_dirs()])
        total, check_problems = check_files([str(p) for p in default_cards()])
        problems = lint_problems + check_problems
        if problems:
            print_problems(problems)
            sys.exit(1)
        else:
            print(f"ok: lint passed; {total} rules")
            sys.exit(0)
    else:  # list
        lines = list_rules(args.files, args.topic)
        for l in lines:
            print(l)

if __name__ == "__main__":
    main()
