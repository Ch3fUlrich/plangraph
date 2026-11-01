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

    lines = [line for line in out.strip().splitlines() if line]
    assert len(lines) == 4
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
