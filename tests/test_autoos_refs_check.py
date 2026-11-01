"""Tests for tools/autoos_refs_check.py."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

from tools.autoos_refs_check import (
    SECRET_KEYWORDS,
    has_autoos,
    looks_secret,
    main,
    scan_all,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "tools" / "autoos_refs_check.py"


def test_fixture_home_hits(tmp_path: Path) -> None:
    fixture_home = tmp_path / "fixture_home"
    fixture_home.mkdir()

    # 1. settings.json hook pointing at /home/x/code/AutoOS/tools/hooks/bash_guard.py
    claude_dir = fixture_home / ".claude"
    claude_dir.mkdir(parents=True)
    settings_file = claude_dir / "settings.json"
    settings_file.write_text(
        '{\n  "hooks": {\n    "PreToolUse": [\n'
        '      {"command": "/home/x/code/AutoOS/tools/hooks/bash_guard.py"}\n'
        "    ]\n  }\n}\n",
        encoding="utf-8",
    )

    # 2. symlinked skill into AutoOS
    skills_dir = claude_dir / "skills"
    skills_dir.mkdir(parents=True)
    skill_link = skills_dir / "autoos_skill"
    skill_link.symlink_to("/home/x/code/AutoOS/skills/guard_skill")

    # 3. systemd unit with AutoOS
    systemd_dir = fixture_home / ".config" / "systemd" / "user"
    systemd_dir.mkdir(parents=True)
    unit_file = systemd_dir / "autoos.service"
    unit_file.write_text(
        "[Unit]\nDescription=Test AutoOS Service\n\n"
        "[Service]\nExecStart=/home/x/code/AutoOS/bin/daemon\n",
        encoding="utf-8",
    )

    # 4. secret-looking file containing 'AutoOS' that must not be opened
    secret_dir = fixture_home / ".local" / "share" / "plangraph"
    secret_dir.mkdir(parents=True)
    secret_file = secret_dir / "secret_launch.json"
    secret_content = "SUPER_SECRET_PAYLOAD_NEVER_PRINT_12345 /home/x/code/AutoOS/db"
    secret_file.write_text(secret_content, encoding="utf-8")

    # Run check via CLI subprocess
    proc = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--home", str(fixture_home)],
        capture_output=True,
        text=True,
    )

    assert proc.returncode == 1, f"Expected rc 1 on hits, got {proc.returncode}"
    out = proc.stdout

    # Assert secret content is NOT in output, only path name with SECRET-PATH
    assert "SUPER_SECRET_PAYLOAD_NEVER_PRINT_12345" not in out
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


def test_clean_home_returns_zero(tmp_path: Path) -> None:
    clean_home = tmp_path / "clean_home"
    clean_home.mkdir()

    # Clean settings.json
    claude_dir = clean_home / ".claude"
    claude_dir.mkdir(parents=True)
    (claude_dir / "settings.json").write_text(
        '{"hooks": {"PreToolUse": [{"command": "/usr/local/bin/safe_hook.py"}]}}',
        encoding="utf-8",
    )

    # Clean skill symlink
    skills_dir = claude_dir / "skills"
    skills_dir.mkdir(parents=True)
    (skills_dir / "clean_skill").symlink_to("/usr/local/share/skills/clean")

    # Clean systemd unit
    systemd_dir = clean_home / ".config" / "systemd" / "user"
    systemd_dir.mkdir(parents=True)
    (systemd_dir / "clean.service").write_text(
        "[Unit]\nDescription=Clean Service\n[Service]\nExecStart=/usr/bin/daemon\n",
        encoding="utf-8",
    )

    # Clean plangraph spec
    pg_dir = clean_home / ".local" / "share" / "plangraph"
    pg_dir.mkdir(parents=True)
    (pg_dir / "clean_spec.json").write_text('{"target": "/opt/clean"}', encoding="utf-8")

    # Clean bin router script
    bin_dir = clean_home / ".local" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "clean_router").write_text("#!/bin/sh\necho clean\n", encoding="utf-8")

    proc = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--home", str(clean_home)],
        capture_output=True,
        text=True,
    )

    assert proc.returncode == 0, f"Expected rc 0 on clean home, got {proc.returncode}"
    assert proc.stdout == ""
    assert scan_all(clean_home) == []


def test_secret_keywords_never_opened(tmp_path: Path) -> None:
    home = tmp_path / "secrets_home"
    pg_dir = home / ".local" / "share" / "plangraph"
    pg_dir.mkdir(parents=True)

    filenames = [
        "my_token.json",
        "secret.txt",
        "credentials.env",
        ".env",
        "id_rsa",
        "cert.pem",
        "password.conf",
        "signing.key",
    ]
    for fn in filenames:
        p = pg_dir / fn
        p.write_text(f"PRIVATE_DATA_{fn} /AutoOS/something", encoding="utf-8")

    hits = scan_all(home)
    assert len(hits) == len(filenames)
    for kind, path_str, detail in hits:
        assert detail == "SECRET-PATH"
        p = Path(path_str)
        assert looks_secret(p)


def test_tracked_repo_symlink(tmp_path: Path) -> None:
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

    hits = scan_all(home=home, repo=repo)
    assert len(hits) == 1
    kind, path_str, detail = hits[0]
    assert kind == "repo-symlink"
    assert path_str == str(link_autoos)
    assert detail == "/home/x/code/AutoOS/tools/helper.sh"


def test_router_bin_and_mcp_configs(tmp_path: Path) -> None:
    home = tmp_path / "extra_home"
    bin_dir = home / ".local" / "bin"
    bin_dir.mkdir(parents=True)

    # Symlink in bin
    (bin_dir / "tool_symlink").symlink_to("/AutoOS/bin/tool")
    # Script in bin
    (bin_dir / "tool_script").write_text(
        "#!/bin/bash\nexec /AutoOS/bin/real_tool \"$@\"\n", encoding="utf-8"
    )

    # MCP configs
    (home / ".claude.json").write_text(
        '{"mcpServers": {"test": {"command": "/AutoOS/mcp/srv"}}}\n',
        encoding="utf-8",
    )
    claude_dir = home / ".claude"
    claude_dir.mkdir(parents=True)
    (claude_dir / ".mcp.json").write_text(
        '{"server": "AutoOS/node_modules/srv"}\n',
        encoding="utf-8",
    )

    hits = scan_all(home)
    assert len(hits) == 4
    details = [h[2] for h in hits]
    assert "/AutoOS/bin/tool" in details
    assert 'exec /AutoOS/bin/real_tool "$@"' in details
    assert any('"command": "/AutoOS/mcp/srv"' in d for d in details)
    assert any('"server": "AutoOS/node_modules/srv"' in d for d in details)


def test_instruction_files_scanned(tmp_path: Path) -> None:
    home = tmp_path / "instr_home"
    claude = home / ".claude"
    (claude / "skills" / "s1").mkdir(parents=True)
    opencode = home / ".config" / "opencode"
    opencode.mkdir(parents=True)

    (claude / "AGENTS.md").write_text("Root: /home/x/code/AutoOS\n", encoding="utf-8")
    (claude / "CLAUDE.md").write_text("See /home/x/code/AutoOS/docs\n", encoding="utf-8")
    (claude / "notes.md").write_text("unit autoos-litellm.service restarts\n", encoding="utf-8")
    (opencode / "CLAUDE.md").write_text("%h/code/AutoOS\n", encoding="utf-8")
    # Skill markdown is only reached by the skill walker, not the instruction walker.
    (claude / "skills" / "s1" / "SKILL.md").write_text(
        "/home/x/code/AutoOS/skills/x\n", encoding="utf-8"
    )

    hits = scan_all(home)
    assert len(hits) == 5
    by_kind: dict[str, list[str]] = {}
    for kind, path_str, detail in hits:
        by_kind.setdefault(kind, []).append(f"{path_str}\t{detail}")
    assert sorted(by_kind) == ["instruction", "skill"]
    assert len(by_kind["instruction"]) == 4
    assert len(by_kind["skill"]) == 1
    assert any("autoos-litellm.service" in d for d in by_kind["instruction"])


def test_has_autoos_case_insensitive() -> None:
    for text in (
        "/home/x/code/AutoOS/tools",
        "AUTOOS/x",
        "autoos-litellm.service",
        "x-autoos-litellm.timer",
        "%h/code/autoos",
        "/var/AutoOS/",
    ):
        assert has_autoos(text), text
    for text in ("autoosx", "xautoos", "myautoostuff"):
        assert not has_autoos(text), text


def test_secret_names_token_boundaries(tmp_path: Path) -> None:
    for name in ("monkey", "keyboard", "monkey_patch.sh", "keyword.txt", "keyboards.json"):
        assert not looks_secret(tmp_path / name), name
    for name in (
        "api_key.json",
        "apikey.txt",
        "my_token.json",
        "passwd",
        "credentials.json",
        "id_rsa",
        "id_ed25519.pub",
        "cert.pem",
        "x.key",
        "y.p12",
        ".env",
    ):
        assert looks_secret(tmp_path / name), name


def test_redaction_on_matching_line(tmp_path: Path) -> None:
    home = tmp_path / "redact_home"
    claude = home / ".claude"
    (claude / "hooks").mkdir(parents=True)
    (claude / "settings.json").write_text(
        '{"hooks": {"PreToolUse": [{"command": "/home/x/code/AutoOS/h.py", '
        '"token": "sk-live-SECRETVALUE1234", '
        '"authorization": "Bearer abc.def.GHIJK"}]}}\n',
        encoding="utf-8",
    )
    (claude / "hooks" / "hook.sh").write_text(
        "#!/bin/sh\nexec /home/x/code/AutoOS/bin/run sk-live-ZZZZ9999 "
        + "A" * 400
        + "\n",
        encoding="utf-8",
    )

    proc = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--home", str(home)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 1
    out = proc.stdout
    for secret in ("SECRETVALUE1234", "abc.def.GHIJK", "ZZZZ9999"):
        assert secret not in out, secret
    assert out.count("<redacted>") >= 3
    assert "/home/x/code/AutoOS/h.py" in out
    for line in out.strip().splitlines():
        parts = line.split("\t")
        assert len(parts) == 3
        assert len(parts[2]) <= 160


def test_backups_and_jobs_excluded(tmp_path: Path) -> None:
    home = tmp_path / "excl_home"
    pg = home / ".local" / "share" / "plangraph"
    (pg / "backups").mkdir(parents=True)
    (pg / "backups" / "snap.json").write_text(
        '{"cmd": "/home/x/code/AutoOS/bin"}\n', encoding="utf-8"
    )
    jobs = home / ".claude" / "jobs" / "j1"
    jobs.mkdir(parents=True)
    (jobs / ".mcp.json").write_text(
        '{"server": "/home/x/code/AutoOS/srv"}\n', encoding="utf-8"
    )

    assert scan_all(home) == []


def test_timer_unit_flagged(tmp_path: Path) -> None:
    home = tmp_path / "timer_home"
    user = home / ".config" / "systemd" / "user"
    user.mkdir(parents=True)
    (user / "autoos-litellm.timer").write_text(
        "[Unit]\nDescription=Timer\n[Timer]\nExecStart=%h/code/AutoOS/bin/tick.sh\n",
        encoding="utf-8",
    )

    hits = scan_all(home)
    assert len(hits) == 1
    kind, path_str, detail = hits[0]
    assert kind == "systemd"
    assert path_str.endswith("autoos-litellm.timer")
    assert "%h/code/AutoOS/bin/tick.sh" in detail


def test_clean_home_exit_code_zero(tmp_path: Path) -> None:
    home = tmp_path / "spotless_home"
    claude = home / ".claude"
    claude.mkdir(parents=True)
    opencode = home / ".config" / "opencode"
    opencode.mkdir(parents=True)
    (claude / "AGENTS.md").write_text("See /usr/share/docs\n", encoding="utf-8")
    (claude / "CLAUDE.md").write_text("# Notes\nnothing here\n", encoding="utf-8")
    (opencode / "AGENTS.md").write_text("clean\n", encoding="utf-8")

    proc = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--home", str(home)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0
    assert proc.stdout == ""
    assert scan_all(home) == []
