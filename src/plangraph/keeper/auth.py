"""Principals and tokens.

Tokens are generated once and only their SHA-256 is stored; ``add_principal``
returns the plaintext token, which is the single time it is visible.  Family is
the reviewer-independence marker: a reviewer from the claimant's family cannot
pass review, and a missing family fails closed.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
from dataclasses import dataclass, field

from .errors import Refused
from .schema import ROLES

__all__ = ["Principal", "RunPrincipal", "RUN_PREFIX", "add_principal", "authenticate", "revoke",
           "get_principal", "list_principals", "hash_token", "norm_family",
           "validate_family"]


@dataclass
class Principal:
    name: str
    role: str
    projects: list = field(default_factory=list)
    family: str | None = None


#: Synthetic run-token principals are named ``run:<run_id>``; no stored
#: principal may use that prefix, so the name proves the token path.
RUN_PREFIX = "run:"


@dataclass
class RunPrincipal(Principal):
    """M2B-RUN-TOKENS: what a run token authenticates as.

    Bound to one item, one epoch and one role (``writer`` acts as a ``worker``
    on heartbeat/evidence/submit, ``reviewer`` on verdict).  The store, not the
    token row alone, decides every use (``Store._check_run_principal``).
    """

    token_id: int = 0
    key: str = ""
    epoch: int = 0
    token_role: str = ""
    run_id: str = ""


def hash_token(token: str) -> str:
    """The only form of a token this system ever stores."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def norm_family(value):
    """Canonical reviewer family marker: ``None`` for unknown, else folded.

    ``None`` and a blank/whitespace string both mean "unknown" and must fail
    closed; ``teamC``, ``TEAMC`` and `` teamC `` are the *same* family.  The
    single home for both rules so no caller can compare raw spellings.
    """
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value.casefold() if value else None


_FAMILY_SOURCE_RE = re.compile(r"[A-Za-z0-9._-]+")


def validate_family(value):
    """Normalise a family marker, refusing a malformed non-blank value.

    ``None`` and blank stay allowed (they mean unknown).  A non-blank value is
    checked *before* casefolding: ``casefold`` happily turns non-ASCII source
    such as ``ß`` into an ASCII token (``ss``) and full-width letters into
    look-alikes, so the source must itself be plain ``[A-Za-z0-9._-]``.  The
    normalised result then always matches ``[a-z0-9._-]+``, and anything else
    is refused rather than silently laundered into unknown or into a token a
    different family would wrongly compare equal to.
    """
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if not stripped:
        return None
    if not _FAMILY_SOURCE_RE.fullmatch(stripped):
        raise Refused("validation", "family must match [a-z0-9._-]+")
    return stripped.casefold()


def _as_principal(row) -> Principal:
    try:
        projects = json.loads(row["projects"] or "[]")
    except (ValueError, TypeError):
        projects = []
    return Principal(row["name"], row["role"], projects,
                     norm_family(row["family"]))


def add_principal(conn, name, role, projects, family=None) -> str:
    """Create a principal and return its token (shown once).

    ``projects`` must be a list of project-name strings.  A bare string is
    refused rather than silently exploded into characters by ``list()``, and
    any non-string entry is refused so a principal cannot smuggle a value that
    later comparisons would treat differently.
    """
    if role not in ROLES:
        raise Refused("validation", f"unknown role {role!r}")
    if not isinstance(name, str) or name.startswith(RUN_PREFIX):
        raise Refused("validation", f"principal name may not start with {RUN_PREFIX!r}")
    family = validate_family(family)
    if isinstance(projects, (str, bytes)) or not hasattr(projects, "__iter__"):
        raise Refused("validation", "projects must be a list of strings")
    projects = list(projects)
    if any(not isinstance(project, str) for project in projects):
        raise Refused("validation", "projects must be a list of strings")
    existing = conn.execute(
        "SELECT 1 FROM principals WHERE name=?", (name,)).fetchone()
    if existing is not None:
        raise Refused("conflict", f"principal {name!r} already exists")
    token = secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO principals(name,role,projects,family,token_sha256,active)"
        " VALUES(?,?,?,?,?,1)",
        (name, role, json.dumps(projects), family,
         hash_token(token)))
    return token


def authenticate(conn, token) -> Principal:
    """Resolve a bearer token to an active principal, or refuse."""
    if not token:
        raise Refused("forbidden", "missing token")
    row = conn.execute(
        "SELECT name,role,projects,family FROM principals"
        " WHERE token_sha256=? AND active=1", (hash_token(token),)).fetchone()
    if row is None:
        return _authenticate_run(conn, token)
    return _as_principal(row)


def _authenticate_run(conn, token) -> RunPrincipal:
    """A run token resolves to its synthetic principal, or the request is refused.

    Expiry, epoch and item-state checks are the store's job at use time (they
    need its clock and are audited); an unknown or already revoked hash is
    refused here, so a revoked token never even becomes an audit actor.
    """
    row = conn.execute(
        "SELECT id,key,project,epoch,role,run_id,family,revoked_at FROM run_tokens"
        " WHERE token_sha256=?", (hash_token(token),)).fetchone()
    if row is None or row["revoked_at"] is not None:
        raise Refused("forbidden", "unknown or revoked token")
    writer = row["role"] == "writer"
    return RunPrincipal(f"{RUN_PREFIX}{row['run_id']}",
                        "worker" if writer else "reviewer", [row["project"]],
                        None if writer else norm_family(row["family"]),
                        row["id"], row["key"], row["epoch"], row["role"],
                        row["run_id"])


def get_principal(conn, name) -> Principal | None:
    """Look up a principal by name (used for family checks); None if unknown."""
    if not name:
        return None
    row = conn.execute(
        "SELECT name,role,projects,family FROM principals WHERE name=?",
        (name,)).fetchone()
    return _as_principal(row) if row is not None else None


def revoke(conn, name):
    """Deactivate a principal; its token stops authenticating."""
    cursor = conn.execute("UPDATE principals SET active=0 WHERE name=?", (name,))
    if cursor.rowcount == 0:
        raise Refused("not_found", f"no principal {name!r}")


def list_principals(conn):
    """All principals (admin/debug); never includes a token."""
    return [_as_principal(row) for row in conn.execute(
        "SELECT name,role,projects,family FROM principals ORDER BY name")]
