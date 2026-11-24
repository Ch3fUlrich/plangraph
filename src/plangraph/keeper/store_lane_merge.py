"""Merge-close helpers: one name per registered repository, and the decision row behind a pre-PR lane merge."""
from __future__ import annotations

import os
import re

from .errors import Refused
from .evidence_types import valid_commit_ref


class LaneMergeMixin:
    def _one_name_per_repository(self, names):
        """*names* with each repository (resolved path and ref) kept once, under its first name.

        keeper.json can register one directory under several names; the commits of
        such a directory are one candidate, not an ambiguity.
        """
        git, seen, kept = self._adapter("git"), set(), []
        for name in names:
            path = git.repo_path(name) if callable(getattr(git, "repo_path", None)) else None
            ref = git.repo_ref(name) if callable(getattr(git, "repo_ref", None)) else "main"
            ident = (os.path.realpath(path), ref) if path else (name, None)
            if ident not in seen:
                seen.add(ident)
                kept.append(name)
        return kept

    def _lane_merge_decision(self, item, merge, now):
        """The D-id of a pre-PR lane merge: named in its subject, and its row names this merge sha and the item.

        ``None`` when the subject names no decision (the merge has no proof at all);
        a refusal in the ``lane_merge_unproven`` class when a named row does not vouch for it.
        """
        sha, subject = merge.get("sha"), str(merge.get("subject") or "")
        ids = list(dict.fromkeys(re.findall(r"\((D-[0-9]+)(?![\w-])", subject)))
        if not ids or not valid_commit_ref(sha):
            return None
        from .store_waiver import _names_item as names_item
        decisions = self._ctx(item, now).decisions
        for did in ids:
            try:
                text = decisions.text(did) if decisions.has(did) else None
            except Exception:  # noqa: BLE001 - an unreadable row never vouches
                text = None
            if (text and re.search(rf"(?<![0-9a-fA-F]){re.escape(sha)}(?![0-9a-fA-F])", text, re.I)
                    and names_item(text, item["key"]) and self._verify_evidence(
                        item, "decision", {"id": did}, now).ok):
                return did
        raise Refused("evidence_unverified", f"lane_merge_unproven: {sha[:12]} {', '.join(ids)}")
