"""Shared identity of a phase merge, used by its writer and evidence door."""


def merge_subject(gate: str) -> str:
    """Return the exact subject binding a merge to its phase gate."""
    return f"Merge phase {gate}"
