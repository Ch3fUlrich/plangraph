"""What a merged item shipped: the merge heads of its work and the deploy proof it still owes.

Split from ``store_waiver`` (the D-5504/D-5506 merged GO close reads both): ``_merged_heads``
names the merges that introduced an item's Plan-Item commits; ``_deploy_pending`` lists the
``host:unit`` pairs the landed changes of those PRs ship without a verified ``deploy`` proof
(LE-MERGED-UNIT-NEVER-DEPLOYED).
"""
from __future__ import annotations

from . import context_sources
from .evidence import deploy_hosts, deploy_missing, deploy_targets
from .evidence_types import valid_commit_ref


class DeployMixin:
    def _merged_heads(self, item, all_work=False):
        """Heads of the merges that introduced the item's Plan-Item commits."""
        git, project = self._adapter("git"), self._merge_project(item)
        try:
            commits = context_sources.plan_item_commits(
                git, project, item["key"], fetch=False,
                **({"limit": None} if all_work else {}))
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
                repo, ref = git.repo_path(project), git.repo_ref(project)
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

    def _deploy_pending(self, item):
        """``host:unit`` pairs a merged item ships but has no verified ``deploy`` proof for.

        An item whose merged commits touch ``deploy/systemd`` or add a host binary
        is not done until every host it names reports the unit active.
        """
        git, project = self._adapter("git"), self._merge_project(item)
        try:
            commits = context_sources.plan_item_commits(
                git, project, item["key"], fetch=False, limit=None)
            repo, ref = git.repo_path(project), git.repo_ref(project)
            # The landed changes of each Plan-Item commit's PR, not only the tagged commits.
            changes = {c for commit in commits or () if valid_commit_ref(commit.get("sha"))
                       for c in git.deploy_changes(repo, commit["sha"], ref)}
        except Exception:  # noqa: BLE001 - an unreadable diff never blocks the close
            return []
        targets = deploy_targets(changes)
        if not targets:
            return []
        return deploy_missing(targets, deploy_hosts(item), self._evidence(item["key"]))

    def _deploy_pending_result(self, item, pending, now):
        """Leave the item open: record the reason as an alert and report it."""
        msg = "deploy_pending: " + ", ".join(pending)
        with self._write():
            if self._has_finding(item["key"], "deploy_pending"):
                self._alerts_raise_finding(item["key"], "deploy_pending", msg, now)
            else:
                self._finding(item["key"], "deploy_pending", msg, now)
        return {"key": item["key"], "state": item["state"], "closed": False,
                "reason": "deploy_pending", "missing": pending}
