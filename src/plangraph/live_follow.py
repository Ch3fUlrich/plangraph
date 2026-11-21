"""Follow the CI-green head of main on the live host, then audit the page.

The live host runs ``python3 -m plangraph.live_follow follow`` before each
render, ``... export`` as the render's gate (the checkout must be the recorded
green head; the render imports an exported copy of that commit) and ``... check``
after it. ``follow`` fast-forwards the live checkout
to ``origin/<branch>`` only when GitHub CI has verified that head green; every
doubt keeps the tree, and nothing here ever raises into the render unit —
a broken gate must not blank a live page. ``check`` compares the published
page's build stamp with the newest green sha and emits one alert line per
green head once the grace window has passed, re-arming when the page catches
up so a later regression can alert again.

The token is only ever handed to :meth:`Runner.run` via ``env``; it never
enters argv (world-readable in ``ps``), output, or the state file.

Public API
----------
``follow(cfg, runner, now) -> dict`` / ``check(cfg, runner, now) -> dict``
    The two steps; each returns a small result dict.
``main(argv=None, env=None, runner=None, now=time.time) -> int``
    CLI entry point, one JSON line on stdout.
``Config`` / ``ConfigError`` / ``Runner``
    Configuration, its parse error, and the injectable command runner.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

__all__ = [
    "Config",
    "ConfigError",
    "Runner",
    "check",
    "follow",
    "gh_api",
    "is_green",
    "load_state",
    "main",
    "page_stamp",
    "read_token",
    "export_tree",
    "record_green",
    "verified",
]


class ConfigError(ValueError):
    """A required PLANGRAPH_* variable is unset; the message names it."""


@dataclass(frozen=True)
class Config:
    """Everything live_follow needs, one field per env var (see Config.from_env)."""

    tree: str
    repo: str
    token_file: str
    out: str
    state: str
    workflow_path: str = ".github/workflows/tests.yml"
    remote: str = "origin"
    branch: str = "main"
    alert_cmd: list[str] | None = None
    grace_s: int = 1800

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "Config":
        def required(name: str) -> str:
            value = env.get(name)
            if not value:
                raise ConfigError(f"{name} is not set")
            return value

        out = required("PLANGRAPH_OUT")
        alert = env.get("PLANGRAPH_ALERT_CMD")
        alert_cmd = None
        if alert:
            try:
                alert_cmd = shlex.split(alert)
            except ValueError:
                raise ConfigError(
                    "PLANGRAPH_ALERT_CMD is not a valid command line "
                    "(unbalanced quote?)"
                ) from None
        grace = env.get("PLANGRAPH_ALERT_GRACE_S")
        grace_s = 1800
        if grace:
            try:
                grace_s = int(grace)
            except ValueError:
                raise ConfigError(
                    f"PLANGRAPH_ALERT_GRACE_S is not an integer: {grace!r}"
                ) from None
            if grace_s <= 0:
                raise ConfigError(
                    f"PLANGRAPH_ALERT_GRACE_S must be > 0, got {grace_s}"
                )
        return cls(
            tree=required("PLANGRAPH_LIVE_TREE"),
            repo=required("PLANGRAPH_REPO"),
            token_file=required("PLANGRAPH_GH_TOKEN_FILE"),
            out=out,
            state=_state_path(env, out),
            workflow_path=(
                env.get("PLANGRAPH_CI_WORKFLOW_PATH")
                or ".github/workflows/tests.yml"
            ),
            alert_cmd=alert_cmd,
            grace_s=grace_s,
        )


def _state_path(env: Mapping[str, str], out: str) -> str:
    """The gate state file: PLANGRAPH_STATE, else a private per-user default.

    The output directory is web-served; gate state in it (or reachable through a
    symlink or ``..``) would be published, so such a path is a ConfigError.
    """
    state = env.get("PLANGRAPH_STATE")
    if not state:
        base = env.get("XDG_STATE_HOME")
        home = env.get("HOME")
        if not base and home:
            base = os.path.join(home, ".local", "state")
        if not base:
            raise ConfigError("PLANGRAPH_STATE is not set and no state directory is known")
        state = os.path.join(base, "plangraph", "live-follow.json")
    real_out = os.path.realpath(out)
    # The atomic save replaces the final entry itself, so a symlink at that entry
    # is judged where it sits (resolved parent + name) as well as where it points.
    entry = os.path.join(os.path.realpath(os.path.dirname(os.path.abspath(state))), os.path.basename(state))
    if any(os.path.commonpath([p, real_out]) == real_out for p in (os.path.realpath(state), entry)):
        raise ConfigError("PLANGRAPH_STATE must be outside PLANGRAPH_OUT (the web-served directory)")
    return state


class Runner:
    """Runs one external command, injectable so tests fake every effect.

    stderr is deliberately dropped: error text from git/gh can echo context
    we do not want forwarded, and no decision here depends on it.
    """

    def run(
        self,
        argv: list[str],
        env: dict | None = None,
        cwd: str | None = None,
    ) -> tuple[int, str]:
        full_env = dict(os.environ)
        if env:
            full_env.update(env)
        try:
            proc = subprocess.run(
                list(argv),
                capture_output=True,
                text=True,
                timeout=60,
                cwd=cwd,
                env=full_env,
            )
        except FileNotFoundError:
            return (127, "")
        except subprocess.TimeoutExpired:
            return (124, "")
        except OSError:
            return (127, "")
        return (proc.returncode, proc.stdout or "")


def read_token(path: str) -> str | None:
    """GH_TOKEN (else GITHUB_TOKEN) from a KEY=VALUE file; None if absent/empty."""
    values: dict[str, str] = {}
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        values[key.strip()] = value
    for key in ("GH_TOKEN", "GITHUB_TOKEN"):
        if values.get(key):
            return values[key]
    return None


def gh_api(runner, cfg: Config, endpoint: str) -> dict | None:
    """Parsed JSON from `gh api endpoint`; a list becomes {"items": [...]}.

    None on any failure (no token, rc != 0, empty/invalid output) so callers
    treat doubt as "not green". The token goes only through ``env``.
    """
    token = read_token(cfg.token_file)
    if not token:
        return None
    rc, out = runner.run(["gh", "api", endpoint], env={"GH_TOKEN": token})
    if rc != 0 or not out.strip():
        return None
    try:
        data = json.loads(out)
    except ValueError:
        return None
    if isinstance(data, dict):
        return data
    if isinstance(data, list):
        return {"items": data}
    return None


def _workflow_file(path: object) -> str | None:
    """The workflow file a run came from, minus any "@refs/..." suffix.

    The API reports a PR run as ".github/workflows/tests.yml@refs/pull/7/merge";
    the ref part is not part of the file's identity. None when the field is
    absent or empty — a run we cannot identify is a run that does not count.
    """
    if not isinstance(path, str) or not path:
        return None
    return path.split("@", 1)[0] or None


def _counts_toward_gate(run: dict, cfg: Config, head_sha: str) -> bool:
    """Whether this run is the pinned workflow of exactly this head in our repo.

    A workflow *name* is not an identity: any other file, or a fork's copy of
    the same workflow, can be named "tests". Pinning the file path and checking
    the run's own head_sha and head_repository keeps a lookalike — including a
    fork PR's run — from deciding the result.
    """
    if run.get("head_sha") != head_sha:
        return False
    head_repo = run.get("head_repository")
    if not isinstance(head_repo, dict) or head_repo.get("full_name") != cfg.repo:
        return False
    return _workflow_file(run.get("path")) == cfg.workflow_path


def _bound_to_pr(run: dict, cfg: Config, number: object) -> bool:
    """Whether the run belongs to pull request ``number`` into the target branch.

    The workflow picks its suites from the PR's target and draft status, so a
    run of another PR (or of the same head into another branch) proves nothing
    about this merge. A run with no usable PR list does not count.
    """
    if not isinstance(number, int) or isinstance(number, bool):
        return False
    for entry in run.get("pull_requests") or []:
        if not isinstance(entry, dict) or entry.get("number") != number:
            continue
        if (entry.get("base") or {}).get("ref") == cfg.branch:
            return True
    return False


def _head_green(
    runner, cfg: Config, head_sha: str, pr_number: object = None, bound: bool = False
) -> bool:
    """True when the newest counting run of cfg.workflow_path on head_sha succeeded.

    ``bound`` restricts the runs to those of pull request ``pr_number`` into the
    target branch; a commit without a PR (``bound`` false) uses any run of the head.
    """
    runs = gh_api(
        runner, cfg, f"repos/{cfg.repo}/actions/runs?head_sha={head_sha}&per_page=30"
    )
    if runs is None:
        return False
    entries = [
        r
        for r in runs.get("workflow_runs") or []
        if isinstance(r, dict)
        and _counts_toward_gate(r, cfg, head_sha)
        and (not bound or _bound_to_pr(r, cfg, pr_number))
    ]
    if not entries:
        return False
    # created_at is an ISO string: lexical order == chronological order.
    newest = max(entries, key=lambda r: str(r.get("created_at") or ""))
    return newest.get("status") == "completed" and newest.get("conclusion") == "success"


def is_green(runner, cfg: Config, sha: str) -> bool:
    """Whether CI has verified sha (fail closed: any API doubt -> False)."""
    prs = gh_api(runner, cfg, f"repos/{cfg.repo}/commits/{sha}/pulls")
    if prs is None:
        return False
    items = prs.get("items") or []
    if not items:
        # A commit pushed without a PR must carry its own green run.
        return _head_green(runner, cfg, sha)
    dicts = [p for p in items if isinstance(p, dict)]
    exact = [p for p in dicts if p.get("merge_commit_sha") == sha]
    if exact:
        candidates = exact
    else:
        # Rebase merges rewrite the sha, so a closed PR into the target
        # branch with merged_at set still identifies this commit; anything
        # else (open PRs, PRs that merely contain the commit) must not.
        candidates = [
            p
            for p in dicts
            if (p.get("base") or {}).get("ref") == cfg.branch
            and p.get("state") == "closed"
            and p.get("merged_at")
        ]
    if not candidates:
        return False
    heads = [(p.get("head") or {}).get("sha") for p in candidates]
    if any(not h for h in heads):
        # A candidate without a head sha cannot be verified: fail closed
        # rather than falling back to the commit sha.
        return False
    return all(
        _head_green(runner, cfg, h, p.get("number"), bound=True)
        for h, p in zip(heads, candidates)
    )


def _result(action: str, reason: str, sha: str | None = None) -> dict:
    return {"action": action, "reason": reason, "sha": sha}


def _now_value(now) -> float:
    return float(now() if callable(now) else now)


def follow(cfg: Config, runner, now=time.time) -> dict:
    """Fast-forward the tree to the green head, or keep it; never raises."""
    try:
        return _follow(cfg, runner, now)
    except Exception:
        return _result("kept", "error")


def _follow(cfg: Config, runner, now) -> dict:
    if read_token(cfg.token_file) is None:
        return _result("kept", "no_token")
    git = ["git", "-C", cfg.tree]
    rc, _ = runner.run(git + ["fetch", cfg.remote, cfg.branch])
    if rc != 0:
        return _result("kept", "fetch_failed")
    rc, head_out = runner.run(git + ["rev-parse", f"{cfg.remote}/{cfg.branch}"])
    if rc != 0:
        return _result("kept", "git_failed")
    rc, cur_out = runner.run(git + ["rev-parse", "HEAD"])
    if rc != 0:
        return _result("kept", "git_failed")
    head, cur = head_out.strip(), cur_out.strip()
    if not head or not cur:
        return _result("kept", "git_failed")
    if head == cur:
        if is_green(runner, cfg, head):
            record_green(cfg, head, now)
            return _result("kept", "up_to_date", head)
        # Not green: leave the state file alone so the staleness clock keeps running.
        return _result("kept", "up_to_date_not_green", head)
    if not is_green(runner, cfg, head):
        return _result("kept", "not_green", head)
    # Record the verified head before touching the tree: a head CI proved
    # green must reach the page audit even when the merge below is refused.
    record_green(cfg, head, now)
    rc, _ = runner.run(git + ["merge-base", "--is-ancestor", cur, head])
    if rc != 0:
        return _result("kept", "not_fast_forward", head)
    # Merge the verified sha, never the moving ref: re-resolving origin/main
    # here could fast-forward onto a head CI was never asked about.
    rc, _ = runner.run(git + ["merge", "--ff-only", head])
    if rc != 0:
        return _result("kept", "ff_failed", head)
    return _result("moved", "ok", head)


def load_state(cfg: Config) -> dict:
    """The state doc with invalid fields dropped, or {} when absent/corrupt.

    A hand-edited or half-written file must never raise into follow/check:
    green_sha must be a non-empty string (without one there is no state at
    all), green_since a real number (bool excluded), alerted a string or None.
    """
    try:
        data = json.loads(Path(cfg.state).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    green = data.get("green_sha")
    if not isinstance(green, str) or not green:
        return {}
    state = {"green_sha": green}
    since = data.get("green_since")
    if isinstance(since, (int, float)) and not isinstance(since, bool):
        state["green_since"] = since
    alerted = data.get("alerted")
    if alerted is None or isinstance(alerted, str):
        state["alerted"] = alerted
    return state


def _save_state(cfg: Config, state: dict) -> None:
    # Temp file in the same directory + os.replace: readers never see a half-written file.
    path = Path(cfg.state)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle)
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def record_green(cfg: Config, sha: str, now=time.time) -> None:
    """Note sha as the newest green head.

    green_since/alerted survive repeats of the same sha: they track how long
    this head has been green and whether it already alerted, not when we
    last looked.
    """
    moment = _now_value(now)
    state = load_state(cfg)
    if state.get("green_sha") == sha:
        state = {
            "green_sha": sha,
            "green_since": state.get("green_since", moment),
            "alerted": state.get("alerted"),
        }
    else:
        state = {"green_sha": sha, "green_since": moment, "alerted": None}
    _save_state(cfg, state)


def verified(cfg: Config, runner) -> dict:
    """Whether the live checkout is exactly the head the gate recorded as green.

    The render publishes the checkout, so it may only run from a revision CI
    verified: a first start, a hand-moved checkout or a refused fast-forward
    leave HEAD different from the record, and the previous page stays up.
    """
    rc, out = runner.run(["git", "-C", cfg.tree, "rev-parse", "HEAD"])
    head = out.strip() if rc == 0 else ""
    green = load_state(cfg).get("green_sha")
    if head and head == green:
        return {"status": "verified", "sha": head}
    return {"status": "unverified", "sha": head or None}


def _extract_tar(archive: Path, dest: Path) -> None:
    """Extract ``archive`` into ``dest``; a member that would leave ``dest`` raises."""
    with tarfile.open(archive) as tar:
        root = dest.resolve()
        for member in tar.getmembers():
            target = (dest / member.name).resolve()
            if target != root and root not in target.parents:
                raise ValueError("archive member outside the destination")
            if member.issym() or member.islnk():
                raise ValueError("archive link")
        tar.extractall(dest, filter="data") if hasattr(tarfile, "data_filter") else tar.extractall(dest)


def export_tree(cfg: Config, runner) -> dict:
    """Export the verified commit to a private directory the render imports from.

    The live checkout may carry local edits or untracked files; the render must
    only see the commit the gate recorded as green. Returns ``{"status":
    "verified", "sha", "tree"}``; any doubt leaves the previous export in place.
    """
    gate = verified(cfg, runner)
    if gate["status"] != "verified":
        return gate
    sha = gate["sha"]
    base = Path(cfg.state).parent
    final = base / "render-tree"
    staging = None
    try:
        base.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(dir=str(base), prefix="render-tree.tmp-"))
        archive = staging / "tree.tar"
        rc, _ = runner.run(["git", "-C", cfg.tree, "archive", "--format=tar", "-o", str(archive), sha])
        if rc != 0:
            return {"status": "unverified", "sha": sha}
        content = staging / "content"
        content.mkdir()
        _extract_tar(archive, content)
        old = staging / "previous"
        if final.exists():
            os.replace(final, old)
        os.replace(content, final)
        return {"status": "verified", "sha": sha, "tree": str(final)}
    except (OSError, ValueError, tarfile.TarError):
        return {"status": "unverified", "sha": sha}
    finally:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)


def page_stamp(path: str) -> str | None:
    """The content of the page's <meta name="plangraph-build"> tag, else None."""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for tag in re.findall(r"<meta\b[^>]*>", text, re.I):
        if not re.search(r"""\bname\s*=\s*["']?plangraph-build["']?""", tag, re.I):
            continue
        content = re.search(r"""\bcontent\s*=\s*(["'])(.*?)\1""", tag, re.I)
        if content:
            return content.group(2)
    return None


def check(cfg: Config, runner, now=time.time) -> dict:
    """Compare the published page stamp with the newest green sha; alert once."""
    moment = _now_value(now)
    page = page_stamp(os.path.join(cfg.out, "plan-cdn.html"))
    state = load_state(cfg)
    green = state.get("green_sha")
    if not green:
        return {"status": "no_green_known", "page": page, "green": None}
    if page and green.startswith(page):
        if state.get("alerted") is not None:
            # The page caught up: re-arm so a later regression can alert again.
            state["alerted"] = None
            _save_state(cfg, state)
        return {"status": "ok", "page": page, "green": green}
    try:
        green_since = float(state.get("green_since"))
        if green_since > moment:
            # A clock skew (or hand-edited file) must not suspend alerts.
            green_since = moment
            state["green_since"] = green_since
            _save_state(cfg, state)
    except (TypeError, ValueError):
        # Corrupt or missing since: the grace clock starts from this tick.
        green_since = moment
        state["green_since"] = green_since
        _save_state(cfg, state)
    if moment - green_since > cfg.grace_s and state.get("alerted") != green:
        minutes = int((moment - green_since) / 60)
        line = (
            f"plangraph page stale: page={page or 'none'} "
            f"main-green={green[:10]} for {minutes} min"
        )
        if cfg.alert_cmd:
            # The alert line is the handler's last argv element, not stdin.
            rc, _ = runner.run(list(cfg.alert_cmd) + [line])
            if rc != 0:
                # The alert did not land: echo it and retry on the next tick.
                print(line, file=sys.stderr)
                return {"status": "stale", "page": page, "green": green}
        else:
            print(line, file=sys.stderr)
        state["alerted"] = green
        _save_state(cfg, state)
        return {"status": "alerted", "page": page, "green": green}
    return {"status": "stale", "page": page, "green": green}


def main(argv=None, env=None, runner=None, now=time.time) -> int:
    """CLI: ``follow`` before the render, ``check`` after it.

    Returns 0 for every follow/check outcome — a page render must never be
    blocked by this step; only a unusable config exits 2. ``verified`` is the
    render gate: 0 when the checkout is the recorded green head, else 3;
    ``export`` also copies that commit to a private tree and prints its path.
    """
    words = list(sys.argv[1:] if argv is None else argv)
    command = words[0] if words else ""
    if command not in ("follow", "check", "verified", "export"):
        print("live_follow: usage: follow|check|verified|export", file=sys.stderr)
        return 2
    try:
        cfg = Config.from_env(env or os.environ)
    except ConfigError as exc:
        print(f"live_follow: {exc}", file=sys.stderr)
        return 2
    active = runner if runner is not None else Runner()
    if command in ("verified", "export"):
        try:
            result = (verified if command == "verified" else export_tree)(cfg, active)
        except Exception:
            result = {"status": "unverified", "sha": None}
        if result["status"] != "verified":
            print(json.dumps(result), file=sys.stderr if command == "export" else sys.stdout)
            return 3
        # export: "<tree> <short sha>" for the render unit to read; verified: JSON.
        print(f"{result['tree']} {result['sha'][:10]}" if command == "export" else json.dumps(result))
        return 0
    try:
        result = (
            follow(cfg, active, now) if command == "follow" else check(cfg, active, now)
        )
        print(json.dumps(result))
    except Exception:
        # Never a traceback into the render unit, and never exception text
        # (it can echo paths or the token): one neutral JSON line, exit 0.
        if command == "follow":
            print(json.dumps({"action": "kept", "reason": "error"}))
        else:
            print(json.dumps({"status": "error"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
