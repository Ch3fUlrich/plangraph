#!/usr/bin/env python3
"""Scan a tree for leaks before it goes public (M2c split).

Rules: hostname, ipv4, ipv6, email, token-shape, high-entropy, credential-shape,
home-path, private-word, symlink.  Output is ``path:line: rule`` only -- the
matched value is never printed, stored or returned.  Exit 0 clean, 1 on any hit,
2 on a usage/config error.

    scrub_check.py TREE [--rules rules.toml] [--words private_words.txt]

``rules.toml`` (merged over the built-in defaults, lists are extended)::

    [scrub]
    hostname_patterns = ["build[0-9]+\\.example-lab"]   # regexes
    allow_email_domains = ["example.org"]
    allow_home_names = ["alice"]
    [[scrub.allow]]                                       # skip rules for paths
    path = "vendor/**"
    rules = ["ipv4", "high-entropy"]

The private word list (one word per line, ``#`` comments, ``re:`` prefix for a
regex) is kept private by the caller; only its path is passed in.
"""
from __future__ import annotations

import argparse
import math
import re
import sys
import tomllib
from collections import namedtuple
from pathlib import Path

Hit = namedtuple("Hit", "path line rule")

RULES = ("hostname", "ipv4", "ipv6", "email", "token-shape", "high-entropy",
         "credential-shape", "home-path", "private-word", "symlink")

_HOST_SUFFIX = r"(?:internal|lan|corp|intranet|home\.arpa|ts\.net|tailnet|localdomain)"
_HOST = re.compile(r"(?<![\w.-])[a-z0-9][a-z0-9-]*(?:\.[a-z0-9-]+)*\." + _HOST_SUFFIX
                   + r"(?![\w-])", re.IGNORECASE)
_IPV4 = re.compile(r"(?<![\d.])(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})(?!\d|\.\d)")
_IPV6 = re.compile(r"(?<![\w:])(?:[0-9a-f]{1,4}:){7}[0-9a-f]{1,4}(?![\w:])", re.IGNORECASE)
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@((?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,})")
_HOME = re.compile(r"(?:/home|/Users)/([A-Za-z0-9._-]+)|[A-Za-z]:\\Users\\([^\\\s\"']+)")
_ENTROPY_CAND = re.compile(r"[A-Za-z0-9_+/=-]{28,}")
_HEX = re.compile(r"[0-9a-fA-F]+")
_TOKENS = [re.compile(p) for p in (
    r"gh[pousr]_[A-Za-z0-9]{30,}", r"github_pat_[A-Za-z0-9_]{30,}",
    r"glpat-[A-Za-z0-9_-]{16,}", r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{20,}",
    r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b", r"\bxox[abprs]-[A-Za-z0-9-]{10,}",
    r"\bAIza[0-9A-Za-z_-]{30,}", r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}",
    r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]{24,}",
    r"(?i)\b(?:token|secret|api[_-]?key|passw(?:or)?d)\b[\"']?\s*[:=]\s*[\"'][A-Za-z0-9._~+/=-]{20,}[\"']",
)]

DEFAULTS = {
    "hostname_patterns": [],
    "allow_email_domains": ["example.com", "example.org", "example.net"],
    "allow_email_suffixes": [".invalid", ".test", ".example"],
    "allow_home_names": ["user", "you", "name", "username", "me", "runner",
                         "example", "alice", "bob", "foo", "x"],
    "allow_ipv4_prefixes": ["127.", "0.0.0.0", "255.255.255.255",
                            "192.0.2.", "198.51.100.", "203.0.113."],
    "entropy_min": 4.0,
}


def _shape_finder():
    """credential_guard.find_shapes (single source of truth) or a local copy."""
    src = Path(__file__).resolve().parents[2] / "src"
    if src.is_dir() and str(src) not in sys.path:
        sys.path.insert(0, str(src))
    try:
        from plangraph.keeper.credential_guard import find_shapes
        return find_shapes
    except Exception:  # standalone use outside a checkout
        shape, word = re.compile(r"\w+/\w{6,}"), re.compile(
            r"credential|password|passwd|pwd|login", re.IGNORECASE)

        def find_shapes(text):
            words = [m.span() for m in word.finditer(text)]
            return [m for m in shape.finditer(text)
                    if any(a <= m.end() + 120 and b >= m.start() - 120 for a, b in words)]
        return find_shapes


def entropy(s):
    return -sum(s.count(c) / len(s) * math.log2(s.count(c) / len(s)) for c in set(s))


def glob_re(pattern):
    """``**`` spans directories, ``*``/``?`` do not; a slash-less pattern matches any name."""
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?"); i += 3
        elif pattern.startswith("**", i):
            out.append(".*"); i += 2
        elif pattern[i] == "*":
            out.append("[^/]*"); i += 1
        elif pattern[i] == "?":
            out.append("[^/]"); i += 1
        else:
            out.append(re.escape(pattern[i])); i += 1
    body = "".join(out)
    return re.compile(body + r"\Z" if "/" in pattern else r"(?:.*/)?" + body + r"\Z")


def _load_words(path):
    pats = []
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        w = raw.strip()
        if not w or w.startswith("#"):
            continue
        if w.startswith("re:"):
            pats.append(re.compile(w[3:], re.IGNORECASE))
        else:
            pats.append(re.compile(r"(?<![A-Za-z0-9])" + re.escape(w) + r"(?![A-Za-z0-9])",
                                   re.IGNORECASE))
    return pats


def build_config(words_file=None, rules_files=(), allow=None, **over):
    """Merge DEFAULTS, rules files, *allow* entries and keyword overrides."""
    cfg = {k: list(v) if isinstance(v, list) else v for k, v in DEFAULTS.items()}
    allow_entries = []
    for rf in rules_files:
        with open(rf, "rb") as fh:
            sect = tomllib.load(fh).get("scrub", {})
        allow_entries += sect.pop("allow", [])
        for k, v in sect.items():
            if k not in cfg:
                raise ValueError(f"unknown [scrub] key {k!r} in {rf}")
            cfg[k] = cfg[k] + list(v) if isinstance(v, list) else v
    allow_entries += list(allow or [])
    cfg.update(over)
    for e in allow_entries:
        bad = set(e.get("rules", [])) - set(RULES)
        if bad or "path" not in e:
            raise ValueError(f"bad allow entry (rules {sorted(bad)}): needs path + known rules")
    cfg["_allow"] = [(glob_re(e["path"]), set(e["rules"])) for e in allow_entries]
    cfg["_host_re"] = [re.compile(p, re.IGNORECASE) for p in cfg["hostname_patterns"]]
    cfg["_words"] = _load_words(words_file) if words_file else []
    return cfg


def _ip_ok(m, cfg):
    octets = [int(g) for g in m.groups()]
    text = ".".join(m.groups())
    return any(o > 255 for o in octets) or any(text.startswith(p) for p in cfg["allow_ipv4_prefixes"])


def _email_ok(m, cfg):
    d = m.group(1).lower()
    return d in cfg["allow_email_domains"] or any(d.endswith(s) for s in cfg["allow_email_suffixes"])


def _entropy_hit(line, cfg):
    for m in _ENTROPY_CAND.finditer(line):
        t = m.group()
        if _HEX.fullmatch(t) or not (re.search(r"[a-z]", t) and re.search(r"[A-Z]", t)
                                     and re.search(r"\d", t)):
            continue
        if entropy(t) >= cfg["entropy_min"]:
            return True
    return False


def scan_line(line, cfg, find_shapes):
    """The rules that fire on one line (a set; values are not retained)."""
    found = set()
    if any(p.search(line) for p in cfg["_host_re"]) or _HOST.search(line):
        found.add("hostname")
    if any(not _ip_ok(m, cfg) for m in _IPV4.finditer(line)):
        found.add("ipv4")
    if _IPV6.search(line):
        found.add("ipv6")
    if any(not _email_ok(m, cfg) for m in _EMAIL.finditer(line)):
        found.add("email")
    if any(p.search(line) for p in _TOKENS):
        found.add("token-shape")
    if _entropy_hit(line, cfg):
        found.add("high-entropy")
    if find_shapes(line):
        found.add("credential-shape")
    if any((m.group(1) or m.group(2)).lower() not in cfg["allow_home_names"]
           for m in _HOME.finditer(line)):
        found.add("home-path")
    if any(p.search(line) for p in cfg["_words"]):
        found.add("private-word")
    return found


def _allowed(rel, cfg):
    skip = set()
    for rx, rules in cfg["_allow"]:
        if rx.match(rel):
            skip |= rules
    return skip


def scan_tree(root, cfg, with_skipped=False):
    """Hits for every file under *root* (``.git`` skipped, symlinks never followed)."""
    root = Path(root)
    find_shapes = _shape_finder()
    hits, skipped = [], []
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root).as_posix()
        if ".git" in p.relative_to(root).parts:
            continue
        skip = _allowed(rel, cfg)
        if any(w.search(rel) for w in cfg["_words"]) and "private-word" not in skip:
            hits.append(Hit(rel, 0, "private-word"))
        if p.is_symlink():
            if "symlink" not in skip:
                hits.append(Hit(rel, 0, "symlink"))
            continue
        if not p.is_file():
            continue
        data = p.read_bytes()
        if b"\x00" in data[:8192]:
            skipped.append(rel)
            continue
        for n, line in enumerate(data.decode("utf-8", "replace").splitlines(), 1):
            for rule in sorted(scan_line(line, cfg, find_shapes) - skip):
                hits.append(Hit(rel, n, rule))
    return (hits, skipped) if with_skipped else hits


def report(hits, out=None, skipped=()):
    out = out or sys.stdout
    for h in hits:
        print(f"{h.path}:{h.line}: {h.rule}", file=out)
    for s in skipped:
        print(f"{s}: binary file not scanned", file=out)
    files = len({h.path for h in hits})
    print(f"scrub_check: {len(hits)} hit(s) in {files} file(s)", file=out)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("tree")
    ap.add_argument("--rules", action="append", default=[], help="rules.toml (repeatable)")
    ap.add_argument("--words", help="private word list file (stays private)")
    args = ap.parse_args(argv)
    if not Path(args.tree).is_dir():
        print(f"scrub_check: not a directory: {args.tree}", file=sys.stderr)
        return 2
    try:
        cfg = build_config(words_file=args.words, rules_files=args.rules)
    except (OSError, ValueError, re.error, tomllib.TOMLDecodeError) as exc:
        print(f"scrub_check: bad config: {type(exc).__name__}", file=sys.stderr)
        return 2
    hits, skipped = scan_tree(args.tree, cfg, with_skipped=True)
    report(hits, skipped=skipped)
    return 1 if hits else 0


if __name__ == "__main__":
    raise SystemExit(main())
