"""Follow the CI-green head of main on the live host, then audit the page.

The live host runs ``python3 -m plangraph.live_follow follow`` before each
render and ``... check`` after it. ``follow`` fast-forwards the live checkout
to ``origin/<branch>`` only when GitHub CI has verified that head green; every
doubt keeps the tree, and nothing here ever raises into the render unit —
a broken gate must not blank a live page. ``check`` compares the published
page's build stamp with the newest green sha and emits one alert line per
stale green head once the grace window has passed.

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
import subprocess
import sys
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
    "record_green",
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
    workflow: str = "tests"
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
        grace = env.get("PLANGRAPH_ALERT_GRACE_S")
        return cls(
            tree=required("PLANGRAPH_LIVE_TREE"),
            repo=required("PLANGRAPH_REPO"),
            token_file=required("PLANGRAPH_GH_TOKEN_FILE"),
            out=out,
            state=env.get("PLANGRAPH_STATE") or os.path.join(out, ".live-follow.json"),
            workflow=env.get("PLANGRAPH_CI_WORKFLOW") or "tests",
            alert_cmd=shlex.split(alert) if alert else None,
            grace_s=int(grace) if grace else 1800,
        )


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


def _head_green(runner, cfg: Config, head_sha: str) -> bool:
    """True when the newest run of cfg.workflow on head_sha completed success."""
    runs = gh_api(
        runner, cfg, f"repos/{cfg.repo}/actions/runs?head_sha={head_sha}&per_page=30"
    )
    if runs is None:
        return False
    entries = [
        r
        for r in runs.get("workflow_runs") or []
        if isinstance(r, dict) and r.get("name") == cfg.workflow
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
        heads = [sha]
    else:
        heads = [
            (p.get("head") or {}).get("sha") or sha
            for p in items
            if isinstance(p, dict)
            and (
                p.get("merge_commit_sha") == sha
                or (p.get("base") or {}).get("ref") == cfg.branch
            )
        ]
    return any(_head_green(runner, cfg, h) for h in heads)


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
    rc, _ = runner.run(git + ["merge-base", "--is-ancestor", cur, head])
    if rc != 0:
        return _result("kept", "not_fast_forward", head)
    if not is_green(runner, cfg, head):
        return _result("kept", "not_green", head)
    # Merge the verified sha, never the moving ref: re-resolving origin/main
    # here could fast-forward onto a head CI was never asked about.
    rc, _ = runner.run(git + ["merge", "--ff-only", head])
    if rc != 0:
        return _result("kept", "ff_failed", head)
    record_green(cfg, head, now)
    return _result("moved", "ok", head)


def load_state(cfg: Config) -> dict:
    """The state doc, or {} when the file is missing or corrupt."""
    try:
        data = json.loads(Path(cfg.state).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


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
        return {"status": "ok", "page": page, "green": green}
    try:
        green_since = float(state.get("green_since"))
    except (TypeError, ValueError):
        green_since = moment
    if moment - green_since > cfg.grace_s and state.get("alerted") != green:
        minutes = int((moment - green_since) / 60)
        line = (
            f"plangraph page stale: page={page or 'none'} "
            f"main-green={green[:10]} for {minutes} min"
        )
        if cfg.alert_cmd:
            # The alert line is the handler's last argv element, not stdin.
            runner.run(list(cfg.alert_cmd) + [line])
        else:
            print(line, file=sys.stderr)
        state["alerted"] = green
        _save_state(cfg, state)
        return {"status": "alerted", "page": page, "green": green}
    return {"status": "stale", "page": page, "green": green}


def main(argv=None, env=None, runner=None, now=time.time) -> int:
    """CLI: ``follow`` before the render, ``check`` after it.

    Returns 0 for every follow/check outcome — a page render must never be
    blocked by this step; only a unusable config exits 2.
    """
    words = list(sys.argv[1:] if argv is None else argv)
    command = words[0] if words else ""
    if command not in ("follow", "check"):
        print("live_follow: usage: follow|check", file=sys.stderr)
        return 2
    try:
        cfg = Config.from_env(env or os.environ)
    except ConfigError as exc:
        print(f"live_follow: {exc}", file=sys.stderr)
        return 2
    active = runner if runner is not None else Runner()
    result = (
        follow(cfg, active, now) if command == "follow" else check(cfg, active, now)
    )
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
