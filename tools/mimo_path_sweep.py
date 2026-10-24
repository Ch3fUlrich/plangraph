#!/usr/bin/env python3
"""Read-only Mimo path audit; emit one secret-safe JSON object per bad line."""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
from pathlib import Path
import re


ACCEPTED = {"mimo/mimo-v2.6-flash", "mimo/mimo-v2.6-pro"}
MODEL = re.compile(r"[\w.-]+/mimo[\w.-]*|(?<![\w/.-])mimo-v2\.6-[a-z]+")
SKIP = {".git", "node_modules", ".venv", "__pycache__"}
SUFFIXES = {".json", ".jsonc", ".md", ".yaml", ".yml", ".toml", ".txt", ".prompt", ".config"}
DEFAULT_ROOTS = (Path(__file__).resolve().parents[1], Path("~/code/AutoOS"),
                 Path("~/code/routing"), Path("~/.config/opencode"),
                 Path("~/.claude"), Path("~/.agents"))


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
                continue
            stack = []
            for number, line in enumerate(lines, 1):
                exempt = (path.suffix.lower() in {'.json', '.jsonc'}
                          and stack[-3:] == ['providers', 'mimo', 'models']
                          and re.fullmatch(r'\s*"mimo-v2\.6-(?:flash|pro)":\s*\{\s*', line))
                if path.suffix.lower() in {'.json', '.jsonc'}:
                    tokens = list(re.finditer(r'"(?:[^"\\]|\\.)*"|//[^\n]*|/\*.*?\*/|[{}:\[\]]', line))
                    key = None
                    for i, token in enumerate(tokens):
                        value = token.group()
                        if value.startswith(('//', '/*')):
                            continue
                        if value.startswith('"') and i + 1 < len(tokens) and tokens[i + 1].group() == ':':
                            key = json.loads(value)
                        elif value in ('{', '['):
                            stack.append(key)
                            key = None
                        elif value in ('}', ']') and stack:
                            stack.pop()
                if exempt:
                    continue
                bad = [m.group() for m in MODEL.finditer(line) if m.group() not in ACCEPTED]
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
