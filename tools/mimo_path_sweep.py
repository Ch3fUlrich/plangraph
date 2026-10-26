#!/usr/bin/env python3
"""Read-only Mimo path audit; emit one secret-safe JSON object per bad line."""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
from pathlib import Path
import re


ACCEPTED = {"mimo/mimo-v2.6-flash", "mimo/mimo-v2.6-pro",
            "opencode/mimo-v2.6-flash-free"}
MODEL = re.compile(r"[\w.-]+/mimo[\w.-]*|(?<![\w/.-])mimo-v2\.6-[a-z]+")
SKIP = {".git", "node_modules", ".venv", "__pycache__"}
SUFFIXES = {".json", ".jsonc", ".md", ".yaml", ".yml", ".toml", ".txt", ".prompt", ".config"}
DEFAULT_ROOTS = (Path(__file__).resolve().parents[1], Path("~/code/AutoOS"),
                 Path("~/code/routing"), Path("~/.config/opencode"),
                 Path("~/.claude"), Path("~/.agents"))


def model_key_exemptions(text):
    """Tokenize JSONC once, retaining real object ancestry and source lines."""
    tokens = list(re.finditer(r'"(?:[^"\\]|\\.)*"|//[^\n]*|/\*[\s\S]*?(?:\*/|$)|[{}:\[\],]|"', text))
    if any(t.group() == '"' for t in tokens):
        raise ValueError('unterminated string')
    if any(t.group().startswith('/*') and (len(t.group()) < 4 or not t.group().endswith('*/')) for t in tokens):
        raise ValueError('unterminated block comment')
    tokens = [t for t in tokens if not t.group().startswith(('//', '/*'))]
    stack, key, exempt = [], None, {}
    brackets = []
    for i, token in enumerate(tokens):
        value = token.group()
        if value.startswith('"'):
            if i + 1 < len(tokens) and tokens[i + 1].group() == ':':
                key = json.loads(value)
                if stack[-3:] == ['providers', 'mimo', 'models'] and key in {
                        'mimo-v2.6-flash', 'mimo-v2.6-pro'}:
                    line = text.count('\n', 0, token.start()) + 1
                    start = token.start() - (text.rfind('\n', 0, token.start()) + 1)
                    exempt.setdefault(line, []).append((start, start + len(value)))
        elif value in ('{', '['):
            brackets.append(value)
            stack.append(key)
            key = None
        elif value in ('}', ']'):
            if not brackets or brackets.pop() != {'}': '{', ']': '['}[value]:
                raise ValueError('bracket mismatch')
            if stack:
                stack.pop()
            key = None
        elif value == ',':
            key = None
    if brackets:
        raise ValueError('unclosed bracket')
    return exempt


def scan(root: Path, allow: list[str]):
    """Yield bad lines; output only matched model tokens, never raw config values."""
    root = root.expanduser().absolute()
    if not root.exists():
        return
    for directory, dirs, files in os.walk(root, followlinks=False):
        parent = Path(directory)
        def allowed(path):
            rel = path.relative_to(root).as_posix()
            return any(fnmatch.fnmatchcase(rel, p) or
                       (p.endswith("/") and (rel + "/").startswith(p)) for p in allow)
        dirs[:] = sorted(d for d in dirs if d not in SKIP and not allowed(parent / d))
        for name in sorted(files):
            path = parent / name
            if path.is_symlink() or allowed(path):
                continue
            if path.suffix.lower() not in SUFFIXES and not any(
                word in name.lower() for word in ("config", "prompt", "skill", "claude")
            ):
                continue
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeError):
                yield {'file': str(path.relative_to(root)), 'error': 'unreadable file'}
                continue
            try:
                exemptions = model_key_exemptions('\n'.join(lines)) if path.suffix.lower() in {'.json', '.jsonc'} else {}
            except ValueError as exc:
                yield {'file': str(path.relative_to(root)), 'error': str(exc)}
                continue
            for number, line in enumerate(lines, 1):
                bad = [m.group() for m in MODEL.finditer(line) if m.group() not in ACCEPTED
                       and not any(start <= m.start() and m.end() <= end
                                   for start, end in exemptions.get(number, ()))]
                if bad:
                    # Deliberately omit ALL surrounding content, even unknown key formats.
                    yield {"root": str(root), "file": path.relative_to(root).as_posix(),
                           "line": number, "text": "[context redacted] " + " ".join(bad),
                           "suggested": sorted({"mimo/mimo-v2.6-pro" if "pro" in m
                                                else "mimo/mimo-v2.6-flash" for m in bad})}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("roots", nargs="*", type=Path)
    parser.add_argument("--allow-file", type=Path,
                        default=Path(__file__).with_name("mimo_path_allow.txt"))
    args = parser.parse_args()
    allow = [line.strip() for line in args.allow_file.read_text().splitlines()
             if line.strip() and not line.lstrip().startswith("#")]
    found = False
    for root in args.roots or DEFAULT_ROOTS:
        for hit in scan(root, allow):
            print(json.dumps(hit))
            found = True
    return int(found)


if __name__ == "__main__":
    raise SystemExit(main())
