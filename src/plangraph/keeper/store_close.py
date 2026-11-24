"""D-228/X7b decision-close verb for :class:`plangraph.keeper.store.Store`.

A ``review_tier='none'`` item is one the operator has decided never to review:
its only required evidence is a ``decision`` row.  The router closes it
directly from ``claimed``/``in_review`` to ``done`` on two pieces of proof:

* at least one attached verified ``decision`` evidence, each re-verified
  against the configured decisions file; and
* a ``batch_ref`` of the form ``<path>@<sha256 hex>`` naming a file the keeper
  can open, that resolves inside one of the configured ``batch_roots``
  directories and whose bytes hash to exactly that digest.

KEEPER-OPTASK-CLOSE (D-2201/D-2206) widens the verb for ``operator_task``:
that kind is never agent-reviewed, so the router may close it from
``claimed``/``in_review`` at *any* review tier, including ``standard`` and
``high``.  Above ``none`` the hashed batch file is not proof enough — the
decision must name the key in a whole ``Closes: <project:ID>`` token.  Every
other kind at a non-``none`` tier is still refused ``bad_state``, and the
kind/tier pair is re-checked under the write lock.

If any of those decisions carries a ``Closes: <project:ID>`` token then *key*
must be one of the named items — the same whole-token rule as ``Waives:``
(X6c3); at tier ``none``, with no ``Closes:`` token anywhere, the hashed batch
file alone authorises the close.  Everything is fail-closed: a traversal or a
symlink that lands outside ``batch_roots``, an unreadable file, a digest
mismatch or an absent decisions file all refuse.

As in ``waive_second_family`` and ``add_evidence_router``, the slow I/O
(filesystem, git, decisions file) runs *before* ``BEGIN IMMEDIATE``; inside the
write lock the row is re-read and its state, epoch and decision-evidence id set
must match what was verified outside it.  Since X7-FIX-B the batch file digest,
the decisions-file checks and the kind/tier pair are re-verified *under*
the write lock too, closing the window between the pre-lock check and the
commit.  The ``closed_on_decision`` event then
locks the key exactly the way ``review_passed`` does, and is attributed to the
router principal that wrote it.
"""
from __future__ import annotations

import hashlib
import os
import re

from . import context_sources, timing
from .errors import Refused
from .evidence import deploy_host_names, deploy_hosts, deploy_missing, deploy_targets
from .evidence_repo import repo_key
from .evidence_types import valid_commit_ref

#: ``<path>@<64 lowercase hex>``; the path may itself contain ``@``.
_BATCH_REF_RE = re.compile(r"\A(?P<path>.+)@(?P<sha>[0-9a-f]{64})\Z",
                           re.DOTALL)
_CHUNK = 1 << 16


def _sha_matches(tok: str, full_sha: str) -> bool:
    """True when tok is a case-insensitive prefix of the full 40-hex commit id (token len 7..40)."""
    if not isinstance(tok, str) or not isinstance(full_sha, str):
        return False
    t, f = tok.strip().lower(), full_sha.strip().lower()
    if len(t) < 7 or len(t) > 40 or len(f) != 40:
        return False
    return f.startswith(t)


class CloseMixin:
    """The router-only ``close_on_decision`` verb."""

    def close_on_decision(self, principal, key, batch_ref=None, *, decision_ref=None,
                          epoch=None):
        if decision_ref is not None:
            from .store_decision_close import close_decision
            return self._run(principal, "close_on_decision", key,
                             lambda now: close_decision(
                                 self, principal, key, decision_ref, epoch, batch_ref, now))
        return self._run(principal, "close_on_decision", key,
                         lambda now: self._close_on_decision(principal, key,
                                                             batch_ref, now))

    # ------------------------------------------------------------- batch file
    def _batch_root_dirs(self):
        """The configured ``batch_roots``, resolved.

        A blank entry is dropped rather than resolved: ``realpath("")`` is the
        process cwd, which would silently authorise whatever the keeper happens
        to be standing in.
        """
        return [os.path.realpath(str(root)) for root in (self.batch_roots or [])
                if str(root).strip()]

    @staticmethod
    def _inside(root, path):
        if path == root:
            return True
        relative = os.path.relpath(path, root)
        return (relative and not os.path.isabs(relative)
                and relative != os.pardir
                and not relative.startswith(os.pardir + os.sep))

    def _read_batch(self, batch_ref):
        """Resolve, confine and hash the batch file; return its ``sha256``.

        Every failure here refuses rather than defaulting to "close enough":
        the digest is the proof that the operator closed the batch they named.
        """
        if not isinstance(batch_ref, str) or not batch_ref.strip():
            raise Refused("validation", "batch_ref must be '<path>@<sha256 hex>'")
        match = _BATCH_REF_RE.match(batch_ref)
        if match is None:
            raise Refused("validation",
                          "batch_ref must be '<path>@<sha256 hex>'")
        raw_path, want = match.group("path"), match.group("sha")
        roots = self._batch_root_dirs()
        if not roots:
            raise Refused("forbidden",
                          "no batch_roots are configured, so nothing may be"
                          " closed on a batch file")
        resolved = os.path.realpath(os.path.abspath(os.path.expanduser(raw_path)))
        if not any(self._inside(root, resolved) for root in roots):
            raise Refused("forbidden", "batch file resolves outside batch_roots")
        digest = hashlib.sha256()
        try:
            with open(resolved, "rb") as handle:
                for chunk in iter(lambda: handle.read(_CHUNK), b""):
                    digest.update(chunk)
        except OSError as exc:
            reason = exc.strerror or type(exc).__name__
            raise Refused("evidence_unverified",
                          f"batch file could not be read ({reason})")
        got = digest.hexdigest()
        if got != want:
            raise Refused("evidence_unverified",
                          f"batch file sha256 is {got}, batch_ref says {want}")
        return got

    # ------------------------------------------------------------------ verb
    def _decisions_adapter(self, item, now):
        decisions = self._ctx(item, now).decisions
        if decisions is None or not callable(getattr(decisions, "has", None)) \
                or not callable(getattr(decisions, "closes", None)):
            raise Refused("evidence_unverified",
                          "no decisions adapter is configured")
        return decisions

    def _decision_evidence(self, key, item, now, require_named=False):
        """Re-verify attached verified ``decision`` rows; return the rows.

        Returns ``(rows, named_keys)`` where ``named_keys`` is the union of the
        whole ``Closes:`` tokens across those rows (empty when none carries one).
        ``require_named`` (set for a non-``none`` tier) makes a decision with no
        ``Closes:`` token at all a refusal: the hashed batch file cannot stand
        in for the operator naming the key.
        """
        rows = [e for e in self._evidence(key)
                if e["epoch"] == item["epoch"] and e["kind"] == "decision"
                and e["verified"]]
        if not rows:
            raise Refused("evidence_missing",
                          f"{key} has no verified decision evidence attached")
        decisions = self._decisions_adapter(item, now)
        named = set()
        for row in rows:
            decision_id = (row.get("ref") or {}).get("id")
            if not decision_id:
                raise Refused("evidence_unverified",
                              f"decision evidence {row['id']} names no id")
            result = self._verify_evidence(item, "decision", row["ref"], now)
            if not result.ok:
                raise Refused("evidence_unverified",
                              result.detail or f"decision {decision_id} unverified")
            try:
                recorded = decisions.has(decision_id)
            except Exception:  # noqa: BLE001 - unreadable file fails closed
                recorded = False
            if not recorded:
                raise Refused("evidence_unverified",
                              f"decision {decision_id} is not in the decisions file")
            closes = set(decisions.closes(decision_id))
            # L5: a ``Closes:`` keyword with no valid project:ID token is a
            # malformed close and fails closed (never batch_ref-only).
            has_keyword = getattr(decisions, "has_closes_keyword", None)
            if not closes and callable(has_keyword) and has_keyword(decision_id):
                raise Refused("evidence_unverified",
                              f"decision {decision_id} has a Closes: keyword "
                              "but no valid project:ID token")
            named |= {self.resolve_key(token) for token in closes}
        if named and key not in named:
            raise Refused("evidence_unverified",
                          f"the decision closes {sorted(named)}, not {key}")
        if require_named and not named:
            raise Refused("evidence_unverified",
                          f"the decision does not close {key} by name")
        return rows, named

    def _closes_token_sha(self, key, rows, item=None, now=None):
        """The commit sha from this key's Closes token in the decision rows, or None.

        SUPERSEDE RULE (router-approved): attached decision rows are processed
        in D-number order; when rows name the same key with different,
        non-prefix-compatible shas, the highest D-number wins (its sha replaces
        earlier ones, never raising for cross-row differences).
        A conflict inside one row stays invalid exactly as today: the
        DecisionsAdapter.closes_with_commits drops such a key, so it never
        reaches the merge. Prefix-compatible shas keep today's behaviour.
        """
        if item is None:
            item = self._item(key)
        if item is None:
            return None
        if now is None:
            now = self.clock()
        decisions = self._decisions_adapter(item, now)
        if not hasattr(decisions, "closes_with_commits"):
            return None

        def _dnum(row):
            try:
                did = (row.get("ref") or {}).get("id")
            except Exception:  # noqa: BLE001 - malformed row sorts first
                return 0
            if isinstance(did, str):
                text = did.strip().upper()
                if text.startswith("D-"):
                    tail = text[2:]
                    if tail.isdigit():
                        return int(tail)
            return 0

        resolved_sha = None
        for row in sorted(rows, key=_dnum):
            decision_id = (row.get("ref") or {}).get("id")
            if not decision_id:
                continue
            try:
                cwc = decisions.closes_with_commits(decision_id)
            except Exception:  # noqa: BLE001
                cwc = {}
            for raw_key, sha in cwc.items():
                if self.resolve_key(raw_key) == key and sha is not None:
                    if resolved_sha is None:
                        resolved_sha = sha
                    else:
                        r_l = resolved_sha.lower()
                        s_l = sha.lower()
                        if r_l.startswith(s_l):
                            pass
                        elif s_l.startswith(r_l):
                            resolved_sha = sha
                        else:
                            # Cross-row mismatch: later (higher D-number) wins.
                            resolved_sha = sha
        return resolved_sha

    def _check_commit_evidence(self, key, item, now, tok=None):
        """For tier 'none' items with 'commit' in evidence_kinds: require at least
        one verified commit evidence row whose sha is an ancestor of configured ref,
        and matches tok if tok is not None.
        """
        commit_rows = [e for e in self._evidence(key)
                       if e["epoch"] == item["epoch"] and e["kind"] == "commit"]
        if not commit_rows:
            raise Refused("evidence_unverified", "no commit evidence")
        ctx = self._ctx(item, now)
        project = item.get("project")
        verified_any = False
        verified_shas = []
        last_sha = None
        last_rkey = project
        for row in commit_rows:
            ref = row.get("ref") or {}
            sha = ref.get("sha") or ref.get("commit")
            rkey = project
            if sha:
                last_sha = sha
                # A fallback commit lives in the track repo: resolve it (and
                # name its ref in the refusal) through the same track-keyed
                # rule the verifiers use, never through the primary repo alone.
                rkey = repo_key(item, ctx, sha)
                last_rkey = rkey
            result = self._verify_evidence(item, "commit", ref, now)
            if result.ok:
                verified_any = True
                if sha:
                    full_sha = None
                    try:
                        repo = (ctx.git.repo_path(rkey)
                                if ctx.git is not None and hasattr(ctx.git, "repo_path")
                                else None)
                    except Exception:
                        repo = None
                    if ctx.git is not None and hasattr(ctx.git, "resolve_commit") and repo:
                        try:
                            full_sha = ctx.git.resolve_commit(repo, sha)
                        except Exception:
                            full_sha = None
                    if full_sha:
                        verified_shas.append(full_sha)
        # ``repo_ref`` is required of every adapter (no invented "main"); a
        # missing adapter keeps the None check and stays a closed refusal.
        target_ref = (ctx.git.repo_ref(last_rkey)
                      if ctx.git is not None else "main")
        if not verified_any:
            sha_label = f" {last_sha}" if last_sha else ""
            raise Refused("evidence_unverified",
                          f"commit{sha_label} not merged into {target_ref}")
        if tok is not None:
            if not any(_sha_matches(tok, s) for s in verified_shas):
                shas = sorted(set(verified_shas))
                raise Refused("evidence_unverified",
                              f"Closes token {key}@{tok} does not match"
                              f" the verified commit evidence {shas}")
        return commit_rows

    def _close_on_decision(self, principal, key, batch_ref, now):
        self._require_role(principal, "router")
        item = self._get_or_refuse(key)
        self._assert_unlocked(item)
        if item["state"] not in ("claimed", "in_review"):
            # An open item has never been worked; it must be moved into an
            # active state first (``set_legacy_state``, a later task).
            raise Refused("bad_state", f"{key} is {item['state']}, not active")
        tier = item.get("review_tier")
        if tier != "none" and item.get("kind") != "operator_task":
            raise Refused("bad_state",
                          f"{key} is review_tier={tier},"
                          " only 'none' closes on a decision")
        # An operator_task above ``none`` is never agent-reviewed, so the router
        # may close it -- but only when the decision names this key; the batch
        # file alone is enough at ``none`` only.
        require_named = tier != "none"
        rows, named = self._decision_evidence(key, item, now,
                                              require_named=require_named)
        tok = self._closes_token_sha(key, rows, item, now)
        commit_rows = (self._check_commit_evidence(key, item, now, tok)
                       if "commit" in (item.get("evidence_kinds") or [])
                       else [])
        sha = self._read_batch(batch_ref)
        decision_ids = sorted({e["ref"]["id"] for e in rows})
        seen = frozenset(e["id"] for e in rows)
        seen_commits = frozenset(e["id"] for e in commit_rows)
        with self._write():
            fresh = self._get_or_refuse(key)
            self._assert_unlocked(fresh)
            if fresh["state"] not in ("claimed", "in_review"):
                raise Refused("conflict", f"{key} is {fresh['state']}")
            if int(fresh["epoch"]) != int(item["epoch"]):
                raise Refused("conflict",
                              f"{key} changed epoch under the transaction")
            if fresh.get("review_tier") != tier:
                raise Refused("conflict",
                              f"{key} changed review_tier under the transaction")
            if fresh.get("kind") != item.get("kind"):
                raise Refused("conflict",
                              f"{key} changed kind under the transaction")
            again = frozenset(e["id"] for e in self._evidence(key)
                              if e["epoch"] == fresh["epoch"]
                              and e["kind"] == "decision" and e["verified"])
            if again != seen:
                raise Refused("conflict",
                              "the decision evidence changed under the transaction")
            # X7-FIX-B: the proof above was gathered before the write lock;
            # re-run every file-backed check inside it (small files, a
            # router-only verb) so nothing can change between the two.
            sha = self._read_batch(batch_ref)
            again_rows, again_named = self._decision_evidence(
                key, fresh, now, require_named=require_named)
            if again_named != named:
                raise Refused("evidence_unverified",
                              "the Closes: set changed under the transaction")
            if set(fresh.get("evidence_kinds") or []) != set(item.get("evidence_kinds") or []):
                raise Refused("conflict",
                              f"{key} changed evidence_kinds under the transaction")
            again_tok = self._closes_token_sha(key, again_rows, fresh, now)
            tok_norm = tok.lower() if tok else None
            again_tok_norm = again_tok.lower() if again_tok else None
            if again_tok_norm != tok_norm:
                raise Refused("evidence_unverified",
                              "the Closes: token sha changed under the transaction")
            if "commit" in (fresh.get("evidence_kinds") or []):
                again_commits = frozenset(e["id"] for e in self._evidence(key)
                                          if e["epoch"] == fresh["epoch"]
                                          and e["kind"] == "commit")
                if again_commits != seen_commits:
                    raise Refused("conflict",
                                  "the commit evidence changed under the transaction")
                self._check_commit_evidence(key, fresh, now, again_tok)
            # The cache row goes first, exactly like ``_pass``: the event below
            # is what locks the key, and the triggers refuse a locked insert.
            cursor = self.conn.execute(
                "UPDATE items SET state='done', reviewer=?, updated_at=?"
                " WHERE key=? AND state IN ('claimed','in_review') AND epoch=?",
                (principal.name, self._iso(now), key, item["epoch"]))
            self._race_guard(cursor, key, fresh["state"], item["epoch"])
            self._append(principal, key, "closed_on_decision",
                         {"decision_ids": decision_ids,
                          "batch_ref": batch_ref, "sha256": sha}, now)
            return {"key": key, "state": "done",
                    "decision_ids": decision_ids, "sha256": sha}

    # --- what a merged item shipped (LE-MERGED-UNIT-NEVER-DEPLOYED), read by the merged GO close
    def _merged_heads(self, item, all_work=False):
        """Heads of the merges that introduced the item's Plan-Item commits."""
        git, project = self._adapter("git"), self._merge_project(item)
        head = item.get("_close_head")
        options = {"head": head} if head is not None else {}
        try:
            commits = context_sources.plan_item_commits(
                git, project, item["key"], fetch=False,
                **({"limit": None} if all_work else {}), **options)
        except Exception:  # noqa: BLE001 - an unreadable git proof never authorizes
            commits = None
        heads = set()
        # A trailer-bearing commit may be inside a multi-commit PR, or a later
        # direct follow-up. Bind to the introducing merge's second parent only.
        for commit in commits or ():
            sha = commit.get("sha")
            if not valid_commit_ref(sha):
                continue
            try:
                repo, ref = git.repo_path(project), head or git.repo_ref(project)
                merge = git.merge_commit_of(repo, sha, ref)
                if not merge or not valid_commit_ref(merge[0]):
                    continue
                proc = git._run(repo, ["show", "-s", "--format=%P",
                                       "--end-of-options", merge[0]])
                parents = proc.stdout.strip().split() if proc.returncode == 0 else []
                if len(parents) == 2 and all(valid_commit_ref(p) for p in parents):
                    heads.add(parents[1])
            except Exception:  # noqa: BLE001 - an unknown merge never authorizes
                continue
        return heads

    def _with_merge_repo(self, item):
        """The item tagged with the one scanned repository that holds its Plan-Item commits.

        A hint (``Merge-Repo:``) or a hit in the item's own repository decides at once;
        otherwise exactly one other scanned repository must hold them, else the refusal names all.
        """
        if item.get("_merge_project") or self._merge_repository(item) != (item["project"], None):
            return item
        git, found = self._adapter("git"), []
        for project in self._scan_projects(item):
            try:
                pinned = self._pin_close(dict(item, _merge_project=project))
                commits = context_sources.plan_item_commits(
                    git, project, item["key"], fetch=False, head=pinned["_close_head"])
            except Exception:  # noqa: BLE001 - an unreadable repository names nothing
                commits = None
            if commits:
                found.append(pinned)
                if project == item["project"]:
                    return pinned
        if len(found) > 1:
            names = ", ".join(candidate["_merge_project"] for candidate in found)
            raise Refused("evidence_unverified", f"merge_repo_ambiguous: {names} (name one with Merge-Repo:)")
        return found[0] if found else item

    def _require_inferred_repo_scope(self, item, decision_id, now):
        """A GO from another project needs an explicit Merge-Repo selection."""
        if any(context_sources.MERGE_REPO_LINE.fullmatch(line) for line in self._merge_text(item).splitlines()):
            return
        rows = self._ctx(item, now).decisions._rows_strict().get(decision_id, ())
        if len(rows) == 1 and rows[0].split("|")[2].strip() != item["project"]:
            raise Refused("evidence_unverified", "merged_on_default_missing")

    def _pin_close(self, item):
        """Capture the default commit after any refresh, before reading close evidence."""
        git, project = self._adapter("git"), self._merge_project(item)
        try:
            head = git.ref_commit(git.repo_path(project), git.repo_ref(project))
        except Exception:
            raise Refused("evidence_unverified", "deploy_scan_unreadable") from None
        return dict(item, _close_head=head)

    def _deploy_changes(self, item):
        """Tagged commits and landed diffs at the same default commit as the merge proofs."""
        git, project = self._adapter("git"), self._merge_project(item)
        repo, head = git.repo_path(project), item["_close_head"]
        commits = context_sources.plan_item_commits(
            git, project, item["key"], fetch=False, limit=None, head=head)
        if commits is None:
            raise RuntimeError("commit history unreadable")
        tagged = frozenset(commit["sha"] for commit in commits)
        named = self._named_merge_proof(item)
        landed = tagged | {named["landed"]} if named else tagged
        changes = {change for sha in landed for change in git.deploy_changes(repo, sha, head, item["key"])}
        per_unit = (self._part_deploy_hosts(git, repo, landed, head, item["key"])
                    if deploy_targets(changes) and not deploy_hosts(item) and hasattr(git, "deploy_part_changes")
                    else {})
        return (head, tagged, per_unit), changes

    @staticmethod
    def _part_deploy_hosts(git, repo, landed, head, key):
        def declarations(commit):
            values = [value for name, value in git.trailers(repo, commit).items() if name.lower() == "deploy"]
            return deploy_host_names(value for group in values for value in ([group] if isinstance(group, str) else group))

        parts = {}
        for sha in landed:
            for part, merge, paths in git.deploy_part_changes(repo, sha, head, key):
                parts[merge] = (part, paths)
        per_unit = {}
        for merge, (part, paths) in parts.items():
            units = deploy_targets(paths)
            if units:
                hosts = declarations(merge) or declarations(part)
                for unit in units:
                    per_unit.setdefault(unit, set()).update(hosts)
        return per_unit

    def _merged_go_deploy_changes(self, item, head):
        git, project = self._adapter("git"), self._merge_project(item)
        try:
            return set(git.deploy_changes(git.repo_path(project), head, item["_close_head"], item["key"]))
        except Exception:  # noqa: BLE001 - an unreadable diff cannot clear the deploy gate
            raise Refused("evidence_unverified", "deploy_scan_unreadable") from None

    def _deploy_pending(self, item, changes, token):
        """``host:unit`` pairs a merged item ships but has no verified ``deploy`` proof for."""
        targets = deploy_targets(changes)
        if not targets:
            return []
        hosts, evidence = deploy_hosts(item), self._evidence(item["key"])
        per_unit = token[2] if token is not None else {}
        if hosts or not per_unit:
            return deploy_missing(targets, hosts, evidence)
        return sorted({pair for unit in targets
                       for pair in deploy_missing([unit], sorted(per_unit.get(unit, ())), evidence)})

    @staticmethod
    def _merge_text(item):
        return f"{item.get('description') or ''}\n{item.get('note') or ''}"

    def _snapshot_deploy(self, key, project, commits, previous):
        """Reuse an unchanged delivery's immutable diffs across default-tip advances."""
        item = self.__dict__.get("_deploy_inputs", {}).get(key)
        if item is None:
            return
        text = self._merge_text(item)
        index = self.__dict__["_warmed"][project]
        try:
            hinted, revision = self._merge_repository(item)
            if (hinted != item["project"] or revision is not None) and hinted != project:
                return
            tagged = dict(item, _merge_project=project)
            head = index.get("target")
            if head is None:
                head = self._pin_close(tagged)["_close_head"]
            if (revision is None and previous and previous["changes"] is not None
                    and previous["text"] == text and previous["commits"] == commits):
                token, changes = (head, *previous["token"][1:]), previous["changes"]
            else:
                token, changes = self._deploy_changes(dict(tagged, _close_head=head))
        except Exception:  # noqa: BLE001 - recorded as unreadable
            token, changes = None, None
        self.__dict__["_deploy_warm"][project, key] = {
            "text": text, "token": token, "commits": commits, "changes": changes}

    def _deploy_scan_current(self, item, token):
        """Keep a scan across first-parent extensions that add no work for this item."""
        git, project = self._adapter("git"), self._merge_project(item)
        pinned, tagged, _ = token
        try:
            repo, ref = git.repo_path(project), git.repo_ref(project)
            if self.__dict__.get("_frozen_lanes"):
                index = self.__dict__.get("_warmed", {}).get(project) or {}
                return git.ref_commit(repo, index.get("ref", ref)) == pinned
            current = git.ref_commit(repo, ref)
            if current != pinned:
                # A second-parent ancestor can have different introducing merges.
                if not git._on_first_parent_chain(repo, pinned, current):
                    return False
                commits = context_sources.plan_item_commits(
                    git, project, item["key"], fetch=False, limit=None, head=current)
                if commits is None or frozenset(c["sha"] for c in commits) != tagged:
                    return False
            return git.ref_commit(repo, ref) == current
        except Exception:  # noqa: BLE001 - an unreadable ref cannot confirm a scan
            return False

    def _auto_deploy_scan(self, item):
        """Use the outside-lock deploy snapshot for a frozen automatic close."""
        project = self._merge_project(item)
        if self.__dict__.get("_frozen_lanes"):
            hit = self.__dict__.get("_deploy_warm", {}).get((project, item["key"]))
            if hit is None or hit["text"] != self._merge_text(item):
                raise Refused("evidence_unverified", "deploy_scan_not_warm")
            if hit["changes"] is None:
                raise Refused("evidence_unverified", "deploy_scan_unreadable")
            if not self._deploy_scan_current(item, hit["token"]):
                raise Refused("evidence_unverified", "deploy_scan_not_warm")
            return hit["token"], hit["changes"]
        if not self._warm(project) and not self._refresh_merged_default_ref(self._adapter("git"), project):
            raise Refused("evidence_unverified", "merged_default_ref_refresh_failed")
        _, token, changes = self._read_current_deploy_scan(item)
        return token, changes

    def _manual_deploy_scan(self, item, *, complete_history, refresh_alternate=False):
        """Refresh the default when required before pinning the manual close's scan."""
        git = self._adapter("git")
        project, revision = self._merge_repository(item)
        refresh_alternate = refresh_alternate and project != item["project"]
        can_pin = callable(getattr(git, "ref_commit", None))
        if refresh_alternate or (can_pin and (revision or (complete_history and not self._warm(project)))):
            if not self._refresh_merged_default_ref(git, project):
                detail = (f"merge_repo_default_refresh_failed: {project}" if revision or refresh_alternate
                          else "merged_default_ref_refresh_failed")
                raise Refused("evidence_unverified", detail)
            item.pop("_close_head", None)
        if not can_pin:
            return item, None, ()
        return self._read_current_deploy_scan(item)

    def _read_current_deploy_scan(self, item):
        """Retry an incompatible ref change before taking the database write lock."""
        for attempt in range(2):
            pinned = self._pin_close(item) if attempt or "_close_head" not in item else item
            try:
                token, changes = self._deploy_changes(pinned)
            except Refused:
                raise
            except Exception:
                raise Refused("evidence_unverified", "deploy_scan_unreadable") from None
            if self._deploy_scan_current(pinned, token):
                return pinned, token, changes
        raise Refused("conflict", "merged_on_default_changed")

    def _deploy_pending_result(self, item, pending, now, *, record=True):
        """Leave the item open: record the reason as an alert and report it."""
        msg = "deploy_pending: " + ", ".join(pending)
        if record:
            with self._write():
                if self._has_finding(item["key"], "deploy_pending"):
                    self._alerts_raise_finding(item["key"], "deploy_pending", msg, now)
                else:
                    self._finding(item["key"], "deploy_pending", msg, now)
        return {"key": item["key"], "state": item["state"], "closed": False,
                "reason": "deploy_pending", "missing": pending}

    # --- the automatic merge close (MERGE-CLOSES-ITEMS-NO-AGENT-CALL) ---
    # Reads the default ref once per repository, takes every unfinished item a
    # merge named by ``Plan-Item`` and closes it by the same proofs as the router
    # verb minus the GO row: the merge itself is the authority. Partial deliveries
    # are annotated on their cards; every refused close is returned in ``listed``.
    AUTO_STATES = ("open", "claimed", "in_review")

    def close_merged_auto(self, principal, project=None, require_seats=None, apply=True, repos_only=False,
                          frozen=False, limit=None, after=None, items=None):
        return self._run(principal, "close_merged_auto", None,
                         lambda now: self._close_merged_auto(
                             principal, project, require_seats, apply, now, repos_only, frozen, limit, after, items),
                         success_message=None)

    def _close_merged_auto(self, principal, project, require_seats, apply, now, repos_only, frozen, limit, after,
                           items=None):
        # ``frozen`` (the API, which holds the keeper lock): read only the warm scan, ask git and the forge nothing.
        self.__dict__["_frozen_lanes"] = bool(frozen)
        try:
            return self._close_merged_auto_run(principal, project, require_seats, apply, now, repos_only, limit, after,
                                               items)
        finally:
            self.__dict__["_frozen_lanes"] = False

    def _close_merged_auto_run(self, principal, project, require_seats, apply, now, repos_only, limit, after,
                               items=None):
        self._require_role(principal, "system", "router")
        if any(type(value) is not bool for value in (apply, repos_only)):
            raise Refused("validation", "apply and repos_only must be booleans")
        require_seats = self._seats_required(require_seats)
        if project is not None and not isinstance(project, str):
            raise Refused("validation", "project must be a string")
        git = self._adapter("git")
        scan = getattr(git, "scan_projects", None)
        repos = list(scan()) if callable(scan) else []
        if repos_only:
            return {"repos": repos}
        if limit is not None and (type(limit) is not int or not 1 <= limit <= 100):
            raise Refused("validation", "limit must be an integer from 1 to 100")
        if after is not None and not isinstance(after, str):
            raise Refused("validation", "after must be an item key")
        indexes, closed, listed, pending, parts = {}, [], [], [], []
        deployments = []
        frozen = self.__dict__.get("_frozen_lanes")
        scan_errors = dict(self.__dict__.get("_merge_scan_errors", {})) if frozen else {}

        def index_of(repo_project):
            if repo_project not in indexes:
                if frozen:
                    if not self._warm(repo_project):
                        mismatch = repo_project in self.__dict__.get("_warmed", {})
                        reason = scan_errors.setdefault(
                            repo_project, scan_errors.get("*", "merge_index_tip_changed" if mismatch
                                                         else "merge_index_not_warm"))
                        raise Refused("evidence_unverified", f"{reason}: {repo_project}")
                    index = self.__dict__["_warmed"][repo_project]
                else:
                    index = context_sources.work_index(git, repo_project, fresh=not self._warm(repo_project))
                if index is not None and index.get("origin_stale"):
                    raise Refused("evidence_unverified", f"merge_index_stale: {repo_project}: {index.get('reason')}")
                indexes[repo_project] = index
            return indexes[repo_project]

        if items is not None and (not isinstance(items, list) or not 1 <= len(items) <= 100
                                  or not all(isinstance(key, str) and key for key in items)):
            raise Refused("validation", "items must be a list of 1 to 100 item keys")
        candidates = self._auto_merge_candidates(project, after, limit, items)
        next_after = None
        for processed, item in enumerate(candidates):
            if processed and (processed == limit or timing.io_timeout(1) < 1):
                next_after = last_key
                break
            last_key = item["key"]
            key = item["key"]
            try:
                hinted, revision = self._merge_repository(item)
                hinted = None if (hinted, revision) == (item["project"], None) else hinted
                if hinted and callable(scan) and hinted not in repos:
                    raise Refused("evidence_unverified", f"merge_repo_not_scanned: {hinted}")
                projects = [hinted] if hinted else self._scan_projects(item)
                found = [p for p in projects if (index := index_of(p)) is not None
                         and (revision or context_sources.indexed_work(index, key)["commits"])]
                if not found:
                    delivered = [{"key": key, "project": p, "pr": part["pr"], "merge_sha": part["sha"]}
                                 for p in projects for part in context_sources.indexed_parts(indexes[p], key)]
                    if apply and delivered:
                        self._note_merged_parts(principal, item, delivered, indexes, now)
                    parts.extend(delivered)
                    continue
                if len(found) > 1:
                    raise Refused("evidence_unverified",
                                  f"merge_repo_ambiguous: {', '.join(found)} (name one with Merge-Repo:)")
                tagged = item if found[0] == item["project"] else dict(item, _merge_project=found[0])
                result = self._auto_close_one(principal, tagged, require_seats, apply, now)
            except Refused as exc:
                listed.append({"key": key, "state": item["state"], "reason": f"{exc.code}: {exc.msg}"})
                continue
            if deployment := result.pop("deployment_pending", None):
                deployments.append({"key": key, **deployment})
            if result:
                (closed if apply else pending).append(result)
            else:
                listed.append({"key": key, "state": item["state"],
                               "reason": "evidence_unverified: deploy_pending: " + ", ".join(deployment["missing"])})
        report = {"closed": closed, "would_close": pending, "listed": listed, "repos": repos,
                  "merged_not_done": len(pending) + len(listed), "require_seats": require_seats,
                  "next_after": next_after, "scan_errors": scan_errors, "parts": parts,
                  "deployment_pending": deployments}
        if items is not None:
            report["settled"] = self._settled_items(items, project, {row["key"] for row in closed})
        return report

    def _note_merged_parts(self, principal, item, parts, indexes, now):
        key = item["key"]
        git = self._adapter("git")
        with self._write():
            fresh = self._get_or_refuse(key)
            self._assert_unlocked(fresh)
            if fresh["state"] not in self.AUTO_STATES or fresh["epoch"] != item["epoch"]:
                raise Refused("conflict", f"{key} changed under the transaction")
            for project in {part["project"] for part in parts}:
                index = indexes[project]
                if git.ref_commit(git.repo_path(project), index["ref"]) != index["target"]:
                    raise Refused("conflict", "merge_index_tip_changed")
            existing = fresh.get("note") or ""
            lines = [f"Merged part: {p['project']} PR #{p['pr']} at {p['merge_sha']}" for p in parts]
            added = [line for line in lines if line not in existing.splitlines()]
            if added:
                note = "\n".join([existing.rstrip(), *added]).lstrip("\n")
                cursor = self.conn.execute(
                    "UPDATE items SET note=?, updated_at=? WHERE key=? AND epoch=? AND state=?",
                    (note, self._iso(now), key, fresh["epoch"], fresh["state"]))
                self._race_guard(cursor, key, fresh["state"], fresh["epoch"])
                self._append(principal, key, "item_defined", {"note": note}, now)
