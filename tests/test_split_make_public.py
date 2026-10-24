"""tools/split/make_public.py: allow-list copy, deny refusal, fresh one-commit history."""
import json
import ntpath
import os
import posixpath
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "split"))

import make_public as mp  # noqa: E402

MANIFEST = """\
[public]
include = ["src", "tests", "tools", "docs/PUBLIC.md", "examples"]
optional = ["LICENSE", "README.md", "pyproject.toml", "NOT_THERE"]
exclude = ["**/__pycache__/**", "*.pyc", "tests/fixtures/real/**", "tools/private_*"]
[deny]
paths = ["**/*.db", ".env*", "**/secrets/**", "DECISIONS.md"]
[commit]
author_name = "plangraph contributors"
author_email = "noreply@example.invalid"
message = "Initial public history"
"""


def git(cwd, *args):
    env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True,
                          capture_output=True, text=True).stdout


def write(root, rel, text="x = 1\n"):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


@pytest.fixture
def src(tmp_path):
    s = tmp_path / "src_repo"
    for rel in ("src/pkg/__init__.py", "src/pkg/core.py", "tests/test_core.py",
                "tests/fixtures/synthetic.json", "tools/apply.py", "docs/PUBLIC.md",
                "examples/p1.yaml", "README.md", "pyproject.toml"):
        write(s, rel)
    # present in the source but NOT public:
    for rel in ("tests/fixtures/real/site.json", "tools/private_sync.py",
                "src/pkg/__pycache__/core.pyc", "docs/RUNBOOK.md", "DECISIONS.md",
                ".claude/settings.json", "out/page.html", "notes.txt"):
        write(s, rel, "private\n")
    git(tmp_path, "init", "-q", "-b", "main", str(s))
    git(s, "add", "-A")
    for i in range(3):
        git(s, "-c", "user.name=Real Person", "-c", "user.email=real@corp.example",
            "commit", "-q", "--allow-empty", "-m", f"private history {i}")
    return s


@pytest.fixture
def manifest(tmp_path):
    p = tmp_path / "manifest.toml"
    p.write_text(MANIFEST)
    return p


def run(src, out, manifest, *extra):
    words = [] if "--words" in extra or "--no-words" in extra else ["--no-words"]
    return mp.main(["--source", str(src), "--out", str(out), "--manifest", str(manifest),
                    "--no-gitleaks", *words, *extra])


EXPECT = {"src/pkg/__init__.py", "src/pkg/core.py", "tests/test_core.py",
          "tests/fixtures/synthetic.json", "tools/apply.py", "docs/PUBLIC.md",
          "examples/p1.yaml", "README.md", "pyproject.toml"}


def test_build_is_fresh_one_commit_exact_allow_list(tmp_path, src, manifest, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".gitconfig").write_text("[user]\n\tname = Someone Else\n[core]\n\thooksPath = /nope\n")
    monkeypatch.setenv("HOME", str(home))
    out = tmp_path / "pub"
    assert run(src, out, manifest) == 0
    assert set(git(out, "ls-files").split()) == EXPECT
    assert {p.relative_to(out).as_posix() for p in out.rglob("*")
            if p.is_file() and ".git" not in p.relative_to(out).parts} == EXPECT
    assert git(out, "rev-list", "--count", "HEAD").strip() == "1"
    assert git(out, "log", "-1", "--format=%an|%ae|%cn|%s").strip() == (
        "plangraph contributors|noreply@example.invalid|plangraph contributors|Initial public history")
    assert git(out, "status", "--porcelain").strip() == ""
    src_head = git(src, "rev-parse", "HEAD").strip()
    assert src_head not in git(out, "log", "--all", "--format=%H")


def test_deny_listed_survivor_is_refused_and_nothing_written(tmp_path, src, manifest, capsys):
    write(src, "src/pkg/state.db", "\n")
    write(src, "src/pkg/secrets/k.txt", "\n")
    out = tmp_path / "pub"
    assert run(src, out, manifest) == 1
    text = capsys.readouterr().out
    assert "src/pkg/state.db:0: deny" in text and "src/pkg/secrets/k.txt:0: deny" in text
    assert not out.exists()
    assert [p for p in tmp_path.iterdir() if p.name.startswith(".mp-")] == []


def test_dirty_content_refused_value_not_printed(tmp_path, src, manifest, capsys):
    write(src, "src/pkg/conf.py", "H = '" + "build7" + ".corp.internal" + "'\n"
          "K = '" + "gh" + "p_" + "Zy9Xw8Vu7T" * 3 + "abcdef" + "'\n"
          "P = '" + "/ho" + "me/" + "zoltan" + "/x'\n")
    out = tmp_path / "pub"
    assert run(src, out, manifest) == 1
    text = capsys.readouterr().out
    for rule in ("hostname", "token-shape", "home-path"):
        assert f"src/pkg/conf.py:" in text and rule in text
    assert "build7" not in text and "Zy9Xw8Vu7T" not in text and "zoltan" not in text
    assert not out.exists()


def test_private_words_file_applies_to_staged_tree(tmp_path, src, manifest, capsys):
    words = tmp_path / "w.txt"
    words.write_text("Project" + "Mordor\n")
    write(src, "src/pkg/doc.py", "# Project" + "Mordor notes\n")
    assert run(src, tmp_path / "pub", manifest, "--words", str(words)) == 1
    assert "private-word" in capsys.readouterr().out


def test_check_mode_builds_nothing(tmp_path, src, manifest, capsys):
    out = tmp_path / "pub"
    assert run(src, out, manifest, "--check") == 0
    assert not out.exists() and "ok" in capsys.readouterr().out
    write(src, "src/pkg/e.py", "m = '" + "a.b" + "@" + "acme-corp" + ".io'\n")
    assert run(src, out, manifest, "--check") == 1


def test_existing_nonempty_out_is_refused(tmp_path, src, manifest):
    out = tmp_path / "pub"
    write(out, "keep.txt")
    assert run(src, out, manifest) == 2
    assert (out / "keep.txt").exists()


def test_symlink_and_missing_required_are_refused(tmp_path, src, manifest, capsys):
    (src / "src" / "pkg" / "ln").symlink_to("/etc/hostname")
    assert run(src, tmp_path / "pub", manifest) == 1
    assert "src/pkg/ln:0: symlink" in capsys.readouterr().out
    (src / "src" / "pkg" / "ln").unlink()
    (src / "examples" / "p1.yaml").unlink()
    (src / "examples").rmdir()
    assert run(src, tmp_path / "pub2", manifest) == 2
    assert "examples" in capsys.readouterr().err


def _fake_gitleaks(tmp_path, findings):
    f = tmp_path / "fake-gitleaks"
    f.write_text("#!/bin/sh\nrp=\nwhile [ $# -gt 0 ]; do [ \"$1\" = --report-path ] && rp=$2; "
                 "[ \"$1\" = -s ] && src=$2; shift; done\n"
                 f"printf '%s' '{json.dumps(findings)}' | sed \"s#@SRC@#$src#\" > \"$rp\"\n"
                 f"exit {1 if findings else 0}\n")
    f.chmod(f.stat().st_mode | stat.S_IXUSR)
    return f


def test_gitleaks_wrapped_skipped_when_missing(tmp_path, src, manifest, capsys, monkeypatch):
    only_git = tmp_path / "bin"
    only_git.mkdir()
    (only_git / "git").symlink_to(shutil.which("git"))
    monkeypatch.setenv("PATH", str(only_git))
    assert mp.main(["--no-words", "--source", str(src), "--out", str(tmp_path / "pub"),
                    "--manifest", str(manifest)]) == 0
    assert "gitleaks not installed" in capsys.readouterr().err


def test_gitleaks_findings_refuse_without_secret(tmp_path, src, manifest, capsys):
    fake = _fake_gitleaks(tmp_path, [{"File": "@SRC@/src/pkg/core.py", "StartLine": 1,
                                      "RuleID": "generic-api-key", "Secret": "TOPSECRETVALUE"}])
    rc = mp.main(["--no-words", "--source", str(src), "--out", str(tmp_path / "pub"),
                  "--manifest", str(manifest), "--gitleaks", str(fake)])
    out = capsys.readouterr()
    assert rc == 1 and "src/pkg/core.py:1: gitleaks:generic-api-key" in out.out
    assert "TOPSECRETVALUE" not in out.out + out.err
    assert not (tmp_path / "pub").exists()
    clean = _fake_gitleaks(tmp_path, [])
    assert mp.main(["--no-words", "--source", str(src), "--out", str(tmp_path / "pub"),
                    "--manifest", str(manifest), "--gitleaks", str(clean)]) == 0


def test_glob_semantics():
    g = mp.glob_re
    assert g("**/__pycache__/**").match("src/a/__pycache__/x.pyc")
    assert g("*.pyc").match("a/b/c.pyc") and not g("*.pyc").match("a/b/c.py")
    assert g("tools/private_*").match("tools/private_x.py")
    assert not g("tools/private_*").match("tools/sub/private_x.py")
    assert g(".env*").match("a/.env.local")


def test_shipped_manifest_passes_on_this_tree(capsys):
    assert mp.main(["--no-words", "--source", str(ROOT), "--check", "--no-gitleaks"]) == 0
    assert "check ok" in capsys.readouterr().out


def test_bad_manifest_is_a_usage_error(tmp_path, src, capsys):
    bad = tmp_path / "bad.toml"
    bad.write_text('[public]\ninclude = ["src"]\nbogus = 1\n')
    assert mp.main(["--source", str(src), "--check", "--manifest", str(bad)]) == 2
    assert "bogus" in capsys.readouterr().err


def test_deny_globs_case_insensitive_and_private_path_hashed(tmp_path, src, manifest, capsys):
    words = tmp_path / "w.txt"
    words.write_text("Project" + "Mordor\n")
    write(src, "src/pkg/STATE.DB", "\n")
    write(src, "src/pkg/Project" + "Mordor.py", "x = 1\n")
    write(src, "src/pkg/Project" + "Mordor.bin", "\n")
    (src / ("src/pkg/Project" + "Mordor.bin")).write_bytes(b"\x00\x01")
    assert run(src, tmp_path / "pub", manifest, "--words", str(words)) == 1
    out = capsys.readouterr()
    assert "src/pkg/STATE.DB:0: deny" in out.out
    assert "Mordor" not in out.out + out.err and "<path-hash:" in out.out + out.err


def test_gitleaks_outside_path_is_normalised(tmp_path, src, manifest, capsys):
    fake = _fake_gitleaks(tmp_path, [{"File": "/etc/ssh/host_key", "StartLine": 3,
                                      "RuleID": "private-key", "Secret": "S"}])
    rc = mp.main(["--no-words", "--source", str(src), "--out", str(tmp_path / "pub"),
                  "--manifest", str(manifest), "--gitleaks", str(fake)])
    out = capsys.readouterr().out
    assert rc == 1 and "<outside>:3: gitleaks:private-key" in out
    assert "/etc" not in out and ".." not in out


def test_tree_rel_nt_path_flavour():
    # a gitleaks File on a different drive must report <outside>, not raise (b1)
    assert mp._tree_rel("D:\\other\\key.pem", "C:\\work\\tree", pathmod=ntpath) == "<outside>"
    assert mp._tree_rel("C:\\work\\tree\\src\\a.py", "C:\\work\\tree",
                        pathmod=ntpath) == "src\\a.py"
    assert mp._tree_rel("..\\x", "C:\\work\\tree", pathmod=ntpath) == "<outside>"


def test_tree_rel_posix_flavour_is_unchanged():
    # "/etc/pass"+"wd": assembled, so this file's own scan reads no word/shape pair
    assert mp._tree_rel("/etc/" + "pass" + "wd", "/work/tree",
                        pathmod=posixpath) == "<outside>"
    assert mp._tree_rel("src/a.py", "/work/tree", pathmod=posixpath) == "src/a.py"
    assert mp._tree_rel("", "/work/tree", pathmod=posixpath) == "<unknown>"
    assert mp._tree_rel(None, "/work/tree", pathmod=posixpath) == "<unknown>"
    # the CLI's own flavour (os.path) keeps the same results on any host
    assert mp._tree_rel(None, "/work/tree") == "<unknown>"
    assert mp._tree_rel("../x", "/work/tree") == "<outside>"
    assert mp._tree_rel("src" + os.sep + "a.py", "/work/tree") == "src" + os.sep + "a.py"


# --- PG-RF-WORDS: the private names list is required for a real build ---------------

WORD = "Project" + "Mordor"  # split so this file never trips the word list itself


def _words(tmp_path, text=WORD + "\n"):
    w = tmp_path / "w.txt"
    w.write_text(text)
    return str(w)


def _main(src, out, manifest, *extra):
    """Like run() but with NO implicit --no-words."""
    return mp.main(["--source", str(src), "--out", str(out), "--manifest", str(manifest),
                    "--no-gitleaks", *extra])


@pytest.mark.parametrize("extra", [(), ("--check",)])
def test_words_required_unless_no_words(tmp_path, src, manifest, capsys, extra):
    out = tmp_path / "pub"
    assert _main(src, out, manifest, *extra) == 2
    err = capsys.readouterr().err
    assert "--words" in err and "required" in err
    assert not out.exists() and [p for p in tmp_path.iterdir() if p.name.startswith(".mp-")] == []


def test_no_words_dev_flag_builds_with_warning(tmp_path, src, manifest, capsys):
    assert _main(src, tmp_path / "pub", manifest, "--no-words") == 0
    assert "--no-words" in capsys.readouterr().err


def test_words_and_no_words_conflict(tmp_path, src, manifest):
    assert _main(src, tmp_path / "pub", manifest, "--words", _words(tmp_path), "--no-words") == 2


@pytest.mark.parametrize("text", ["", "# only a comment\n\n   \n"])
def test_empty_words_file_is_refused(tmp_path, src, manifest, capsys, text):
    assert _main(src, tmp_path / "pub", manifest, "--check", "--words", _words(tmp_path, text)) == 2
    assert "no entries" in capsys.readouterr().err


def test_missing_words_file_is_refused(tmp_path, src, manifest, capsys):
    assert _main(src, tmp_path / "pub", manifest, "--check", "--words", str(tmp_path / "nope.txt")) == 2
    assert "cannot read words file" in capsys.readouterr().err


def test_bad_regex_in_words_names_line_not_entry(tmp_path, src, manifest, capsys):
    bad = "re:" + "(unclosed-" + "Mordor"
    assert _main(src, tmp_path / "pub", manifest, "--check",
                 "--words", _words(tmp_path, f"# c\n{WORD}\n{bad}\n")) == 2
    cap = capsys.readouterr()
    assert "line 3" in cap.err
    assert "unclosed" not in cap.out + cap.err and "Mordor" not in cap.out + cap.err


def test_undecodable_words_file_is_refused(tmp_path, src, manifest, capsys):
    w = tmp_path / "w.txt"
    w.write_bytes(b"\xff\xfe\x00badword" + b"Qx" + b"7\n")
    assert _main(src, tmp_path / "pub", manifest, "--check", "--words", str(w)) == 2
    out = capsys.readouterr()
    assert "UTF-8" in out.err
    assert "badword" not in out.err + out.out and "\ufffd" not in out.err + out.out   # never echoed


def test_overflowing_regex_word_is_refused_with_line_only(tmp_path, src, manifest, capsys):
    w = tmp_path / "w.txt"
    w.write_text("ok" + "name\nre:a{99999999999999999999}\n")
    assert _main(src, tmp_path / "pub", manifest, "--check", "--words", str(w)) == 2
    out = capsys.readouterr()
    assert "line 2" in out.err and "99999999999999999999" not in out.err + out.out


def test_words_matching_is_case_insensitive_and_whole_token(tmp_path, src, manifest, capsys):
    w = _words(tmp_path, "# names\nZan" + "dor\n20" + "128\n/ho" + "me/s\n")
    clean = tmp_path / "clean"
    write(src, "src/pkg/ok.py", "x = 'Zan" + "dors' 'xZan" + "dor' 'Zan" + "dorx' 'p120" + "128' 'x_20" + "1289'\n")
    assert _main(src, clean, manifest, "--words", w) == 0, capsys.readouterr().out
    for i, line in enumerate(["# zan" + "DOR here", "port 20" + "128.", "x = 'ZANDOR'", "a_zan" + "dor_b"]):
        write(src, "src/pkg/ok.py", line + "\n")
        assert _main(src, tmp_path / f"pub{i}", manifest, "--words", w) == 1
        out = capsys.readouterr().out
        assert "src/pkg/ok.py:1: private-word" in out


def test_words_content_never_in_output(tmp_path, src, manifest, capsys):
    """Hit in content, in a file name, in a deny-listed path and in a gitleaks path."""
    w = _words(tmp_path, "Zan" + "dor\nre:lab-" + r"\d+" + "\n")
    write(src, "src/pkg/Zan" + "dor_notes.py", "# nothing\n")
    write(src, "src/pkg/secrets/Zan" + "dor.txt", "\n")
    write(src, "src/pkg/c.py", "# lab-" + "42 and Zan" + "dor\n")
    assert _main(src, tmp_path / "pub", manifest, "--words", w) == 1
    cap = capsys.readouterr()
    text = cap.out + cap.err
    assert "private-word" in text and "<path-hash:" in text
    assert "zandor" not in text.lower() and "lab-42" not in text


# --- PG-RF-WORDS: reviewed gitleaks allowlist for synthetic test fixtures --------------

LEAKS_CFG = ROOT / "tools" / "split" / "gitleaks.toml"
FAKE = "Zx81kQ92" + "LmPq77Vb"  # the reviewed synthetic demo value
#: The two private-key waiver regexes, exactly as BOTH configs must carry them:
#: pinned as text so a widened waiver (prefix-only, open-ended body) fails the pin even
#: while every scan test still passes.  The "\s*" spellings keep this file's own text
#: from ever matching the rule the config exempts.
PEM_WAIVER_FAKE = (
    r"^-----BEGIN TEST\s*PRIVATE KEY-----(?:\r?\nA{64})+\r?\n"
    r"-----END TEST\s*PRIVATE KEY-----$"
)
PEM_WAIVER_HISTORICAL = (
    r'^-----BEGIN TEST\s*PRIVATE KEY-----"\.to_string\(\)\];\s+for _ in 0\.\.body_lines'
    r'\s*\{\s+v\.push\("A"\.repeat\(64\)\);\s+\}\s+v\.push\("-----END TEST\s*PRIVATE KEY-----$'
)


def test_leaks_allowlist_only_names_test_paths():
    """Exact pins per entry: count, sorted path list, targetRules list, value set.

    Every entry of BOTH configs is compared as a whole
    ``(sorted paths, targetRules, value set)``, so a NEW entry, path, rule or value
    fails here -- not just a broad one.  Historical prose and Rust test fixtures are
    the only non-tests/ exceptions.
    """
    import tomllib
    cfg = tomllib.loads(LEAKS_CFG.read_text())
    assert cfg["extend"]["useDefault"] is True
    lists = cfg["allowlists"]
    root = tomllib.loads((ROOT / ".gitleaks.toml").read_text())
    assert lists == root["allowlists"]

    # Historical prose and Rust test fixtures are the only non-tests/ exceptions.
    exceptions = {
        r"^PROMPT\.md$", r"^docs/plans/2026-10-16-orchestration-setup\.md$",
        r"^rust/worker-core/src/sandbox_env\.rs$",
        r"^rust/worker/tests/fixtures/phase_defer\.json$",
        r"^rust/worker/tests/fixtures/phase_two_cards\.json$",
        r"^rust/worker-core/src/check\.rs$", r"^rust/worker-core/src/redact\.rs$",
    }
    generic = ("generic-api-key",)
    #: 6 replay ids the current fixtures hold + 12 older generations only git history has.
    replay = frozenset({
        r"^e2e-defer-a-33069540525861094644722115702876536833$",
        r"^e2e-defer-b-33069540525861163949139600629661958146$",
        r"^phase-33069540525861169132674685342045962243$",
        r"^e2e-phase-a-33069540670160061784166484599698882561$",
        r"^e2e-phase-b-33069540670160144222665750007685054466$",
        r"^phase-33069540670160152892635464651174313987$",
        r"^e2e-defer-a-33069397607394545167591213328121200644$",
        r"^e2e-defer-a-33069465295839244738770422596414472193$",
        r"^e2e-defer-b-33069397607394594328164169764076257285$",
        r"^e2e-defer-b-33069465295839301296487752589899726850$",
        r"^phase-33069397607394603385515509955466100742$",
        r"^phase-33069465295839305354771448806001082371$",
        r"^e2e-phase-a-33069397622649438415115238476464783369$",
        r"^e2e-phase-a-33069465440153862981330642214886309889$",
        r"^e2e-phase-b-33069397622649511593348978882256044042$",
        r"^e2e-phase-b-33069465440153917306991939289515819010$",
        r"^phase-33069397622649516942904760258026012683$",
        r"^phase-33069465440153924519668872109950500867$",
    })
    expected = {
        ((r"^PROMPT\.md$",), generic, frozenset({r"^bool/str/negative$"})),
        ((r"^docs/plans/2026-10-16-orchestration-setup\.md$",), generic,
         frozenset({r"^RT\-P2\-DECISIONS$"})),
        ((r"^tests/test_keeper_"
          r"credential_guard\.py$",), generic,
         frozenset({r"^svcacct/Zq9xk2LmPw$"})),
        ((r"^tests/test_keeper_estimate_src_edit\.py$",), generic,
         frozenset({r"^svcacct/Zq9xk2LmPw$"})),
        ((r"^tests/test_keeper_guard_key\.py$",), generic,
         frozenset({r"^svc/Xk9fT2mQ$"})),
        ((r"^tests/fixtures/md_sync/QUESTIONS\.md$",
          r"^tests/fixtures/md_sync/sample_questions\.md$"),
         generic, frozenset({r"^Zx81kQ92LmPq77Vb$"})),
        ((r"^tests/test_md_sync_e2e\.py$",), generic, frozenset({r"^Zx81kQ92LmPq77Vb$"})),
        ((r"^tests/test_md_sync_parse\.py$",), generic, frozenset({r"^Zx81kQ92LmPq77Vb$"})),
        ((r"^tests/test_md_sync_parse\.py$",), generic, frozenset({r"^AbCd1234Efgh$"})),
        ((r"^tests/test_md_sync_plan\.py$",), generic, frozenset({r"^Zx81kQ92LmPq77Vb$"})),
        ((r"^rust/worker-core/src/sandbox_env\.rs$",), generic,
         frozenset({r"^sentinel-wf8-opkey$"})),
        ((r"^rust/worker/tests/fixtures/phase_defer\.json$",
          r"^rust/worker/tests/fixtures/phase_two_cards\.json$"), generic, replay),
        ((r"^rust/worker-core/src/check\.rs$", r"^rust/worker-core/src/redact\.rs$"),
         ("private-key",), frozenset({PEM_WAIVER_FAKE})),
        ((r"^rust/worker-core/src/check\.rs$", r"^rust/worker-core/src/redact\.rs$"),
         ("private-key",), frozenset({PEM_WAIVER_HISTORICAL})),
    }
    actual = {(tuple(sorted(al["paths"])), tuple(al["targetRules"]),
               frozenset(al["regexes"])) for al in lists}
    assert len(lists) == len(expected) == 14, f"{len(lists)} allowlist entries, 14 pinned"
    missing, unexpected = expected - actual, actual - expected
    assert not missing and not unexpected, (
        f"missing pinned entries: {sorted(map(repr, missing))}; "
        f"unexpected entries: {sorted(map(repr, unexpected))}")
    for al in lists:
        assert al["condition"] == "AND"
        assert all(p.startswith("^tests/") or p in exceptions for p in al["paths"])
        assert all(p.startswith("^") and p.endswith("$") for p in al["paths"])
        assert all(r.startswith("^") and r.endswith("$") for r in al["regexes"])


def test_historical_md_sync_path_is_covered_in_both_configs():
    import tomllib
    for config in (LEAKS_CFG, ROOT / ".gitleaks.toml"):
        lists = tomllib.loads(config.read_text())["allowlists"]
        assert any(r"^tests/fixtures/md_sync/QUESTIONS\.md$" in al["paths"]
                   and al["regexes"] == [r"^Zx81kQ92LmPq77Vb$"] for al in lists)


def test_public_phase_values_are_exact():
    import tomllib
    lists = tomllib.loads(LEAKS_CFG.read_text())["allowlists"]
    entry = next(al for al in lists if r"^rust/worker/tests/fixtures/phase_defer\.json$" in al["paths"])
    # 6 ids the current fixtures hold + the 12 older generations only git history still has.
    assert len(entry["regexes"]) == 18
    assert all("[0-9]" not in value and "\\d" not in value for value in entry["regexes"])


def test_public_pem_waivers_are_exact():
    """Both configs: exactly two private-key entries, exact rule, paths and regexes.

    The regex text is pinned verbatim (PEM_WAIVER_*), so a widened waiver -- a
    header-only prefix or an open-ended body -- fails this pin even while every
    scan test still passes.
    """
    import tomllib
    for config in (LEAKS_CFG, ROOT / ".gitleaks.toml"):
        lists = tomllib.loads(config.read_text())["allowlists"]
        entries = [al for al in lists if "private-key" in al.get("targetRules", ())]
        assert len(entries) == 2, f"{config}: exactly two private-key entries expected"
        for entry in entries:
            assert entry["targetRules"] == ["private-key"]
            assert entry["condition"] == "AND"
            assert tuple(entry["paths"]) == (r"^rust/worker-core/src/check\.rs$",
                                             r"^rust/worker-core/src/redact\.rs$")
        assert {tuple(entry["regexes"]) for entry in entries} == {
            (PEM_WAIVER_FAKE,), (PEM_WAIVER_HISTORICAL,)}


def test_public_config_provenance_and_guardrail():
    """Parse-and-compare: the public config is the make_public build of the root one.

    Structured fields are compared as parsed data (title, extend, allowlists -- the
    allowlists themselves value-by-value in test_leaks_allowlist_only_names_test_paths);
    the provenance/guardrail sentences live in the header comment, so they are checked
    against the parsed comment lines instead of anywhere in the raw text.
    """
    import tomllib
    cfg = tomllib.loads(LEAKS_CFG.read_text(encoding="utf-8"))
    root = tomllib.loads((ROOT / ".gitleaks.toml").read_text(encoding="utf-8"))
    assert cfg["title"] == "plangraph public build"
    assert cfg["extend"] == {"useDefault": True} == root["extend"]
    assert cfg["allowlists"] == root["allowlists"]
    comments = "\n".join(line for line in LEAKS_CFG.read_text(encoding="utf-8").splitlines()
                         if line.lstrip().startswith("#"))
    assert "passed with -c by make_public" in comments
    assert "Never add a non-test path here" in comments


def test_public_real_pem_in_other_file_is_reported(tmp_path):
    """gitleaks is REQUIRED here -- no skip when the binary is absent.

    A gate test that silently skips on a gitleaks-less runner proves nothing, so the
    binary must exist (PATH or ~/.local/bin) or this test FAILS.  The public config
    must report a real PEM outside the two allow-listed fixture paths.
    """
    exe = shutil.which("gitleaks") or str(Path.home() / ".local" / "bin" / "gitleaks")
    assert Path(exe).exists(), "gitleaks is required for this gate test (PATH or ~/.local/bin)"
    header = "-----BEGIN " + "PRIVATE KEY-----"
    footer = "-----END " + "PRIVATE KEY-----"
    body = "MIIEvQIBADANBgkqhkiG" + str(9) + "w" + str(0) + "BAQEFAASCBKcwggSjAgEAAoIBAQC"
    write(tmp_path, "src/other.pem", header + "\n" + body + "\n" + footer)
    hits, error = mp.run_gitleaks(tmp_path, exe, LEAKS_CFG)
    assert not error and hits



@pytest.mark.skipif(shutil.which("gitleaks") is None
                    and not (Path.home() / ".local/bin/gitleaks").exists(), reason="gitleaks not installed")
def test_leaks_allowlist_exempts_exact_fixture_only(tmp_path):
    exe = shutil.which("gitleaks") or str(Path.home() / ".local/bin/gitleaks")
    line = f"Rotate the demo token: {FAKE} now\n"

    def scan(rel, text, config=LEAKS_CFG):
        tree = tmp_path / rel.replace("/", "_")
        write(tree, rel, text)
        return mp.run_gitleaks(tree, exe, config)[0]

    assert scan("tests/test_md_sync_parse.py", line, None) != []          # rule fires without it
    assert scan("tests/test_md_sync_parse.py", line) == []                # exact path + value
    assert scan("src/pkg/real.py", line) != []                            # non-test path: never exempt
    assert scan("src/tests/test_md_sync_parse.py", line) != []            # nested tests/ is not the root one
    other = "Rotate the demo token: " + "Qw7" + "ErT" + "9yUi" + "Op2AsD" + "\n"
    assert scan("tests/test_md_sync_parse.py", other) != []               # other value, same path


def test_words_path_that_is_a_directory_is_refused(tmp_path, src, manifest, capsys):
    assert _main(src, tmp_path / "pub", manifest, "--check", "--words", str(tmp_path)) == 2
    assert "cannot read words file" in capsys.readouterr().err


def test_gitleaks_path_with_private_word_is_hashed(tmp_path, src, manifest, capsys):
    w = _words(tmp_path, "Zan" + "dor\nre:lab-" + r"\d+" + "\n")
    fake = _fake_gitleaks(tmp_path, [{"File": "@SRC@/src/pkg/Zan" + "dor_key.py", "StartLine": 2,
                                      "RuleID": "generic-api-key", "Secret": "S"}])
    assert mp.main(["--source", str(src), "--out", str(tmp_path / "pub"), "--manifest", str(manifest),
                    "--words", w, "--gitleaks", str(fake)]) == 1
    cap = capsys.readouterr()
    text = cap.out + cap.err
    assert "<path-hash:" in text and ":2: gitleaks:generic-api-key" in text
    assert "zandor" not in text.lower() and "lab-" not in text
