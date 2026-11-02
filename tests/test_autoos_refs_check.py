"""Tests for tools/autoos_refs_check.py (redaction: test_autoos_refs_redact.py;
the routing roots, symlinks and ``-z``: test_autoos_refs_roots.py)."""

from __future__ import annotations

from pathlib import Path
import subprocess

import pytest

from autoos_refs_support import (  # noqa: F401  (ar is a fixture)
    ar, assert_no_leak, run_cli, windows_skip, write,
)


@windows_skip
def test_fixture_home_hits(tmp_path: Path) -> None:
    fixture_home = tmp_path / "fixture_home"
    fixture_home.mkdir()
    claude_dir = fixture_home / ".claude"

    # 1. settings.json hook pointing at /home/x/code/AutoOS/tools/hooks/bash_guard.py
    write(claude_dir / "settings.json",
          '{\n  "hooks": {\n    "PreToolUse": [\n'
          '      {"command": "/home/x/code/AutoOS/tools/hooks/bash_guard.py"}\n'
          "    ]\n  }\n}\n")

    # 2. symlinked skill into AutoOS
    skills_dir = claude_dir / "skills"
    skills_dir.mkdir(parents=True)
    skill_link = skills_dir / "autoos_skill"
    skill_link.symlink_to("/home/x/code/AutoOS/skills/guard_skill")

    # 3. systemd unit with AutoOS
    write(fixture_home / ".config" / "systemd" / "user" / "autoos.service",
          "[Unit]\nDescription=Test AutoOS Service\n\n"
          "[Service]\nExecStart=/home/x/code/AutoOS/bin/daemon\n")

    # 4. secret-looking file containing 'AutoOS' that must not be opened
    secret_file = fixture_home / ".local" / "share" / "plangraph" / "secret_launch.json"
    secret_content = "SUPER_SECRET_PAYLOAD_NEVER_PRINT_12345 /home/x/code/AutoOS/db"
    write(secret_file, secret_content)

    # Run check via CLI subprocess
    proc = run_cli("--home", str(fixture_home))

    assert proc.returncode == 1, f"Expected rc 1 on hits, got {proc.returncode}"
    out = proc.stdout

    # Assert secret content is NOT in output, only path name with SECRET-PATH
    assert_no_leak(("SUPER_SECRET_PAYLOAD_NEVER_PRINT_12345",), out)
    assert str(secret_file) in out
    assert "SECRET-PATH" in out

    # Assert other targets are reported
    assert "/home/x/code/AutoOS/tools/hooks/bash_guard.py" in out
    assert "/home/x/code/AutoOS/skills/guard_skill" in out
    assert "/home/x/code/AutoOS/bin/daemon" in out
    # 'AutoOS' as a word is matched too (case-insensitive)
    assert "Description=Test AutoOS Service" in out

    lines = [line for line in out.strip().splitlines() if line]
    assert len(lines) == 5
    for line in lines:
        parts = line.split("\t")
        assert len(parts) == 3, f"Line must have <kind>\\t<path>\\t<detail>: {line}"


@windows_skip
def test_clean_home_returns_zero(ar, tmp_path: Path) -> None:
    clean_home = tmp_path / "clean_home"
    clean_home.mkdir()
    claude_dir = clean_home / ".claude"

    # Clean settings.json
    write(claude_dir / "settings.json",
          '{"hooks": {"PreToolUse": [{"command": "/usr/local/bin/safe_hook.py"}]}}')

    # Clean skill symlink
    skills_dir = claude_dir / "skills"
    skills_dir.mkdir(parents=True)
    (skills_dir / "clean_skill").symlink_to("/usr/local/share/skills/clean")

    # Clean systemd unit
    write(clean_home / ".config" / "systemd" / "user" / "clean.service",
          "[Unit]\nDescription=Clean Service\n[Service]\nExecStart=/usr/bin/daemon\n")

    # Clean plangraph spec
    write(clean_home / ".local" / "share" / "plangraph" / "clean_spec.json",
          '{"target": "/opt/clean"}')

    # Clean bin router script
    write(clean_home / ".local" / "bin" / "clean_router", "#!/bin/sh\necho clean\n")

    # Clean instruction files (merged spotless scenario)
    write(claude_dir / "AGENTS.md", "See /usr/share/docs\n")
    write(claude_dir / "CLAUDE.md", "# Notes\nnothing here\n")
    write(clean_home / ".config" / "opencode" / "AGENTS.md", "clean\n")

    proc = run_cli("--home", str(clean_home))

    assert proc.returncode == 0, f"Expected rc 0 on clean home, got {proc.returncode}"
    assert proc.stdout == ""
    assert ar.scan_all(clean_home) == []


def test_secret_keywords_never_opened(ar, tmp_path: Path) -> None:
    home = tmp_path / "secrets_home"
    pg_dir = home / ".local" / "share" / "plangraph"

    filenames = [
        "my_token.json", "secret.txt", "credentials.env", ".env",
        "id_rsa", "cert.pem", "password.conf", "signing.key",
    ]
    for fn in filenames:
        write(pg_dir / fn, f"PRIVATE_DATA_{fn} /AutoOS/something")

    hits = ar.scan_all(home)
    assert len(hits) == len(filenames)
    for kind, path_str, detail in hits:
        assert detail == "SECRET-PATH"
        p = Path(path_str)
        assert ar.looks_secret(p)


@windows_skip
def test_tracked_repo_symlink(ar, tmp_path: Path) -> None:
    home = tmp_path / "home_empty"
    home.mkdir()

    repo = tmp_path / "test_repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init"], check=True, capture_output=True)

    link_autoos = repo / "autoos_link"
    link_autoos.symlink_to("/home/x/code/AutoOS/tools/helper.sh")
    link_clean = repo / "clean_link"
    link_clean.symlink_to("/usr/local/bin/helper.sh")

    subprocess.run(
        ["git", "-C", str(repo), "add", "autoos_link", "clean_link"],
        check=True,
        capture_output=True,
    )

    hits = ar.scan_all(home=home, repo=repo)
    assert len(hits) == 1
    kind, path_str, detail = hits[0]
    assert kind == "repo-symlink"
    assert path_str == str(link_autoos)
    assert detail == "/home/x/code/AutoOS/tools/helper.sh"


@windows_skip
def test_router_bin_and_mcp_configs(ar, tmp_path: Path) -> None:
    home = tmp_path / "extra_home"
    bin_dir = home / ".local" / "bin"

    # Symlink in bin
    write(bin_dir / "tool_script",
          "#!/bin/bash\nexec /AutoOS/bin/real_tool \"$@\"\n")
    (bin_dir / "tool_symlink").symlink_to("/AutoOS/bin/tool")

    # MCP configs
    write(home / ".claude.json",
          '{"mcpServers": {"test": {"command": "/AutoOS/mcp/srv"}}}\n')
    write(home / ".claude" / ".mcp.json",
          '{"server": "AutoOS/node_modules/srv"}\n')

    hits = ar.scan_all(home)
    assert len(hits) == 4
    details = [h[2] for h in hits]
    assert "/AutoOS/bin/tool" in details
    assert 'exec /AutoOS/bin/real_tool "$@"' in details
    assert any('"command": "/AutoOS/mcp/srv"' in d for d in details)
    assert any('"server": "AutoOS/node_modules/srv"' in d for d in details)


def test_instruction_files_scanned(ar, tmp_path: Path) -> None:
    home = tmp_path / "instr_home"
    claude = home / ".claude"
    (claude / "skills" / "s1").mkdir(parents=True)
    opencode = home / ".config" / "opencode"
    opencode.mkdir(parents=True)

    write(claude / "AGENTS.md", "Root: /home/x/code/AutoOS\n")
    write(claude / "CLAUDE.md", "See /home/x/code/AutoOS/docs\n")
    write(claude / "notes.md", "unit autoos-litellm.service restarts\n")
    write(opencode / "CLAUDE.md", "%h/code/AutoOS\n")
    # Skill markdown is only reached by the skill walker, not the instruction walker.
    write(claude / "skills" / "s1" / "SKILL.md", "/home/x/code/AutoOS/skills/x\n")

    hits = ar.scan_all(home)
    assert len(hits) == 5
    by_kind: dict[str, list[str]] = {}
    for kind, path_str, detail in hits:
        by_kind.setdefault(kind, []).append(f"{path_str}\t{detail}")
    assert sorted(by_kind) == ["instruction", "skill"]
    assert len(by_kind["instruction"]) == 4
    assert len(by_kind["skill"]) == 1
    assert any("autoos-litellm.service" in d for d in by_kind["instruction"])


def test_has_autoos_case_insensitive(ar) -> None:
    for text in (
        "/home/x/code/AutoOS/tools", "AUTOOS/x", "autoos-litellm.service",
        "x-autoos-litellm.timer", "%h/code/autoos", "/var/AutoOS/",
    ):
        assert ar.has_autoos(text), text
    for text in ("autoosx", "xautoos", "myautoostuff"):
        assert not ar.has_autoos(text), text


def test_secret_names_token_boundaries(ar, tmp_path: Path) -> None:
    for name in ("monkey", "keyboard", "monkey_patch.sh", "keyword.txt", "keyboards.json"):
        assert not ar.looks_secret(tmp_path / name), name
    for name in (
        "api_key.json", "apikey.txt", "my_token.json", "passwd",
        "credentials.json", "id_rsa", "id_ed25519.pub", "cert.pem",
        "x.key", "y.p12", ".env",
    ):
        assert ar.looks_secret(tmp_path / name), name


def test_redaction_on_matching_line(tmp_path: Path) -> None:
    home = tmp_path / "redact_home"
    claude = home / ".claude"
    write(claude / "settings.json",
          '{"hooks": {"PreToolUse": [{"command": "/home/x/code/AutoOS/h.py", '
          '"token": "sk-'
          'live-SECRETVALUE1234", '
          '"authorization": "Bearer abc.def.GHIJK"}]}}\n')
    write(claude / "hooks" / "hook.sh",
          "#!/bin/sh\nexec /home/x/code/AutoOS/bin/run sk-live-ZZZZ9999 "
          + "A" * 400
          + "\n")

    proc = run_cli("--home", str(home))
    assert proc.returncode == 1
    out = proc.stdout
    assert_no_leak(("SECRETVALUE1234", "abc.def.GHIJK", "ZZZZ9999"), out)
    assert out.count("<redacted>") >= 3
    assert "/home/x/code/AutoOS/h.py" in out
    for line in out.strip().splitlines():
        parts = line.split("\t")
        assert len(parts) == 3
        assert len(parts[2]) <= 160


def test_backups_and_jobs_excluded(ar, tmp_path: Path) -> None:
    home = tmp_path / "excl_home"
    write(home / ".local" / "share" / "plangraph" / "backups" / "snap.json",
          '{"cmd": "/home/x/code/AutoOS/bin"}\n')
    write(home / ".claude" / "jobs" / "j1" / ".mcp.json",
          '{"server": "/home/x/code/AutoOS/srv"}\n')

    assert ar.scan_all(home) == []


def test_timer_unit_flagged(ar, tmp_path: Path) -> None:
    home = tmp_path / "timer_home"
    write(home / ".config" / "systemd" / "user" / "autoos-litellm.timer",
          "[Unit]\nDescription=Timer\n[Timer]\nExecStart=%h/code/AutoOS/bin/tick.sh\n")

    hits = ar.scan_all(home)
    assert len(hits) == 1
    kind, path_str, detail = hits[0]
    assert kind == "systemd"
    assert path_str.endswith("autoos-litellm.timer")
    assert "%h/code/AutoOS/bin/tick.sh" in detail


def test_missing_home_exits_2(ar, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert ar.main(["--home", str(tmp_path / "absent")]) == 2
    assert "error" in capsys.readouterr().err


def test_repo_git_failure_exits_2(ar, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    home, bad_repo = tmp_path / "h", tmp_path / "not_a_repo"
    home.mkdir()
    bad_repo.mkdir()
    assert ar.main(["--home", str(home), "--repo", str(bad_repo)]) == 2
    assert "git" in capsys.readouterr().err
    with pytest.raises(ar.RepoScanError):
        ar.scan_all(home, repo=bad_repo)
    with pytest.raises(ar.RepoScanError):
        ar.scan_all(home, repo=tmp_path / "missing_repo")


def test_apikey_values_redacted(tmp_path: Path) -> None:
    home = tmp_path / "h"
    # Fragment-built so no synthetic token-shaped literal lands in the source.
    secret_a = "AAAA" + "1111"
    secret_b = "CCCC" + "3333"
    write(home / ".claude" / "settings.json",
          f'{{"cmd": "/home/x/code/AutoOS/run", "apikey": "{secret_a}", '
          f'"OPENAI_API_KEY": "{secret_b}"}}\n')
    proc = run_cli("--home", str(home))
    assert proc.returncode == 1
    out = proc.stdout
    assert_no_leak((secret_a, secret_b), out)
    assert out.count("<redacted>") >= 2


def test_settings_local_json_scanned(ar, tmp_path: Path) -> None:
    home = tmp_path / "h"
    write(home / ".claude" / "settings.local.json",
          '{"hooks": {"cmd": "/home/x/code/AutoOS/h.py"}}\n')
    hits = ar.scan_all(home)
    assert len(hits) == 1 and hits[0][0] == "claude-settings"


@windows_skip
def test_systemd_dropins_and_wants(ar, tmp_path: Path) -> None:
    home = tmp_path / "h"
    user = home / ".config" / "systemd" / "user"
    write(user / "autoos.service.d" / "override.conf",
          "[Service]\nExecStart=/home/x/code/AutoOS/bin/x\n")
    (user / "multi-user.target.wants").mkdir(parents=True)
    (user / "multi-user.target.wants" / "autoos.service").symlink_to(
        "/home/x/code/AutoOS/units/autoos.service")
    hits = ar.scan_all(home)
    assert len(hits) == 2 and {h[0] for h in hits} == {"systemd"}


@windows_skip
def test_symlinked_skill_dir_traversed(ar, tmp_path: Path) -> None:
    home = tmp_path / "h"
    skills = home / ".claude" / "skills"
    skills.mkdir(parents=True)
    real = tmp_path / "real_skill"
    write(real / "SKILL.md", "/home/x/code/AutoOS/skills/x\n")
    (skills / "linked").symlink_to(real, target_is_directory=True)
    # A link cycle must not loop or duplicate hits (resolved dirs are seen once).
    (skills / "loop").symlink_to(skills, target_is_directory=True)
    hits = ar.scan_all(home)
    assert len(hits) == 1 and hits[0][2] == "/home/x/code/AutoOS/skills/x"


def test_repo_symlink_regular_file_fallback(ar, tmp_path: Path) -> None:
    home = tmp_path / "h"
    home.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init"], check=True, capture_output=True)
    write(repo / "link", "/home/x/code/AutoOS/tools/h.sh\n")
    blob = subprocess.run(
        ["git", "-C", str(repo), "hash-object", "-w", "link"],
        check=True, capture_output=True, text=True).stdout.strip()
    subprocess.run(
        ["git", "-C", str(repo), "update-index", "--add",
         "--cacheinfo", f"120000,{blob},link"],
        check=True, capture_output=True)
    hits = ar.scan_all(home, repo=repo)
    assert len(hits) == 1 and hits[0][1] == str(repo / "link")
    assert "/home/x/code/AutoOS/tools/h.sh" in hits[0][2]


def test_tabs_flattened_in_report(ar, tmp_path: Path) -> None:
    home = tmp_path / "h"
    write(home / ".claude" / "CLAUDE.md", "/home/x/code/AutoOS/x\ttabbed\tline\r\n")
    hits = ar.scan_all(home)
    assert len(hits) == 1
    assert "\t" not in hits[0][2] and "\r" not in hits[0][2]
    out = run_cli("--home", str(home)).stdout
    assert all(len(line.split("\t")) == 3 for line in out.strip().splitlines())


def test_secret_path_only_exits_3(ar, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    home = tmp_path / "h"
    write(home / ".local" / "share" / "plangraph" / "launch_token.json",
          "/home/x/code/AutoOS/db\n")
    assert ar.main(["--home", str(home)]) == 3
    assert "SECRET-PATH" in capsys.readouterr().out
