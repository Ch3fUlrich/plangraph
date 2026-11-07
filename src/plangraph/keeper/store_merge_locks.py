"""Principal-bound merge lease locks."""
from .errors import Refused
from .store_cards import _text

_LOCK_ROLES = ("router", "system", "l1", "worker", "spawner", "phase-worker")
_OWNER_MAX = 512
_ACQUIRE_KEYS = frozenset(("owner", "ttl_s"))
_RELEASE_KEYS = frozenset(("token", "owner"))


class MergeLocksMixin:
    # -------------------------------------------------------- merge locks
    # A lease lock: expiry is what makes it safe, the token is only an
    # advisory generation that grows on each (re)acquisition.  The keeper
    # never fences writes store-side: a stale writer's later writes are not
    # rejected.
    # Owners are bound to the calling principal and stored as
    # ``<escaped principal>:<owner>``, with ``%`` and ``:`` in the principal
    # name percent-escaped so the first ``:`` always ends the principal
    # segment.
    def lock_acquire(self, principal, repo, body):
        """Acquire/renew the merge lock for *repo*.

        Return the principal-prefixed owner and advisory generation.
        """
        from .worker_policy import check_lock_project
        check_lock_project(self, principal, repo)
        return self._run(principal, "lock_acquire", None,
                         lambda now: self._lock_acquire(principal, repo, body,
                                                        now))

    def _lock_acquire(self, principal, repo, body, now):
        self._require_role(principal, *_LOCK_ROLES)
        repo = _text("repo", repo, required=True, cap=512)
        from .store_worker import _check_body, _lock_owner, _escape_principal_name
        _check_body(body, _ACQUIRE_KEYS, "lock_acquire")
        owner = _text("owner", body.get("owner"), required=True, cap=256)
        owner = _lock_owner(principal, owner)
        if len(owner) > _OWNER_MAX:
            raise Refused("validation",
                          f"owner (with the principal prefix) exceeds {_OWNER_MAX} characters")
        ttl = body.get("ttl_s")
        if (isinstance(ttl, bool) or not isinstance(ttl, int)
                or not 1 <= ttl <= 86400):
            raise Refused("validation", "ttl_s must be an integer in 1..86400")
        t = int(now)
        with self._write():
            row = self.conn.execute(
                "SELECT token, owner, expires_at FROM merge_locks WHERE repo=?",
                (repo,)).fetchone()
            if row is None:
                token = 1
                self.conn.execute(
                    "INSERT INTO merge_locks(repo,token,owner,expires_at)"
                    " VALUES(?,?,?,?)", (repo, token, owner, t + ttl))
            elif (row["owner"] != "" and row["expires_at"] > t
                  and row["owner"] != owner):
                raise Refused("conflict",
                              "merge lock for repo is held by another owner")
            elif row["owner"] != "" and row["expires_at"] > t:
                token = row["token"]
                self.conn.execute(
                    "UPDATE merge_locks SET expires_at=? WHERE repo=?",
                    (t + ttl, repo))
            else:
                token = row["token"] + 1
                self.conn.execute(
                    "UPDATE merge_locks SET token=?, owner=?, expires_at=?"
                    " WHERE repo=?", (token, owner, t + ttl, repo))
        return {"token": token, "owner": owner}

    def lock_release(self, principal, repo, body):
        """Release using the owner and generation returned by ``lock_acquire``."""
        from .worker_policy import check_lock_project
        check_lock_project(self, principal, repo)
        return self._run(principal, "lock_release", None,
                         lambda now: self._lock_release(principal, repo, body,
                                                        now))

    def _lock_release(self, principal, repo, body, now):
        self._require_role(principal, *_LOCK_ROLES)
        repo = _text("repo", repo, required=True, cap=512)
        from .store_worker import _check_body, _lock_owner, _escape_principal_name
        _check_body(body, _RELEASE_KEYS, "lock_release")
        owner = _text("owner", body.get("owner"), required=True,
                      cap=_OWNER_MAX)
        segment, sep, _rest = owner.partition(":")
        if not sep or segment != _escape_principal_name(principal.name):
            raise Refused("conflict", "merge lock is held by another principal")
        token = body.get("token")
        if isinstance(token, bool) or not isinstance(token, int):
            raise Refused("validation", "token must be an integer")
        t = int(now)
        with self._write():
            row = self.conn.execute(
                "SELECT token, owner, expires_at FROM merge_locks WHERE repo=?",
                (repo,)).fetchone()
            if (row is None or row["owner"] == "" or row["expires_at"] <= t
                    or row["token"] != token or row["owner"] != owner):
                raise Refused("conflict", "merge lock is not held by this token")
            self.conn.execute(
                "UPDATE merge_locks SET owner='', expires_at=0 WHERE repo=?"
                " AND token=?", (repo, token))
        return {}
