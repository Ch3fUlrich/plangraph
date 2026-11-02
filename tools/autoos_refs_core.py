"""File-level parts of tools/autoos_refs_check.py: what makes a name secret,
what counts as an AutoOS mention, how a hit line is redacted and how one file
or one symlink is checked.

A hit is a ``(kind, path, detail)`` tuple; ``detail`` is the redacted matching
line, the link target, ``SECRET-PATH`` or ``broken-link``.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import re

Hit = tuple[str, str, str]

# Word keywords are matched on token boundaries (the filename split on
# non-alphanumerics), so "monkey" / "keyboard" do not match "key".
SECRET_KEYWORDS = frozenset(
    {"token", "secret", "credential", "credentials", "password", "passwd",
     "apikey", "api_key", "id_rsa", "id_ed25519", "key", "authorization"}
)

SECRET_SUFFIXES = (".pem", ".key", ".p12", ".env")

# Compound keywords that contain separators; matched against the filename
# with its separators normalized to underscores.
_COMPOUND_KEYWORDS = ("id_rsa", "id_ed25519", "api_key")

_WORD_SPLIT_RE = re.compile(r"[^a-z0-9]+")

# "autoos" as a path component or word, any case: /AutoOS, AutoOS/,
# autoos-litellm.service, %h/code/AutoOS.
AUTOOS_RE = re.compile(r"(?<![A-Za-z0-9])autoos(?![A-Za-z0-9])", re.IGNORECASE)

MAX_LINE_CHARS = 160


def _load_masker():
    """``redact_credentials`` from tools/hooks/redact_common.py, loaded by path.

    One home for the masking rules (the Stop hook ships the same file) and no
    sys.path change when this module is imported. A missing file raises: a
    scanner must never fall back to printing unmasked lines.
    """
    path = Path(__file__).resolve().parent / "hooks" / "redact_common.py"
    spec = importlib.util.spec_from_file_location("redact_common", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.redact_credentials


_mask_credentials = _load_masker()


def is_secret_name(name: str) -> bool:
    """True when the file *name* carries a secret keyword (token boundaries)."""
    name = name.lower()
    if name.endswith(SECRET_SUFFIXES):
        return True
    tokens = [w for w in _WORD_SPLIT_RE.split(name) if w]
    if set(tokens) & (SECRET_KEYWORDS - set(_COMPOUND_KEYWORDS)):
        return True
    for kw in _COMPOUND_KEYWORDS:
        parts = kw.split("_")
        width = len(parts)
        if any(tokens[i : i + width] == parts for i in range(len(tokens) - width + 1)):
            return True
    return False


def looks_secret(path: Path) -> bool:
    """True when the filename looks like a secret and must not be opened."""
    return is_secret_name(path.name)


def has_autoos(text: str) -> bool:
    """Case-insensitive match of 'autoos' as a path component or word."""
    return bool(AUTOOS_RE.search(text))


def redact(line: str) -> str:
    """Mask credentials anywhere on a matched line; flatten tabs; truncate.

    Masking comes first, so a secret that straddles the truncation point is
    never half shown. The rules are redact_common.redact_credentials.
    """
    line = _mask_credentials(line, "<redacted>")
    line = line.replace("\t", " ").replace("\r", " ").replace("\n", " ")
    return line[:MAX_LINE_CHARS]


def _read_hits(
    source: Path, shown: Path, kind: str, sniff_binary: bool
) -> list[Hit]:
    """Matching lines of *source*, reported under the path *shown*."""
    hits: list[Hit] = []
    try:
        with open(source, "r", encoding="utf-8", errors="replace") as f:
            if sniff_binary:
                if "\0" in f.read(4096):
                    return []
                f.seek(0)
            for line in f:
                if has_autoos(line):
                    hits.append((kind, str(shown), redact(line.rstrip("\r\n").strip())))
    except OSError:
        pass
    return hits


def _check_link(path: Path, kind: str) -> list[Hit]:
    """A symlink: report it when it points at AutoOS, else follow it to the
    file a reader would open and scan that content (reported under the link's
    own path). A secret-named target is reported, never opened; a dangling or
    looping link has nothing to read.
    """
    try:
        target = os.readlink(path)
    except OSError:
        return []
    if has_autoos(target):
        return [(kind, str(path), redact(target))]
    try:
        real = path.resolve(strict=True)
    except (OSError, RuntimeError):
        return []
    if has_autoos(str(real)):
        return [(kind, str(path), redact(str(real)))]
    if looks_secret(real):
        return [(kind, str(path), "SECRET-PATH")]
    if not real.is_file():
        return []
    return _read_hits(real, path, kind, sniff_binary=True)


def check_file(path: Path, kind: str, is_text_script: bool = False) -> list[Hit]:
    """AutoOS hits in one file or symlink; secret-named files are never opened.

    *is_text_script* skips binary files (a NUL in the first 4 KiB) - for
    directories such as ~/.local/bin that also hold executables. A followed
    symlink target is always sniffed.
    """
    if looks_secret(path):
        return [(kind, str(path), "SECRET-PATH")]
    if path.is_symlink():
        return _check_link(path, kind)
    if path.is_file():
        return _read_hits(path, path, kind, sniff_binary=is_text_script)
    return []
