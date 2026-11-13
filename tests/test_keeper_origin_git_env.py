"""The isolated decisions fetch env keeps global credentials, drops other overrides."""
import os
import subprocess


def test_origin_env_carries_global_credentials_and_drops_other_overrides(tmp_path, monkeypatch):
    from plangraph.keeper import evidence_decisions as ed
    monkeypatch.chdir(tmp_path)

    for name in [n for n in os.environ if n.startswith("GIT_")]:
        monkeypatch.delenv(name)
    config = tmp_path / "user.gitconfig"
    config.write_text(
        '[credential]\n\thelper = store --file=/nonexistent/creds\n'
        '[credential "https://github.com"]\n\tusername = someone\n'
        '[url "/elsewhere/"]\n\tinsteadOf = https://github.com/\n', encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "nope"))
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
    env = ed._origin_git_env()
    count = int(env["GIT_CONFIG_COUNT"])
    pairs = {env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"] for i in range(count)}
    assert pairs == {"credential.helper": "store --file=/nonexistent/creds"}
    assert env["GIT_CONFIG_GLOBAL"] == os.devnull and env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert "GIT_DIR" not in env and env["GIT_TERMINAL_PROMPT"] == "0"
    # git itself sees the helper but not the unrelated url rewrite
    seen = subprocess.run(["git", "config", "--list"], env=env, capture_output=True,
                          text=True, check=True).stdout
    assert "credential.helper=store" in seen and "insteadof" not in seen.lower()


def test_origin_env_without_global_credentials_adds_no_config_entries(tmp_path, monkeypatch):
    from plangraph.keeper import evidence_decisions as ed
    monkeypatch.chdir(tmp_path)

    for name in [n for n in os.environ if n.startswith("GIT_")]:
        monkeypatch.delenv(name)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    assert "GIT_CONFIG_COUNT" not in ed._origin_git_env()
