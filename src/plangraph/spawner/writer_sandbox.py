"""Writer permission deny rules and sandbox enforcement (Part B)."""
from __future__ import annotations

import json

# Leading '*' so the same pattern matches a bash command line (`cat /proc/1/environ`)
# and a read path alike.
WRITER_DENY_PATTERNS = (
    "*/proc/*/environ*",
    "*/proc/*/cmdline*",
    "*/proc/*/mem*",
    "*/.config/plangraph*",
    "*/.local/state/plangraph*",
    "*keeper.db*",
)


def merge_writer_permissions(cfg: dict) -> dict:
    """Merge deny rules into writer config and return the permission dict.

    # Note: bash pattern denial is best effort and not a kernel sandbox.
    """
    perm = cfg.setdefault("permission", {})
    for tool in ("read", "bash"):
        current = perm.get(tool)
        if isinstance(current, dict):
            current.update({p: "deny" for p in WRITER_DENY_PATTERNS})
        elif isinstance(current, str):
            perm[tool] = {"*": current, **{p: "deny" for p in WRITER_DENY_PATTERNS}}
        else:
            perm[tool] = {p: "deny" for p in WRITER_DENY_PATTERNS}
    if "external_directory" not in perm:
        perm["external_directory"] = "deny"
    elif isinstance(perm["external_directory"], dict):
        perm["external_directory"].setdefault("*", "deny")
    return perm


def apply_writer_sandbox(child_env: dict) -> None:
    try:
        cfg = json.loads(child_env.get("OPENCODE_CONFIG_CONTENT") or "{}")
        if not isinstance(cfg, dict):
            return
        merge_writer_permissions(cfg)
        child_env["OPENCODE_CONFIG_CONTENT"] = json.dumps(cfg, sort_keys=True)
    except (ValueError, TypeError):
        pass
