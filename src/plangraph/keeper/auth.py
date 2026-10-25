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
import time
from dataclasses import dataclass, field
from datetime import datetime

from .errors import Refused
from .schema import ROLES

__all__ = ["Principal", "RunPrincipal", "OversightPrincipal", "RUN_PREFIX",
           "OVERSIGHT_PREFIX", "add_principal", "add_oversight_principal",
           "authenticate", "revoke", "get_principal", "list_principals",
           "hash_token", "norm_family", "validate_family", "set_clock"]


@dataclass
class Principal:
    name: str
    role: str
    projects: list = field(default_factory=list)
    family: str | None = None


#: Synthetic run-token principals are named ``run:<run_id>``; no stored
#: principal may use that prefix, so the name proves the token path.
RUN_PREFIX = "run:"

#: RT-OVERSIGHT-KEEPER-SCOPE: the third token namespace, disjoint from the
#: prefixless principal tokens and from ``pgr1_`` run tokens.
OVERSIGHT_PREFIX = "pgo1_"


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


@dataclass
class OversightPrincipal(Principal):
    """RT-OVERSIGHT-KEEPER-SCOPE: a short-lived, scoped ``oversight`` identity.

    ``projects`` is copied verbatim from the principals row so the store's
    per-verb identity re-check (``_check_principal``) compares equal; the token
    row's id/generation/scope are re-read on every use
    (``Store._check_oversight_principal``).
    """

    token_id: int = 0
    generation: int = 0
    scope: str = "plan-write"


def hash_token(token: str) -> str:
    """The only form of a token this system ever stores."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def set_clock(clock) -> None:
    """Deprecated: no process-global clock; authenticate defaults to time.time()."""
    pass


def _now(now):
    return time.time() if now is None else float(now)


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


def _check_projects(projects) -> list:
    """Validate a principal's project scope; refuse a bare string/non-string."""
    if isinstance(projects, (str, bytes)) or not hasattr(projects, "__iter__"):
        raise Refused("validation", "projects must be a list of strings")
    projects = list(projects)
    if any(not isinstance(project, str) for project in projects):
        raise Refused("validation", "projects must be a list of strings")
    return projects


def add_principal(conn, name, role, projects, family=None) -> str:
    """Create a principal and return its token (shown once).

    ``projects`` must be a list of project-name strings.  A bare string is
    refused rather than silently exploded into characters by ``list()``, and
    any non-string entry is refused so a principal cannot smuggle a value that
    later comparisons would treat differently.
    """
    if role == "oversight":
        # R1/R2: oversight identity is only minted through the TTL-capable
        # ``add_oversight_principal`` + ``Store.mint_oversight_token`` pair; a
        # principals-column token has no TTL and would never expire.
        raise Refused("validation",
                      "role 'oversight' needs add_oversight_principal")
    if role not in ROLES:
        raise Refused("validation", f"unknown role {role!r}")
    if not isinstance(name, str) or name.startswith(RUN_PREFIX):
        raise Refused("validation", f"principal name may not start with {RUN_PREFIX!r}")
    family = validate_family(family)
    projects = _check_projects(projects)
    existing = conn.execute(
        "SELECT 1 FROM principals WHERE name=?", (name,)).fetchone()
    if existing is not None:
        raise Refused("conflict", f"principal {name!r} already exists")
    token = _mint_token()
    conn.execute(
        "INSERT INTO principals(name,role,projects,family,token_sha256,active)"
        " VALUES(?,?,?,?,?,1)",
        (name, role, json.dumps(projects), family,
         hash_token(token)))
    return token


def _mint_token() -> str:
    """A prefixless principal token that can never collide with a namespace.

    ``secrets.token_urlsafe`` draws from an alphabet that includes ``_``, so
    the text of a fresh token could start with ``pgo1_``; the dispatch would
    then depend on luck.  Minting re-rolls until it cannot happen (the lookup
    order in :func:`authenticate` is the second line of defence).
    """
    token = secrets.token_urlsafe(32)
    while token.startswith(OVERSIGHT_PREFIX):
        token = secrets.token_urlsafe(32)
    return token


def add_oversight_principal(conn, name, projects):
    """Provision the revocable ``oversight`` principals row (no token, no TTL).

    R5: the scope is copied verbatim and an empty list is refused outright --
    unlike the router's "all projects", ``[]`` would mean *nothing*, so fail
    closed at provisioning rather than mint an unusable identity.
    """
    if not isinstance(name, str) or not name or name.startswith(RUN_PREFIX):
        raise Refused("validation", f"invalid principal name {name!r}")
    projects = _check_projects(projects)
    if not projects:
        raise Refused("validation", "oversight principal needs a non-empty projects scope")
    existing = conn.execute(
        "SELECT 1 FROM principals WHERE name=?", (name,)).fetchone()
    if existing is not None:
        raise Refused("conflict", f"principal {name!r} already exists")
    conn.execute(
        "INSERT INTO principals(name,role,projects,family,token_sha256,active)"
        " VALUES(?,?,?,?,NULL,1)",
        (name, "oversight", json.dumps(projects), None))


def authenticate(conn, token, *, now=None) -> Principal:
    """Resolve a bearer token to an active principal, or refuse.

    Dispatch is by STORED MARKER, not by the token's text: the principals table
    is consulted first, so a prefixless principal token whose random bytes
    happen to start with ``pgo1_`` still resolves to its own row instead of
    being misrouted to (and refused by) the oversight lookup.  *now* is the
    clock an expiry is judged against; the default is wall time (time.time()).
    """
    if not token:
        raise Refused("forbidden", "missing token")
    row = conn.execute(
        "SELECT name,role,projects,family FROM principals"
        " WHERE token_sha256=? AND active=1", (hash_token(token),)).fetchone()
    if row is not None:
        return _as_principal(row)
    if isinstance(token, str) and token.startswith(OVERSIGHT_PREFIX):
        return _authenticate_oversight(conn, token, now=now)
    return _authenticate_run(conn, token)


def _expired(value, now) -> bool:
    """True when the ISO *expires_at* is missing, unreadable or in the past."""
    if not isinstance(value, str):
        return True
    try:
        expires = datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return True
    return expires <= _now(now)


def _authenticate_oversight(conn, token, *, now=None) -> OversightPrincipal:
    """Resolve a ``pgo1_`` token; an unknown or revoked row is refused here.

    Unknown tokens stay unaudited. When the token row resolves to an active
    oversight principal, an expired token is audited (code oversight:token_expired)
    through the denied budget and alarm path before refusing.
    """
    row = conn.execute(
        "SELECT o.id,o.principal,o.generation,o.scope,o.revoked_at,o.expires_at,"
        " p.role,p.projects,p.family,p.active"
        " FROM oversight_tokens o JOIN principals p ON p.name=o.principal"
        " WHERE o.token_sha256=?", (hash_token(token),)).fetchone()
    if row is None or row["revoked_at"] is not None:
        raise Refused("forbidden", "unknown or revoked oversight token")
    if not row["active"]:
        raise Refused("forbidden", "oversight principal is revoked")
    if row["role"] != "oversight":
        raise Refused("forbidden", "oversight principal role drifted")
    if _expired(row["expires_at"], now):
        cur_now = _now(now)
        from . import store_oversight
        store_oversight.audit_oversight_expired_auth(
            conn, row["principal"], row["id"], row["generation"], cur_now)
        raise Refused("forbidden", "oversight:token_expired token has expired")
    try:
        projects = json.loads(row["projects"] or "[]")
    except (ValueError, TypeError):
        projects = []
    return OversightPrincipal(row["principal"], "oversight", projects,
                              norm_family(row["family"]), row["id"],
                              row["generation"], row["scope"])



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
