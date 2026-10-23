"""Shared repository-relative card path rule for planner and keeper boundaries."""
import re

_DRIVE_RE = re.compile(r"[A-Za-z]:")


def normalise_path(value: str) -> str | None:
    """Canonicalise separators and dot components without resolving parents."""
    path = value.strip().replace("\\", "/")
    parts = [p for p in path.split("/") if p not in ("", ".")]
    return ("/" if path.startswith("/") else "") + "/".join(parts) or None


def path_problem(name: str, original: str, path: str | None) -> str | None:
    if path is None:
        return f"{name} entry {original!r} is empty after normalisation"
    if path.startswith("/") or _DRIVE_RE.match(path):
        return f"{name} entry {original!r} must be a relative path"
    if ".." in path.split("/"):
        return f"{name} entry {original!r} must not contain '..'"
    return None
