"""D-228/X7b decision-close verb for :class:`plangraph.keeper.store.Store`.

A ``review_tier='none'`` item is one the operator has decided never to review:
its only required evidence is a ``decision`` row.  The router closes it
directly from ``claimed``/``in_review`` to ``done`` on two pieces of proof:

* at least one attached ``decision`` evidence, every one of which verifies
  against the configured decisions file; and
* a ``batch_ref`` of the form ``<path>@<sha256 hex>`` naming a file the keeper
  can open, that resolves inside one of the configured ``batch_roots``
  directories and whose bytes hash to exactly that digest.

If any of those decisions carries a ``Closes: <project:ID>`` token then *key*
must be one of the named items — the same whole-token rule as ``Waives:``
(X6c3); with no ``Closes:`` token anywhere, the hashed batch file alone
authorises the close.  Everything is fail-closed: a traversal or a symlink that
lands outside ``batch_roots``, an unreadable file, a digest mismatch or an
absent decisions file all refuse.

As in ``waive_second_family`` and ``add_evidence_router``, the slow I/O
(filesystem, git, decisions file) runs *before* ``BEGIN IMMEDIATE``; inside the
write lock the row is re-read and its state, epoch and decision-evidence id set
must match what was verified outside it.  Since X7-FIX-B the batch file digest,
the decisions-file checks and ``review_tier=='none'`` are re-verified *under*
the write lock too, closing the window between the pre-lock check and the
commit.  The ``closed_on_decision`` event then
locks the key exactly the way ``review_passed`` does, and is attributed to the
router principal that wrote it.
"""
from __future__ import annotations

import hashlib
import os
import re

from .errors import Refused

#: ``<path>@<64 lowercase hex>``; the path may itself contain ``@``.
_BATCH_REF_RE = re.compile(r"\A(?P<path>.+)@(?P<sha>[0-9a-f]{64})\Z",
                           re.DOTALL)
_CHUNK = 1 << 16


def _sha_matches(tok: str, evidence_sha: str) -> bool:
    """True when tok and evidence_sha match (case-insensitive prefix, both >= 7 chars)."""
    if not isinstance(tok, str) or not isinstance(evidence_sha, str):
        return False
    t, e = tok.strip().lower(), evidence_sha.strip().lower()
    if len(t) < 7 or len(e) < 7:
        return False
    return t.startswith(e) or e.startswith(t)


class CloseMixin:
    """The router-only ``close_on_decision`` verb."""

    def close_on_decision(self, principal, key, batch_ref):
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

    def _decision_evidence(self, key, item, now):
        """Verify every attached ``decision`` row pre-lock; return the rows.

        Returns ``(rows, named_keys)`` where ``named_keys`` is the union of the
        whole ``Closes:`` tokens across those rows (empty when none carries one).
        """
        rows = [e for e in self._evidence(key)
                if e["epoch"] == item["epoch"] and e["kind"] == "decision"]
        if not rows:
            raise Refused("evidence_missing",
                          f"{key} has no decision evidence attached")
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
        return rows, named

    def _closes_token_sha(self, key, rows, item=None, now=None):
        """The commit sha from this key's Closes token in the decision rows, or None."""
        if item is None:
            item = self._item(key)
        if item is None:
            return None
        if now is None:
            now = self.clock()
        decisions = self._decisions_adapter(item, now)
        if not hasattr(decisions, "closes_with_commits"):
            return None
        for row in rows:
            decision_id = (row.get("ref") or {}).get("id")
            if not decision_id:
                continue
            try:
                cwc = decisions.closes_with_commits(decision_id)
            except Exception:  # noqa: BLE001
                cwc = {}
            for raw_key, sha in cwc.items():
                if self.resolve_key(raw_key) == key and sha is not None:
                    return sha
        return None

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
        target_ref = (ctx.git.repo_ref(item.get("project"))
                      if ctx.git is not None and hasattr(ctx.git, "repo_ref")
                      else "main")
        verified_any = False
        verified_shas = []
        last_sha = None
        for row in commit_rows:
            ref = row.get("ref") or {}
            sha = ref.get("sha") or ref.get("commit")
            if sha:
                last_sha = sha
            result = self._verify_evidence(item, "commit", ref, now)
            if result.ok:
                verified_any = True
                if sha:
                    verified_shas.append(sha)
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
        if item.get("review_tier") != "none":
            raise Refused("bad_state",
                          f"{key} is review_tier={item.get('review_tier')},"
                          " only 'none' closes on a decision")
        rows, named = self._decision_evidence(key, item, now)
        tok = (self._closes_token_sha(key, rows, item, now)
               if "commit" in (item.get("evidence_kinds") or [])
               else None)
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
            again = frozenset(e["id"] for e in self._evidence(key)
                              if e["epoch"] == fresh["epoch"]
                              and e["kind"] == "decision")
            if again != seen:
                raise Refused("conflict",
                              "the decision evidence changed under the transaction")
            # X7-FIX-B: the proof above was gathered before the write lock;
            # re-run every file-backed check inside it (small files, a
            # router-only verb) so nothing can change between the two.
            sha = self._read_batch(batch_ref)
            again_rows, again_named = self._decision_evidence(key, fresh, now)
            if again_named != named:
                raise Refused("evidence_unverified",
                              "the Closes: set changed under the transaction")
            if fresh.get("review_tier") != "none":
                raise Refused("conflict",
                              f"{key} changed review_tier under the transaction")
            if set(fresh.get("evidence_kinds") or []) != set(item.get("evidence_kinds") or []):
                raise Refused("conflict",
                              f"{key} changed evidence_kinds under the transaction")
            if "commit" in (fresh.get("evidence_kinds") or []):
                again_commits = frozenset(e["id"] for e in self._evidence(key)
                                          if e["epoch"] == fresh["epoch"]
                                          and e["kind"] == "commit")
                if again_commits != seen_commits:
                    raise Refused("conflict",
                                  "the commit evidence changed under the transaction")
                again_tok = self._closes_token_sha(key, again_rows, fresh, now)
                tok_norm = tok.lower() if tok else None
                again_tok_norm = again_tok.lower() if again_tok else None
                if again_tok_norm != tok_norm:
                    raise Refused("evidence_unverified",
                                  "the Closes: token sha changed under the transaction")
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
