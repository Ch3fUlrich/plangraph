#!/usr/bin/env python3
"""Scan a tree for leaks before it goes public (M2c split).

Rules: hostname, ipv4, ipv6, email, token-shape, high-entropy, credential-shape,
home-path, private-word, symlink.  Output is ``path:line: rule`` only -- the
matched value is never printed, stored or returned.  Exit 0 clean, 1 on any hit,
2 on a usage/config error.

    scrub_check.py TREE [--rules rules.toml] [--words private_words.txt]
                        [--hash-hits]

``rules.toml`` (merged over the built-in defaults, lists are extended)::

    [scrub]
    hostname_patterns = ["build[0-9]+\\.example-lab"]   # regexes
    allow_email_domains = ["example.org"]
    allow_home_names = ["alice"]
    [[scrub.allow]]                                       # skip rules for paths
    path = "vendor/**"
    rules = ["ipv4", "high-entropy"]
    [[scrub.allow_value]]                                 # skip one matched value
    path = "tests/fixtures/token.json"                    # exact path, no globs
    rule = "token-shape"                                  # one known rule id
    value_sha256 = "<sha256 hex of the exact matched text>"
    why = "reviewed synthetic fixture"                    # required; never printed

A value allowance suppresses a hit only when *path*, *rule* and the sha256 of the
matched text all match (matched text = the whole regex match, in tree-relative
line context).  A missing or empty key, a glob in *path*, an unknown rule or a
malformed hash is a ``ValueError`` naming the entry number only -- neither path
nor value is echoed.  ``--hash-hits`` appends `` sha256=<hex>`` to every hit line,
so an allowance can be written without anyone seeing the value.  Symlink hits are
never allow-listable (path-wide or per value) and carry no hash.

The private word list (one word per line, ``#`` comments, ``re:`` prefix for a
regex) is kept private by the caller; only its path is passed in.

A path that itself contains a private word (and every ``private-word`` hit on a
path, and every "binary file not scanned" note) is printed as
``<path-hash:sha256(path)[:12]>`` so the word never reaches the output;
``--resolve HASH`` prints the real relative path on the operator's own terminal.
"""
from __future__ import annotations

import argparse
import hashlib
import math
import re
import sys
import tomllib
from collections import namedtuple
from pathlib import Path

#: ``sha`` is the sha256 of one matched value that is still hitting (empty when the
#: hit has no value to allow-list, e.g. a symlink); the value itself is never kept.
Hit = namedtuple("Hit", "path line rule sha", defaults=("",))

RULES = ("hostname", "ipv4", "ipv6", "email", "token-shape", "high-entropy",
         "credential-shape", "home-path", "private-word", "symlink")

#: keys every [[scrub.allow_value]] entry needs, and the shape of its hash.
_VALUE_KEYS = ("path", "rule", "value_sha256", "why")
_SHA256 = re.compile(r"[0-9a-fA-F]{64}")

_HOST_SUFFIX = r"(?:internal|lan|corp|intranet|home\.arpa|ts\.net|tailnet|localdomain)"
_HOST = re.compile(r"(?<![\w.-])[a-z0-9][a-z0-9-]*(?:\.[a-z0-9-]+)*\." + _HOST_SUFFIX
                   + r"(?![\w-])", re.IGNORECASE)
_IPV4 = re.compile(r"(?<![\d.])(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})(?!\d|\.\d)")
_H = r"[0-9a-f]{1,4}"
_IPV6_FULL = re.compile(rf"(?<![\w:])(?:{_H}:){{7}}{_H}(?![\w:])", re.IGNORECASE)
# compressed forms need a digit (db::add, cafe::beef are code, not addresses); a bare
# a bare loopback address or its bracketed URL form only outside a subscript.
_IPV6_COMP = re.compile(
    rf"(?<![\w:])(?:(?:{_H}:){{1,7}}:(?:{_H}(?::{_H}){{0,6}})?|::(?:{_H}:){{1,6}}{_H}"
    rf"|::(?:ffff:)?\d{{1,3}}(?:\.\d{{1,3}}){{3}})(?![\w:])", re.IGNORECASE)
_CC = ":" * 2
_IPV6_LOOP = re.compile(rf"(?<![\w\]\)])\[{_CC}1\](?!\w)|(?<![\w\[:]){_CC}1(?![\w:\]])")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@((?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,})")
_HOME = re.compile(r"(?:/var/home|/home|/Users)/([A-Za-z0-9._-]+)|[A-Za-z]:[\\/]Users[\\/]([^\\/\s\"']+)")
_ENTROPY_CAND = re.compile(r"[A-Za-z0-9_+/=-]{28,}")
_HEX = re.compile(r"[0-9a-fA-F]+")
_SECRET_WORD = re.compile(r"secret|token|api[_-]?key|passw|private|credential|auth", re.IGNORECASE)
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


def glob_re(pattern, ci=False):
    """``**`` spans directories, ``*``/``?`` do not; a slash-less pattern matches any name.

    *ci* makes the match case-insensitive (deny and allow globs always are)."""
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
    rx = body + r"\Z" if "/" in pattern else r"(?:.*/)?" + body + r"\Z"
    return re.compile(rx, re.IGNORECASE if ci else 0)


_ALNUM = r"[^\W_]"  # a unicode letter or digit; "_" and "-" are token boundaries


def _load_words(path):
    """Compile the private word list: case-insensitive, whole-token (no letter/digit
    right before or after), ``re:`` lines are regexes.  A bad entry raises ValueError
    naming its line number only (the entry itself is never echoed)."""
    pats = []
    for n, raw in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        w = raw.strip()
        if not w or w.startswith("#"):
            continue
        try:
            if w.startswith("re:"):
                pats.append(re.compile(w[3:], re.IGNORECASE))
            else:
                pats.append(re.compile(rf"(?<!{_ALNUM})" + re.escape(w) + rf"(?!{_ALNUM})",
                                       re.IGNORECASE))
        except (re.error, OverflowError, RecursionError, MemoryError):
            raise ValueError(f"bad regex in word list line {n}") from None
    return pats


def build_config(words_file=None, rules_files=(), allow=None, allow_value=None, **over):
    """Merge DEFAULTS, rules files, *allow*/*allow_value* entries and keyword overrides."""
    cfg = {k: list(v) if isinstance(v, list) else v for k, v in DEFAULTS.items()}
    allow_entries = []
    value_entries = []
    for rf in rules_files:
        with open(rf, "rb") as fh:
            sect = tomllib.load(fh).get("scrub", {})
        allow_entries += sect.pop("allow", [])
        value_entries += sect.pop("allow_value", [])
        for k, v in sect.items():
            if k not in cfg:
                raise ValueError(f"unknown [scrub] key {k!r} in {rf}")
            cfg[k] = cfg[k] + list(v) if isinstance(v, list) else v
    allow_entries += list(allow or [])
    value_entries += list(allow_value or [])
    cfg.update(over)
    for e in allow_entries:
        bad = set(e.get("rules", [])) - set(RULES)
        if bad or "path" not in e:
            raise ValueError(f"bad allow entry (rules {sorted(bad)}): needs path + known rules")
    cfg["_allow"] = [(glob_re(e["path"], ci=True), set(e["rules"])) for e in allow_entries]
    cfg["_allow_value"] = _value_allows(value_entries)
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


def _entropy_hits(line, cfg):
    """High-entropy tokens on the line: mixed case+digit (>= entropy_min); letters+digits
    of one case (length >= 32, >= entropy_min + 0.2); plain hex only on a line naming a secret."""
    secretish = _SECRET_WORD.search(line)
    out = []
    for m in _ENTROPY_CAND.finditer(line):
        t = m.group()
        lo, up, dg = (re.search(p, t) for p in ("[a-z]", "[A-Z]", r"\d"))
        if _HEX.fullmatch(t):
            if len(t) >= 32 and secretish and entropy(t) >= 3.0:
                out.append(t)
        elif lo and up and dg:
            if entropy(t) >= cfg["entropy_min"]:
                out.append(t)
        elif dg and (lo or up) and len(t) >= 32 and entropy(t) >= cfg["entropy_min"] + 0.2:
            out.append(t)
    return out


def _ipv6_hits(line):
    """The address-shaped matches (a compressed form needs a digit: db::add is code)."""
    return ([m.group() for m in _IPV6_FULL.finditer(line)]
            + [m.group() for m in _IPV6_LOOP.finditer(line)]
            + [m.group() for m in _IPV6_COMP.finditer(line) if re.search(r"\d", m.group())])


def scan_line_values(line, cfg, find_shapes):
    """``{rule: [matched text, ...]}`` for one line; an empty list never happens.

    The matched text is kept only to hash it against ``[[scrub.allow_value]]``:
    it is never printed, returned by :func:`scan_line` or stored in a ``Hit``."""
    hosts = [m.group() for p in cfg["_host_re"] for m in p.finditer(line)]
    hosts += [m.group() for m in _HOST.finditer(line)]
    found = {
        "hostname": hosts,
        "ipv4": [m.group() for m in _IPV4.finditer(line) if not _ip_ok(m, cfg)],
        "ipv6": _ipv6_hits(line),
        "email": [m.group() for m in _EMAIL.finditer(line) if not _email_ok(m, cfg)],
        "token-shape": [m.group() for p in _TOKENS for m in p.finditer(line)],
        "high-entropy": _entropy_hits(line, cfg),
        "credential-shape": [m.group() for m in find_shapes(line)],
        "home-path": [m.group() for m in _HOME.finditer(line)
                      if (m.group(1) or m.group(2)).lower() not in cfg["allow_home_names"]],
        "private-word": [m.group() for p in cfg["_words"] for m in p.finditer(line)],
    }
    return {rule: vals for rule, vals in found.items() if vals}


def scan_line(line, cfg, find_shapes):
    """The rules that fire on one line (a set; values are not retained)."""
    return set(scan_line_values(line, cfg, find_shapes))


def _allowed(rel, cfg):
    skip = set()
    for rx, rules in cfg["_allow"]:
        if rx.match(rel):
            skip |= rules
    return skip


def _sha(text):
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def _allowed_shas(rel, rule, cfg):
    return cfg["_allow_value"].get((rel, rule), ())


def _value_allows(entries):
    """Validate ``[[scrub.allow_value]]`` entries -> ``{(path, rule): {sha256, ...}}``.

    Fail closed: a missing/empty key, a glob in path, an unknown rule or a malformed
    hash raises.  The message carries the entry number only -- path and value are
    never echoed (a path can itself hold a private word)."""
    out = {}
    for n, e in enumerate(entries, 1):
        if not isinstance(e, dict):
            raise ValueError(f"bad allow_value entry {n}: expected a table")
        bad = [k for k in _VALUE_KEYS if not isinstance(e.get(k), str) or not e[k].strip()]
        if bad:
            raise ValueError(f"bad allow_value entry {n}: missing or empty {sorted(bad)}")
        path, rule = e["path"], e["rule"]
        if any(c in path for c in "*?[]"):
            raise ValueError(f"bad allow_value entry {n}: path must be exact, no globs")
        if rule not in RULES:
            raise ValueError(f"bad allow_value entry {n}: unknown rule")
        digest = e["value_sha256"].strip().lower()
        if not _SHA256.fullmatch(digest):
            raise ValueError(f"bad allow_value entry {n}: value_sha256 is not 64 hex chars")
        out.setdefault((path, rule), set()).add(digest)
    return out


def scan_tree(root, cfg, with_skipped=False):
    """Hits for every file under *root* (``.git`` skipped, symlinks never followed).

    A rule that is not path-wide allowed still drops its hit only when *every*
    value it matched on the line is allowed by ``[[scrub.allow_value]]``."""
    root = Path(root)
    find_shapes = _shape_finder()
    hits, skipped = [], []
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root).as_posix()
        if ".git" in p.relative_to(root).parts:
            continue
        skip = _allowed(rel, cfg)
        if any(w.search(rel) for w in cfg["_words"]) and "private-word" not in skip:
            digest = _sha(rel)
            if digest not in _allowed_shas(rel, "private-word", cfg):
                hits.append(Hit(rel, 0, "private-word", digest))
        if p.is_symlink():  # never allow-listable: a link target is a path leak
            hits.append(Hit(rel, 0, "symlink"))
            continue
        if not p.is_file():
            continue
        data = p.read_bytes()
        if b"\x00" in data[:8192]:
            skipped.append(rel)
            continue
        for n, line in enumerate(data.decode("utf-8", "replace").splitlines(), 1):
            for rule, vals in sorted(scan_line_values(line, cfg, find_shapes).items()):
                if rule in skip:
                    continue
                pending = sorted({_sha(v) for v in vals} - set(_allowed_shas(rel, rule, cfg)))
                if pending:
                    hits.append(Hit(rel, n, rule, pending[0]))
    return (hits, skipped) if with_skipped else hits


def path_tag(path):
    """``<path-hash:sha256(path)[:12]>``: a path stand-in that cannot leak a word."""
    return "<path-hash:" + hashlib.sha256(path.encode()).hexdigest()[:12] + ">"


def shown_path(hit, words=()):
    """The path to print: hashed for private-word path hits and for any path that
    itself contains a private word."""
    if (hit.rule == "private-word" and hit.line == 0) or any(w.search(hit.path) for w in words):
        return path_tag(hit.path)
    return hit.path


def report(hits, out=None, skipped=(), words=(), hash_hits=False):
    """``path:line: rule`` (plus `` sha256=<hex>`` under *hash_hits*); never a value."""
    out = out or sys.stdout
    for h in hits:
        tail = f" sha256={h.sha}" if hash_hits and h.sha else ""
        print(f"{shown_path(h, words)}:{h.line}: {h.rule}{tail}", file=out)
    for s in skipped:
        print(f"{path_tag(s)}: binary file not scanned", file=out)
    files = len({h.path for h in hits})
    print(f"scrub_check: {len(hits)} hit(s) in {files} file(s)", file=out)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("tree")
    ap.add_argument("--rules", action="append", default=[], help="rules.toml (repeatable)")
    ap.add_argument("--words", help="private word list file (stays private)")
    ap.add_argument("--resolve", metavar="HASH", help="print the path(s) behind a <path-hash:HASH>")
    ap.add_argument("--hash-hits", action="store_true",
                    help="append sha256=<hex> of the matched value to every hit line "
                         "(the hash is for [[scrub.allow_value]]; the value stays hidden)")
    args = ap.parse_args(argv)
    if not Path(args.tree).is_dir():
        print(f"scrub_check: not a directory: {args.tree}", file=sys.stderr)
        return 2
    try:
        cfg = build_config(words_file=args.words, rules_files=args.rules)
    except (OSError, ValueError, re.error, tomllib.TOMLDecodeError) as exc:
        print(f"scrub_check: bad config: {type(exc).__name__}", file=sys.stderr)
        detail = str(exc) if isinstance(exc, ValueError) else ""
        if "word list line" in detail or "allow_value entry" in detail:  # both echo nothing
            print(f"scrub_check: {detail}", file=sys.stderr)
        return 2
    if not args.words and not args.resolve:
        print("scrub_check: warning: no --words, the private-word rule is inactive", file=sys.stderr)
    if args.resolve:
        found = [p.relative_to(args.tree).as_posix() for p in sorted(Path(args.tree).rglob("*"))
                 if path_tag(p.relative_to(args.tree).as_posix()) == f"<path-hash:{args.resolve}>"]
        print("\n".join(found))
        return 0 if found else 1
    hits, skipped = scan_tree(args.tree, cfg, with_skipped=True)
    report(hits, skipped=skipped, words=cfg["_words"], hash_hits=args.hash_hits)
    return 1 if hits else 0


if __name__ == "__main__":
    raise SystemExit(main())
