"""LAND-MODE-SETTING: the one setting ``landing.mode`` and its resolution per repository.

Mutations killed: an unknown mode accepted, a repository table ignored, the repository override
losing to the global value, a default that differs between callers, a mode list that drops one
of the three paths.
"""
import json

import pytest

from plangraph import settings


def write(tmp_path, data):
    (tmp_path / ".git").mkdir(exist_ok=True)
    (tmp_path / "plangraph.json").write_text(json.dumps(data))


@pytest.fixture
def policy(tmp_path, monkeypatch):
    monkeypatch.setenv("PLANGRAPH_SETTINGS", "plangraph.json")
    monkeypatch.setenv("PLANGRAPH_REPO_ROOT", str(tmp_path))
    return tmp_path


def test_the_three_modes_exist_and_tested_ff_is_the_recommended_one():
    assert settings.LANDING_MODES == ("tested-ff", "train-pr-merge", "single-pr")
    assert settings.RECOMMENDED_LANDING_MODE == "tested-ff"
    assert settings.DEFAULT_LANDING_MODE in settings.LANDING_MODES


def test_without_a_settings_file_the_default_applies(monkeypatch):
    monkeypatch.delenv("PLANGRAPH_SETTINGS", raising=False)
    assert settings.landing_mode("owner/proj") == settings.DEFAULT_LANDING_MODE


@pytest.mark.parametrize("mode", settings.LANDING_MODES)
def test_the_global_mode_is_chosen_by_the_setting(policy, mode):
    write(policy, {"landing": {"mode": mode}})
    assert settings.landing_mode("owner/proj") == mode


def test_a_repository_table_overrides_the_global_mode_for_that_repository_only(policy):
    write(policy, {"landing": {"mode": "tested-ff", "repos": {"owner/proj": "train-pr-merge"}}})
    assert settings.landing_mode("owner/proj") == "train-pr-merge"
    assert settings.landing_mode("owner/other") == "tested-ff"
    assert settings.landing_mode() == "tested-ff"


def test_changing_the_file_changes_the_mode_without_a_code_change(policy):
    write(policy, {"landing": {"mode": "tested-ff"}})
    assert settings.landing_mode("owner/proj") == "tested-ff"
    write(policy, {"landing": {"mode": "single-pr"}})
    assert settings.landing_mode("owner/proj") == "single-pr"


@pytest.mark.parametrize("bad", [
    {"landing": {"mode": "fast-forward"}},
    {"landing": {"mode": ""}},
    {"landing": {"mode": 3}},
    {"landing": {"repos": {"owner/proj": "merge"}}},
    {"landing": {"repos": ["owner/proj"]}},
    {"landing": "tested-ff"},
    {"landing": {"mode": "tested-ff", "other": 1}},
])
def test_an_unknown_mode_or_shape_is_refused(policy, bad):
    write(policy, bad)
    with pytest.raises(settings.SettingsError, match="landing"):
        settings.landing_mode("owner/proj")


def test_a_bad_repository_name_is_refused(policy):
    write(policy, {})
    with pytest.raises(settings.SettingsError, match="repository"):
        settings.landing_mode("not a repo")
