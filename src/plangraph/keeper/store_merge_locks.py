"""Principal-bound merge lease locks."""
import json
import re
from datetime import datetime, timezone
from importlib.resources import files

from plangraph.settings import LANDING_MODES

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
        entry = {"pr": _land_number(part.get("pr")), "head": _land_sha(part.get("head")),
                 "items": sorted(set(items)), "ref": ref}
        reviews = part.get("review_runs", {})
        if not isinstance(reviews, dict) or not set(reviews).issubset(items) or any(
                not isinstance(run, str) or not re.fullmatch(r"run-[A-Za-z0-9._-]{1,120}", run)
                for run in reviews.values()):
            raise Refused("validation", "landing reviews must cite a run for a manifest item")
        if reviews:
            entry["review_runs"] = dict(sorted(reviews.items()))
        manifest.append(entry)
    if len({part["pr"] for part in manifest}) != len(manifest):
        raise Refused("validation", "landing part PR appears twice")
    return sorted(manifest, key=lambda part: part["pr"])



def require_train_review(adapter, key, head, run_id):
    """Require one passing seat bound to this item and exact part head."""
    seats = adapter.review_seats({"key": key, "run_ids": json.dumps([run_id])}, head, "",
                                 allow_cli_reported=True, require_diff_head=True,
                                 require_explicit_accept=True) if adapter is not None else []
    if len(seats) != 1 or seats[0]["verdict"] != "pass":
        raise ValueError(f"{run_id}: expected one passing review seat")


def _saved_manifest(row):
    manifest = json.loads(row["manifest"])
    if isinstance(manifest, list):
        manifest = {"parts": manifest, "batch_key": f"train-{row['pr']}"}
    return manifest


def _landing_data(row):
    return {"repo": row["repo"], "pr": row["pr"], "base": row["base"],
            **_saved_manifest(row),
            "head": row["head"], "merge": row["merged"],
            "abandoned": bool(row["abandoned"]),
            "open_steps": list(_LAND_STEPS[row["cursor"]:]) if row["merged"] else []}


def _receipt_inputs(value, parts):
    """Validate the pre-landing metadata without accepting a local file path."""
    if not isinstance(value, dict):
        raise Refused("validation", "landing receipt inputs must be an object")
    train = value.get("train")
    if not isinstance(train, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,47}", train):
        raise Refused("validation", "landing receipt requires a batch name")
    inputs = {"train": train}
    for key in ("build_started_at", "go_at", "plan_item"):
        inputs[key] = _text(key, value.get(key), cap=256)
    for key in ("ci_reruns", "fit_commits", "left_out"):
        count = value.get(key)
        if type(count) is not int or not 0 <= count < 2**63:
            raise Refused("validation", f"invalid landing receipt {key}")
        inputs[key] = count
    reviews = value.get("review_runs")
    if not isinstance(reviews, dict) or not {str(part["pr"]) for part in parts}.issuperset(reviews):
        raise Refused("validation", "landing receipt reviews must name frozen parts")
    inputs["review_runs"] = {}
    for pr, runs in reviews.items():
        if not isinstance(runs, dict):
            raise Refused("validation", "landing receipt reviews must map item keys to runs")
        inputs["review_runs"][pr] = {_text("item", key, required=True, cap=256):
                                     _text("review run", run, required=True, cap=256) for key, run in runs.items()}
    return inputs


def _included_parts(value, parts):
    if not isinstance(value, list):
        raise Refused("validation", "included landing PRs must be a list")
    included = sorted(_land_number(pr) for pr in value)
    allowed = {part["pr"] for part in parts}
    if len(included) != len(set(included)) or not allowed.issuperset(included):
        raise Refused("validation", "included landing PRs must be unique frozen parts")
    return included


def _timing(row):
    start, end = row.get("started_at_ms"), row.get("ended_at_ms")
    if type(start) is not int or type(end) is not int or not 0 <= start <= end < 2**63:
        raise Refused("validation", "landing timestamps must be ordered Unix milliseconds")
    return start, end


def _host_receipts(value, merge):
    if not isinstance(value, list) or len(value) != 3:
        raise Refused("validation", "host sync requires three verification receipts")
    verified = {}
    for row in value:
        if not isinstance(row, dict) or any(row.get(field) != merge for field in ("head", "checkout", "hooks", "build")):
            raise Refused("validation", "host receipt does not cover the verified merged head")
        host = _text("host", row.get("host"), required=True, cap=64)
        if host in verified or type(row.get("synced")) is not bool:
            raise Refused("validation", "duplicate or incomplete host verification")
        verified[host] = {"host": host, "head": merge, "checkout": merge,
                          "hooks": merge, "build": merge, "synced": row["synced"]}
        writers = row.get("writers")  # the host's writer-stop report; absent when it keeps no inventory
        if writers is not None:
            if (not isinstance(writers, list) or len(writers) > 100
                    or any(not isinstance(w, dict) or w.get("status") not in ("stopped", "kept") for w in writers)):
                raise Refused("validation", "host writer report is malformed")
            verified[host]["writers"] = [{"status": w["status"], "run_id": str(w.get("run_id") or "")[:128]}
                                         for w in writers]
    return list(verified.values())


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
        if action in ("stage", "ci_timings", "timeline", "stage_day"):
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
                previous = self.conn.execute(select, identity).fetchone()
                previous_data = _landing_data(previous) if previous else None
                if previous_data is None or previous_data["parts"] != parts:
                    self._verify_train_reviews(parts)
                batch = _text("batch_key", body.get("batch_key", f"train-{identity[1]}"), required=True, cap=256)
                mode = body.get("mode", "train-pr-merge")
                if mode not in LANDING_MODES:
                    raise Refused("validation", "invalid landing mode")
                manifest = {"parts": parts, "batch_key": batch}
                if "mode" in body:
                    manifest["mode"] = mode
                if "receipt_inputs" in body:
                    manifest["receipt_inputs"] = _receipt_inputs(body["receipt_inputs"], parts)
                frozen = (_land_sha(body.get("base")), _land_sha(body.get("head")),
                          json.dumps(manifest, separators=(",", ":")))
                self.conn.execute("INSERT INTO landing_trains(repo,pr,base,head,manifest) VALUES(?,?,?,?,?)"
                                  " ON CONFLICT (repo,pr) DO UPDATE SET"
                                  " base=excluded.base, head=excluded.head, manifest=excluded.manifest"
                                  " WHERE landing_trains.merged IS NULL AND landing_trains.abandoned=0",
                                  (*identity, *frozen))
                row = self.conn.execute(select, identity).fetchone()
                data = _landing_data(row)
                if ((row["base"], row["head"], data["parts"], data["batch_key"], data.get("mode", "train-pr-merge"),
                     data.get("receipt_inputs")) != (*frozen[:2], parts, batch, mode, manifest.get("receipt_inputs")) or row["abandoned"]):
                    raise Refused("conflict", "landing manifest differs from prepared train")
                if previous_data is not None and (previous_data["head"] != data["head"]
                                                    or previous_data["parts"] != data["parts"]):
                    self._reconcile_train_claims(principal, repo, f"train #{identity[1]} changed", now)
                return data
            row = self.conn.execute(select, identity).fetchone()
            if row is None:
                raise Refused("not_found", "landing train is not prepared")
            if action == "abandon" and row["merged"] is None:
                self.conn.execute("UPDATE landing_trains SET abandoned=1 WHERE repo=? AND pr=? AND abandoned=0",
                                  identity)
                self._reconcile_train_claims(principal, repo, f"train #{identity[1]} abandoned", now)
                return _landing_data(self.conn.execute(select, identity).fetchone())
            if action == "check_dependencies":
                if row["abandoned"] or row["merged"] is not None or body.get("head", row["head"]) != row["head"]:
                    raise Refused("conflict", "dependency check requires the current open train head")
                self._check_train_dependencies(repo, _landing_data(row))
                return {}
            merge = _land_sha(body.get("merge"))
            if row["abandoned"] or row["merged"] not in (None, merge):
                raise Refused("conflict", "verified merge differs from the landing journal")
            manifest = _saved_manifest(row)
            if action == "landed":
                if manifest.get("mode") == "tested-ff" and merge != row["head"]:
                    raise Refused("conflict", "tested-ff must land the exact prepared head")
                first_outcomes = "included" in body and "included" not in manifest
                if "included" in body:
                    included = _included_parts(body["included"], manifest["parts"])
                    if manifest.get("included", included) != included:
                        raise Refused("conflict", "part outcomes differ from the landing journal")
                    manifest["included"] = included
                if "mode" in manifest and "included" not in manifest:
                    raise Refused("conflict", "landing requires checked part outcomes")
                if row["merged"] is None or first_outcomes:
                    # Settlement records the verified host outcome even if an edge is unmet.
                    try:
                        self._check_train_dependencies(repo, _landing_data(row), manifest.get("included"))
                    except Refused as exc:
                        if exc.code != "conflict":
                            raise
                        manifest["dependency_warning"] = exc.msg
                    else:
                        manifest.pop("dependency_warning", None)
                if "receipt_inputs" in manifest and "landed_at" not in manifest:
                    manifest["landed_at"] = datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="seconds")
                if row["merged"] is None or "included" in body:
                    self.conn.execute("UPDATE landing_trains SET merged=?,manifest=? WHERE repo=? AND pr=?",
                                      (merge, json.dumps(manifest, separators=(",", ":")), *identity))
                    self._reconcile_train_claims(principal, repo, f"train #{identity[1]} dropped a part", now)
            elif action == "advance" and row["merged"] == merge:
                if "included" not in manifest:
                    raise Refused("conflict", "landing requires checked part outcomes")
                step = body.get("step")
                if step not in _LAND_STEPS or _LAND_STEPS.index(step) > row["cursor"]:
                    raise Refused("conflict", "landing cursor is out of order")
                if _LAND_STEPS.index(step) == row["cursor"]:
                    if step == "host_sync":
                        manifest["host_verification"] = _host_receipts(body.get("verification"), merge)
                        self.conn.execute("UPDATE landing_trains SET cursor=cursor+1,manifest=? WHERE repo=? AND pr=?",
                                          (json.dumps(manifest, separators=(",", ":")), *identity))
                    else:
                        self.conn.execute("UPDATE landing_trains SET cursor=cursor+1 WHERE repo=? AND pr=?", identity)
            else:
                raise Refused("conflict", "invalid landing journal transition")
            return _landing_data(self.conn.execute(select, identity).fetchone())

    def _verify_train_reviews(self, parts):
        adapter = self._adapter("spawner_records")
        for part in parts:
            for key, run in part.get("review_runs", {}).items():
                try:
                    require_train_review(adapter, key, part["head"], run)
                except (ValueError, OSError):
                    raise Refused("evidence_unverified",
                                  f"{key}: no explicit ACCEPT for part #{part['pr']}") from None

    def _landing_timing(self, principal, repo, action, body):
        roles = ("router", "system", "spawner") if action == "stage" else ("router", "system")
        self._require_role(principal, *roles)
        if action == "stage_day":
            start = body.get("start")
            if type(start) is not int or not 0 <= start < 2**63 - 86_400_000 or start % 86_400_000:
                raise Refused("validation", "stage day needs a UTC Unix midnight in milliseconds")
            rows = self.conn.execute(
                "SELECT * FROM landing_stages WHERE repo=? AND ended_at_ms>=? AND ended_at_ms<?"
                " ORDER BY ended_at_ms,batch_key,stage,attempt", (repo, start, start + 86_400_000))
            return {"stages": [dict(row) for row in rows]}
        batch = _text("batch_key", body.get("batch_key"), required=True, cap=256)
        if action == "timeline":
            stages = self.conn.execute(_LIST_STAGE_SQL, (repo, batch))
            jobs = self.conn.execute(
                "SELECT *, ended_at_ms-started_at_ms AS duration_ms FROM landing_ci_jobs WHERE repo=? AND batch_key=? "
                "ORDER BY run_id,attempt,started_at_ms,job_id", (repo, batch))
            trains = self.conn.execute("SELECT * FROM landing_trains WHERE repo=? ORDER BY pr DESC", (repo,))
            train = next((data for row in trains if (data := _landing_data(row))["batch_key"] == batch), None)
            accepts = []
            rows = [dict(row) for row in stages]
            for part in (train or {}).get("parts", []):
                member = [dict(row) for row in self.conn.execute(
                    "SELECT * FROM landing_stages WHERE repo=? AND head=? ORDER BY started_at_ms,stage,attempt",
                    (repo, part["head"])) if row["batch_key"] in part["items"]]
                accepted = max((row["ended_at_ms"] for row in member if row["stage"] == "review"), default=None)
                accepts.append(accepted)
                rows.extend(row for row in member if row["batch_key"] != batch)
            return {"stages": sorted(rows, key=lambda row: (row["started_at_ms"], row["stage"], row["attempt"])),
                    "jobs": [dict(row) for row in jobs], "train": train, "accepts": accepts}
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
