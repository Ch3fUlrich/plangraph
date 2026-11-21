"""Principal-bound merge lease locks."""
import json
import re
from importlib.resources import files

from .errors import Refused
from .store_cards import _text

_LOCK_ROLES = ("router", "system", "l1", "worker", "spawner", "phase-worker")
_OWNER_MAX = 512
_ACQUIRE_KEYS = frozenset(("owner", "ttl_s"))
_RELEASE_KEYS = frozenset(("token", "owner"))
_LAND_STEPS = ("evidence", "close_parts", "host_sync", "cleanup")
_STAGE_NAMES = frozenset(("writing", "review", "fast_checks", "push", "ci", "delta_review", "go", "merge", "host_sync"))
_STAGE_SQL = files(__package__).joinpath("landing_stage_insert.sql").read_text(encoding="utf-8")
_LIST_STAGE_SQL = files(__package__).joinpath("landing_stage_list.sql").read_text(encoding="utf-8")


def _land_sha(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}", value):
        raise Refused("validation", "landing needs a full Git SHA")
    return value


def _land_number(value):
    if type(value) is not int or not 0 < value < 2**63:
        raise Refused("validation", "landing needs a positive PR number")
    return value


def _land_manifest(body):
    parts = body.get("parts")
    if not isinstance(parts, list) or not 1 <= len(parts) <= 100:
        raise Refused("validation", "landing needs 1..100 folded parts")
    manifest = []
    for part in parts:
        if not isinstance(part, dict):
            raise Refused("validation", "landing part must be an object")
        items = part.get("items", [])
        if not isinstance(items, list) or any(
                not isinstance(key, str) or not re.fullmatch(r"[\w.-]+:[\w.-]+", key) for key in items):
            raise Refused("validation", "landing part items must be keeper keys")
        ref = part.get("ref", "")
        if not isinstance(ref, str) or len(ref) > 256 or any(ch.isspace() for ch in ref):
            raise Refused("validation", "landing part ref must be a Git branch")
        manifest.append({"pr": _land_number(part.get("pr")), "head": _land_sha(part.get("head")),
                         "items": sorted(set(items)), "ref": ref})
    if len({part["pr"] for part in manifest}) != len(manifest):
        raise Refused("validation", "landing part PR appears twice")
    return sorted(manifest, key=lambda part: part["pr"])


def _landing_data(row):
    manifest = json.loads(row["manifest"])
    if isinstance(manifest, list):
        manifest = {"parts": manifest, "batch_key": f"train-{row['pr']}"}
    return {"repo": row["repo"], "pr": row["pr"], "base": row["base"], **manifest,
            "head": row["head"], "merge": row["merged"],
            "abandoned": bool(row["abandoned"]),
            "open_steps": list(_LAND_STEPS[row["cursor"]:]) if row["merged"] else []}


def _timing(row):
    start, end = row.get("started_at_ms"), row.get("ended_at_ms")
    if type(start) is not int or type(end) is not int or not 0 <= start <= end < 2**63:
        raise Refused("validation", "landing timestamps must be ordered Unix milliseconds")
    return start, end


class MergeLocksMixin:
    def land(self, principal, body):
        """Journal the lander's immutable manifest and issue fenced cursor updates."""
        return self._run(principal, "land", None, lambda now: self._land(principal, body, now),
                         success_message=None)

    def _land(self, principal, body, now):
        repo = body.get("repo")
        if not isinstance(repo, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}", repo):
            raise Refused("validation", "landing repository must be OWNER/NAME")
        repo = repo.lower()
        action = body.get("action")
        if action in ("stage", "ci_timings", "timeline"):
            return self._landing_timing(principal, repo, action, body)
        self._require_role(principal, "router", "system")
        if action == "acquire":
            return self._lock_acquire(principal, repo, {"owner": body.get("owner"), "ttl_s": 1800}, now)
        if action == "release":
            return self._lock_release(principal, repo, {"owner": body.get("owner"), "token": body.get("token")}, now)
        if action == "pending":
            rows = self.conn.execute(
                "SELECT * FROM landing_trains WHERE repo=? AND abandoned=0 AND cursor<? ORDER BY pr",
                (repo, len(_LAND_STEPS)))
            return {"trains": [_landing_data(row) for row in rows]}
        if action == "consumed":
            parts = _land_manifest({"parts": body.get("parts")})
            requested = {(part["pr"], part["head"]) for part in parts}
            landed = self.conn.execute(
                "SELECT * FROM landing_trains WHERE repo=? AND merged IS NOT NULL", (repo,))
            matched = {(part["pr"], part["head"]) for row in landed
                       for part in _landing_data(row)["parts"] if (part["pr"], part["head"]) in requested}
            return {"consumed": [{"pr": pr, "head": head} for pr, head in sorted(matched)]}
        identity = (repo, _land_number(body.get("pr")))
        select = "SELECT * FROM landing_trains WHERE repo=? AND pr=?"
        if action == "show":
            row = self.conn.execute(select, identity).fetchone()
            return {"train": _landing_data(row) if row else None}
        with self._write():
            from .store_worker import _escape_principal_name
            lease = self.conn.execute(
                "SELECT token,owner,expires_at FROM merge_locks WHERE repo=?", (repo,)).fetchone()
            if (lease is None or lease["owner"] != body.get("owner") or lease["token"] != body.get("token")
                    or not lease["owner"].startswith(_escape_principal_name(principal.name) + ":")
                    or lease["expires_at"] <= int(now)):
                raise Refused("conflict", "landing mutation requires the current keeper lease")
            if action == "prepare":
                parts = _land_manifest(body)
                batch = _text("batch_key", body.get("batch_key", f"train-{identity[1]}"), required=True, cap=256)
                manifest = json.dumps({"parts": parts, "batch_key": batch}, separators=(",", ":"))
                frozen = (_land_sha(body.get("base")), _land_sha(body.get("head")), manifest)
                self.conn.execute("INSERT INTO landing_trains(repo,pr,base,head,manifest) VALUES(?,?,?,?,?)"
                                  " ON CONFLICT (repo,pr) DO NOTHING", (*identity, *frozen))
                row = self.conn.execute(select, identity).fetchone()
                data = _landing_data(row)
                if ((row["base"], row["head"], data["parts"], data["batch_key"]) !=
                        (*frozen[:2], parts, batch) or row["abandoned"]):
                    raise Refused("conflict", "landing manifest differs from prepared train")
                return data
            row = self.conn.execute(select, identity).fetchone()
            if row is None:
                raise Refused("not_found", "landing train is not prepared")
            if action == "abandon" and row["merged"] is None:
                self.conn.execute("UPDATE landing_trains SET abandoned=1 WHERE repo=? AND pr=? AND abandoned=0",
                                  identity)
                return _landing_data(self.conn.execute(select, identity).fetchone())
            merge = _land_sha(body.get("merge"))
            if row["abandoned"] or row["merged"] not in (None, merge):
                raise Refused("conflict", "verified merge differs from the landing journal")
            if action == "landed" and row["merged"] is None:
                self.conn.execute("UPDATE landing_trains SET merged=? WHERE repo=? AND pr=?", (merge, *identity))
            elif action == "advance" and row["merged"] == merge:
                step = body.get("step")
                if step not in _LAND_STEPS or _LAND_STEPS.index(step) > row["cursor"]:
                    raise Refused("conflict", "landing cursor is out of order")
                if _LAND_STEPS.index(step) == row["cursor"]:
                    self.conn.execute("UPDATE landing_trains SET cursor=cursor+1 WHERE repo=? AND pr=?", identity)
            elif action != "landed":
                raise Refused("conflict", "invalid landing journal transition")
            return _landing_data(self.conn.execute(select, identity).fetchone())

    def _landing_timing(self, principal, repo, action, body):
        roles = ("router", "system", "spawner") if action == "stage" else ("router", "system")
        self._require_role(principal, *roles)
        batch = _text("batch_key", body.get("batch_key"), required=True, cap=256)
        if action == "timeline":
            stages = self.conn.execute(_LIST_STAGE_SQL, (repo, batch))
            jobs = self.conn.execute(
                "SELECT *, ended_at_ms-started_at_ms AS duration_ms FROM landing_ci_jobs WHERE repo=? AND batch_key=? "
                "ORDER BY run_id,attempt,started_at_ms,job_id", (repo, batch))
            return {"stages": [dict(row) for row in stages], "jobs": [dict(row) for row in jobs]}
        stage = body.get("stage") if action == "ci_timings" else body
        if not isinstance(stage, dict) or not isinstance(stage.get("stage"), str) or stage["stage"] not in _STAGE_NAMES:
            raise Refused("validation", "unknown landing stage")
        head = _land_sha(stage.get("head"))
        row = (repo, batch, stage["stage"], _land_number(stage.get("attempt")), head, *_timing(stage))
        with self._write():
            self.conn.execute(_STAGE_SQL, row)
            if action == "ci_timings":
                jobs = body.get("jobs")
                if stage["stage"] != "ci" or not isinstance(jobs, list) or len(jobs) > 500:
                    raise Refused("validation", "CI timing needs a CI stage and up to 500 jobs")
                run_id = _land_number(body.get("run_id"))
                attempt = _land_number(body.get("run_attempt"))
                conclusion = _text("run conclusion", body.get("run_conclusion"), required=True, cap=64)
                for job in jobs:
                    if not isinstance(job, dict):
                        raise Refused("validation", "CI job must be an object")
                    self.conn.execute(
                        "INSERT INTO landing_ci_jobs VALUES(?,?,?,?,?,?,?,?,?,?,?)"
                        " ON CONFLICT (repo,batch_key,run_id,attempt,job_id) DO NOTHING",
                        (repo, batch, run_id, attempt, _land_number(job.get("job_id")), head,
                         _text("job name", job.get("name"), required=True, cap=512),
                         _text("job conclusion", job.get("conclusion"), required=True, cap=64),
                         conclusion, *_timing(job)))
        return {"stage": stage["stage"], "attempt": row[3]}

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
