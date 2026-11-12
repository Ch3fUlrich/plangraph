"""D-3649: context pack guards -- secret names, redaction, fail-closed, one line convention, caps, one read (hermetic).

Every test names the invariant it pins; mutation probes (shrink DENY, drop the .git check, make tracked() fail
open, mix line conventions, read a file twice) must turn one of them red.
"""
from __future__ import annotations

import asyncio
import os
import pathlib
import re
import subprocess
from types import SimpleNamespace

import pytest

from plangraph.planner.legs import DEFAULT_LEGS
from plangraph.planner.routing import SpawnPlan
from plangraph.spawner import context_pack
from plangraph.spawner.context_pack import ContextError
from plangraph.spawner.seat_spawn import SeatSpawnMixin

from registry_route_util import CARD, _service  # noqa: F401
from test_opencode_config import load_config

INLINE = SimpleNamespace(tool_mode="inline-only", max_request_tokens=100000)


class _Seat(SeatSpawnMixin):
    def __init__(self, cwd):
        self.runner = SimpleNamespace(default_cwd=str(cwd))


def git(tree, *args):
    subprocess.run(["git", "-C", str(tree), *args], check=True, capture_output=True)


@pytest.fixture
def tree(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))   # never discover a repo above tmp_path
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    return repo


def track(tree, name, data):
    path = tree / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data if isinstance(data, bytes) else data.encode())
    git(tree, "add", name)
    return path


def pack_for(tree, *context, prompt=""):
    return context_pack.build(prompt, dict(CARD, context=list(context)), tree)


# --- I4: refused names are a superset of the repo's secret patterns, case-insensitive ------------------------


def fence_samples():
    read_denies = [p["resource"] for p in load_config()["permissions"] if p["action"] == "read" and p["effect"] == "deny"]
    assert len(read_denies) >= 8   # *.env, *.env.*, *auth.json, *api_keys*, *api-keys*, *client.key*, *manage.key*, *.claude.json*
    return sorted({s for pat in read_denies for s in (pat.replace("*", "x"), pat.replace("*", ""), pat.replace("*", "a-b"))})


@pytest.mark.parametrize("sample", fence_samples())
def test_deny_covers_every_read_fence_of_opencode_jsonc(sample):
    for name in (sample, sample.upper(), sample.title(), "dir/" + sample, "Dir/Sub/" + sample.upper()):
        assert context_pack._refusal(name, frozenset({name})) == "secret-looking path", name


SECRET_NAMES = (
    "keeper.env", "prod.env", ".env", ".ENV.local", "api_keys.yml", "API-KEYS.yaml", "apikey.txt", "auth.json", "My-Auth.JSON",
    ".netrc", "_netrc", ".npmrc", ".pypirc", ".pgpass", ".htpasswd", ".git-credentials", "token.txt", "Token.TXT",
    "access_token.json", "github-token.yml", "a.token", "pg_dsn.txt", "PG_DSN.txt", "db.dsn", "x.p12", "cert.PFX", "k.pem",
    "server.key", "id_rsa", "id_ed25519", "store.jks", "vault.kdbx", "release.keystore", "password.txt", "passwd", "client_secret.json",
    "secrets.yml", "credentials", "service-account.json", "terraform.tfvars", "prod.tfstate", "prod.tfstate.backup", "kubeconfig",
    "cookies.txt", ".claude.json", ".claude.json.bak", ".ssh/config", ".aws/config", ".gnupg/pubring.kbx", "home/.kube/config",
    "private_key.txt", "vault_pass.txt", ".git/config", ".GIT/HEAD", "sub/.git/config", "env/prod.env", "nested/dir/Auth.Json",
)


@pytest.mark.parametrize("name", SECRET_NAMES)
def test_secret_looking_names_are_refused_even_when_tracked(name):
    assert context_pack._refusal(name, frozenset({name})) == "secret-looking path"


@pytest.mark.parametrize("name", [
    "src/app.py", "README.md", "AUTHORS", "docs/authors.md", "tokenizer.py", "src/keeper/cli_token.py", "tests/test_run_tokens.py",
    "fixtures/real-ok-tool-tokens.jsonl", ".github/workflows/tests.yml", ".gitignore", ".gitattributes", "keyboard.json",
    "docs/OAUTH.md", "src/dsn_parse.py", "tools/author_list.txt",
])
def test_ordinary_names_stay_packable(name):
    assert context_pack._refusal(name, frozenset({name})) is None
    assert context_pack._refusal(name, frozenset()) == "not tracked by git"


def test_dot_git_is_refused_by_name_not_only_by_being_untracked():
    assert context_pack._refusal(".git/config", frozenset({".git/config"})) == "secret-looking path"
    assert context_pack._refusal(".git/config", frozenset()) == "secret-looking path"


def test_windows_spellings_of_a_secret_name_are_refused():
    for name in (".env.", ".env ", "token.txt::$DATA", "prod.env:stream"):
        assert context_pack._refusal(name, frozenset({name})) == "secret-looking path", name


def test_secret_files_are_never_read_or_packed(tree, monkeypatch):
    track(tree, "keeper.env", "TOP=1\n")
    track(tree, "token.txt", "tok\n")
    track(tree, "ok.py", "fine\n")
    opened = instrument(monkeypatch, tree)["opened"]
    pack = pack_for(tree, "keeper.env:1-1", "token.txt:1-1", "ok.py:1-1")
    assert "fine" in pack.text and "TOP" not in pack.text and "tok" not in pack.text
    assert opened == ["ok.py"]
    assert [w.split(" (")[0] for w in pack.warnings] == ["context_path_refused: keeper.env", "context_path_refused: token.txt"]


def test_every_slice_passes_through_redact_before_packing(tree):
    leaked = "sk" + "-" + "fake0123456789"
    track(tree, "cfg.py", f'a = 1\nSERVICE_KEY=hunter2\nheader = "Bearer abc.def.ghi"\nb = "{leaked}"\nlast = 2\n')
    pack = pack_for(tree, "cfg.py:1-5")
    assert "a = 1" in pack.text and "last = 2" in pack.text
    assert "SERVICE_KEY=hunter2" in pack.text   # source mode skips credential-name assignments only
    assert "abc.def.ghi" not in pack.text and leaked not in pack.text
    assert 'header = "Bearer [REDACTED]"' in pack.text and 'b = "[REDACTED]"' in pack.text
    assert pack.text.count("[REDACTED]") == 2


def test_redact_runs_on_the_text_that_is_packed(tree, monkeypatch):
    from plangraph.spawner import env_redact
    seen = []

    def scrub(text):
        seen.append(text)
        return text.replace("two", "2")

    monkeypatch.setattr(context_pack, "redact_source", scrub)
    monkeypatch.setattr(env_redact, "redact_source", scrub)
    track(tree, "m.py", "one\ntwo\nthree\n")
    pack = pack_for(tree, "m.py:1-3")
    assert seen == ["one\ntwo\nthree", "one\n2\nthree"]   # _source_block scrubs the first output again
    assert "--- m.py:1-3 ---\n```\none\n2\nthree\n```" in pack.text
    assert "one\ntwo\nthree" not in pack.text


def test_slice_holding_a_private_key_block_is_dropped(tree):
    header = "-----BEGIN " + "RSA PRIVATE KEY" + "-----"
    track(tree, "notes.py", f"x = 1\n{header}\nbody\n")
    track(tree, "fine.py", "y = 2\n")
    pack = pack_for(tree, "notes.py:1-3", "fine.py:1-1")
    assert "body" not in pack.text and "x = 1" not in pack.text and "y = 2" in pack.text
    assert any("notes.py" in w and "secret-looking content" in w for w in pack.warnings)


@pytest.mark.parametrize("failure", [OSError("no git"), subprocess.CalledProcessError(128, "git"), subprocess.TimeoutExpired("git", 30)])
def test_git_failure_fails_closed_with_context_error(tree, monkeypatch, failure):
    track(tree, "a.py", "a1\n")

    def broken(*args, **kwargs):
        raise failure
    monkeypatch.setattr(context_pack.subprocess, "run", broken)
    with pytest.raises(ContextError, match="tracked files"):
        context_pack.annotate({}, "p", dict(CARD, context=["a.py:1-1"]), tree)
    with pytest.raises(ContextError):
        _Seat(tree)._leg_prompt("p", dict(CARD, context=["a.py:1-1"]), "implement", INLINE)
    skipped = context_pack.warnings("see a.py", dict(CARD), tree)   # no entries: no refusal, but never a silent clean scan
    assert [w.split(":", 1)[0] for w in skipped] == ["big_file_scan_skipped"] and "tracked files" in skipped[0]
    record = {}
    context_pack.annotate(record, "see a.py", dict(CARD), tree)
    assert [e["event"] for e in record["events"]] == ["big_file_scan_skipped"]
    assert context_pack.warnings("no file named here", dict(CARD), tree) == []   # nothing named: no listing attempted


def test_directory_without_a_git_work_tree_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "a.py").write_text("a1\n")
    with pytest.raises(ContextError, match="git work tree"):
        context_pack.annotate({}, "p", dict(CARD, context=["a.py:1-1"]), plain)
    assert [w.split(":", 1)[0] for w in context_pack.warnings("see a.py and big.py", dict(CARD), plain)] == ["big_file_scan_skipped"]


def test_git_redirect_env_cannot_point_the_listing_at_another_repo(tree, tmp_path, monkeypatch):
    other = tmp_path / "other"
    other.mkdir()
    git(other, "init", "-q")
    (other / "only_there.py").write_text("x\n")
    git(other, "add", "only_there.py")
    track(tree, "here.py", "h\n")
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(other))
    assert context_pack.tracked(tree.resolve()) == frozenset({"here.py"})


def test_symlink_loops_fail_closed_not_with_a_runtime_error(tree):
    os.symlink("loop.py", tree / "loop.py")
    os.symlink("two.py", tree / "one.py")
    os.symlink("one.py", tree / "two.py")
    git(tree, "add", "loop.py", "one.py", "two.py")
    for name in ("loop.py", "one.py"):
        with pytest.raises(ContextError, match="cannot be resolved"):
            context_pack.annotate({}, "p", dict(CARD, context=[name + ":1-1"]), tree)
    assert context_pack.warnings("edit loop.py and one.py", dict(CARD), tree) == []   # a looped name in the brief is skipped


# --- I5: one line convention; a range past EOF warns ---------------------------------------------------------


def test_dangling_and_missing_context_paths_stay_not_a_file(tree):
    (tree / "dangling.py").symlink_to("missing.py")
    for name in ("dangling.py", "missing.py"):
        with pytest.raises(ContextError, match="is not a file in the run cwd"):
            pack_for(tree, name + ":1-1")


@pytest.mark.parametrize("cycle", [False, True])
def test_looped_run_cwd_has_specific_resolution_error(tmp_path, cycle):
    (tmp_path / "one").symlink_to("two" if cycle else "one")
    if cycle:
        (tmp_path / "two").symlink_to("one")
    with pytest.raises(ContextError, match=r"cannot be resolved \(symlink loop or unreadable\)"):
        context_pack.build("", {}, tmp_path / "one")

CONVENTION_INPUTS = [
    b"", b"a", b"a\n", b"a\nb", b"a\r\nb\r\n", b"a\rb\rc", b"a\r\rb\n", b"\r", b"\n\n", b"a\r\n\r\nb",
    b"a\x0cb\nc\n", b"a\x0bb\x1cc\nd", "a b\nc\n".encode(), b"a\xc2\x85b\nc", b"a\x85b\nc\n", b"x" * 70000 + b"\r\n" + b"y",
    b"x" * 200000 + b"\nz\n", b"x\n" * 70000 + b"tail", b"x" * 65536 + b"\r" + b"\ny", b"p\r" * 40000,
]


def oracle(data):
    parts = re.split(r"\r\n|\r|\n", data.decode("utf-8", errors="replace"))
    return parts[:-1] if parts[-1] == "" else parts


@pytest.mark.parametrize("data", CONVENTION_INPUTS, ids=[f"input{i}" for i in range(len(CONVENTION_INPUTS))])
def test_lines_count_and_slices_share_one_convention(tmp_path, data):
    path = tmp_path / "f.txt"
    path.write_bytes(data)
    expected = oracle(data)
    lines = list(context_pack._lines(path, limit=1 << 30))
    assert lines == expected
    assert context_pack._line_count(path) == len(expected)
    if expected:
        texts, total = context_pack._extract(path, [(1, len(expected) + 5)], 1 << 30)
        assert texts == ["\n".join(expected)] and total == len(expected)
        texts, total = context_pack._extract(path, [(len(expected), len(expected))], 1 << 30)
        assert texts == [expected[-1]] and total is None


def test_overlong_lines_are_cut_but_still_one_line_each(tmp_path):
    path = tmp_path / "f.txt"
    path.write_bytes(b"short\n" + b"y" * 25 + b"\nlast")
    assert list(context_pack._lines(path, limit=10)) == ["short", "y" * 11, "last"]
    assert context_pack._line_count(path) == 3


def test_slice_numbers_match_grep_for_files_with_exotic_separators(tree):
    rows = ["alpha", "be\x0cta", "gam ma", "del\x85ta", "eps\x0bilon", "zeta"]
    track(tree, "odd.py", "\n".join(rows) + "\n")
    pack = pack_for(tree, "odd.py:5-6")
    assert "--- odd.py:5-6 ---\n```\neps\x0bilon\nzeta\n```" in pack.text   # grep -n numbers: 5 and 6
    assert not [w for w in pack.warnings if "past_eof" in w]


def test_cr_only_file_gets_the_big_file_warning(tree):
    track(tree, "cr.py", "\r".join(f"x{i}" for i in range(500)))
    assert [w for w in context_pack.warnings("edit cr.py", dict(CARD), tree) if w.startswith("big_file_unsliced: cr.py 500 lines")]
    track(tree, "crlf.py", "\r\n".join(f"x{i}" for i in range(500)) + "\r\n")
    assert [w for w in context_pack.warnings("edit crlf.py", dict(CARD), tree) if w.startswith("big_file_unsliced: crlf.py 500 lines")]


def test_range_past_eof_warns_and_clamps_or_drops(tree):
    track(tree, "ten.py", "".join(f"t{i}\n" for i in range(1, 11)))
    track(tree, "none.py", "")
    record = {}
    pack = context_pack.annotate(record, "p", dict(CARD, context=["ten.py:8-20", "ten.py:11-12", "ten.py:1-10", "none.py:1-3"]), tree)
    assert "--- ten.py:8-10 ---\n```\nt8\nt9\nt10\n```" in pack.text and "--- ten.py:1-10 ---" in pack.text
    assert "ten.py:11-12" not in pack.text and "none.py" not in pack.text and "```\n\n```" not in pack.text
    past = [w for w in pack.warnings if w.startswith("context_range_past_eof")]
    assert past == ["context_range_past_eof: ten.py:8-20 (file has 10 lines; clamped)",
                    "context_range_past_eof: ten.py:11-12 (file has 10 lines; not packed)",
                    "context_range_past_eof: none.py:1-3 (file has 0 lines; not packed)"]
    assert [e["detail"] for e in record["events"] if e["event"] == "context_range_past_eof"] == past
    assert record["warnings"] == list(pack.warnings)


def test_range_ending_exactly_at_eof_is_clean(tree):
    track(tree, "ten.py", "".join(f"t{i}\n" for i in range(1, 11)))
    assert pack_for(tree, "ten.py:10-10", "ten.py:1-10").warnings == ()


# --- I7: capped entries, one read per file, one listing and one pack per spawn --------------------------------


def test_entry_cap_refuses_one_more(tree):
    track(tree, "a.py", "a\n")
    ok = ["a.py:1-1"] * context_pack.MAX_ENTRIES
    assert pack_for(tree, *ok).text
    with pytest.raises(ContextError, match=f"cap is {context_pack.MAX_ENTRIES}"):
        pack_for(tree, *ok, "a.py:1-1")
    assert context_pack.MAX_ENTRIES == 64


def instrument(monkeypatch, tree):
    """Count git listings and file opens under ``tree``."""
    calls = {"ls_files": 0, "opened": []}
    real_run, real_open = subprocess.run, pathlib.Path.open

    def run(cmd, *a, **k):
        calls["ls_files"] += "ls-files" in cmd
        return real_run(cmd, *a, **k)

    def opened(self, *a, **k):
        if str(self).startswith(str(tree.resolve())):
            calls["opened"].append(self.name)
        return real_open(self, *a, **k)
    monkeypatch.setattr(context_pack.subprocess, "run", run)
    monkeypatch.setattr(pathlib.Path, "open", opened)
    return calls


def test_each_file_is_read_once_and_listed_once_per_spawn(tree, monkeypatch):
    track(tree, "a.py", "".join(f"a{i}\n" for i in range(1, 30)))
    track(tree, "b.py", "".join(f"b{i}\n" for i in range(1, 30)))
    track(tree, "big.py", "".join(f"x{i}\n" for i in range(500)))
    calls = instrument(monkeypatch, tree)
    card = dict(CARD, context=["a.py:1-3", "a.py:10-12", {"path": "b.py", "start": 2, "end": 4}, "a.py:20-22"])
    record = {}
    pack = context_pack.annotate(record, "edit big.py, a.py and b.py", card, tree)
    assert sorted(calls["opened"]) == ["a.py", "b.py", "big.py"] and calls["ls_files"] == 1
    for _ in range(3):   # three more legs reuse the spawn's pack: no listing, no read
        out = _Seat(tree)._leg_prompt("p", card, "implement", INLINE, pack)
        assert "a1" in out and "a21" in out and "b3" in out
    assert sorted(calls["opened"]) == ["a.py", "b.py", "big.py"] and calls["ls_files"] == 1
    assert [w.split(" ")[1] for w in pack.warnings] == ["big.py"]


def test_service_spawn_builds_the_pack_once_for_its_legs(tree, tmp_path, monkeypatch):
    track(tree, "a.py", "".join(f"a{i}\n" for i in range(1, 10)))
    svc = _service(tmp_path / "state", monkeypatch)
    svc.runner.default_cwd = str(tree)
    leg = next(l for l in DEFAULT_LEGS if l.id == "mimo-v2.6-pro")
    monkeypatch.setattr(svc, "_route", lambda *a, **k: SpawnPlan((leg,), (), "test", (), ()))
    calls = instrument(monkeypatch, tree)
    out = asyncio.run(svc.spawn(card=dict(CARD, context=["a.py:2-3"]), prompt="edit a.py", dry_run=True))
    sent = svc.runner.calls[0]["prompt"]
    assert "--- a.py:2-3 ---\n```\na2\na3\n```" in sent and out["status"] != "refused"
    assert calls["opened"] == ["a.py"] and calls["ls_files"] == 1


def test_dispatch_spawn_builds_the_pack_once_for_its_legs(tree, tmp_path, monkeypatch):
    from plangraph.spawner.runner import SpawnerRunner, spawn_service
    track(tree, "a.py", "".join(f"a{i}\n" for i in range(1, 10)))
    monkeypatch.setenv("PLANGRAPH_GATEWAY_URL", "http://127.0.0.1:9/v1")
    runner = SpawnerRunner(default_cwd=str(tree))
    service = spawn_service(runner, tmp_path / "state")
    leg = next(l for l in DEFAULT_LEGS if l.id == "mimo-v2.6-pro")
    monkeypatch.setattr(service, "_route", lambda *a, **k: SpawnPlan((leg,), (), "test", (), ()))
    calls = instrument(monkeypatch, tree)
    out = asyncio.run(service.spawn(card=dict(CARD, context=["a.py:2-3"]), prompt="edit a.py", dry_run=True, wait_seconds=None))
    sent = next(iter(runner.jobs.values())).task_prompt
    assert "--- a.py:2-3 ---\n```\na2\na3\n```" in sent and out["status"] != "refused"
    assert calls["opened"] == ["a.py"] and calls["ls_files"] == 1


def test_oversize_slice_is_dropped_without_holding_the_file(tree, monkeypatch):
    track(tree, "wide.py", "\n".join("w" * 100 for _ in range(300)) + "\n")
    monkeypatch.setenv("PLANGRAPH_CONTEXT_PACK_MAX_CHARS", "1000")
    texts, total = context_pack._extract(tree / "wide.py", [(1, 300), (2, 3)], 1000)
    assert texts[0] is None and texts[1] == "w" * 100 + "\n" + "w" * 100 and total is None
    pack = pack_for(tree, "wide.py:1-300", "wide.py:2-3")
    assert "omitted: wide.py:1-300" in pack.text and "--- wide.py:2-3 ---" in pack.text and len(pack.text) <= 1000
