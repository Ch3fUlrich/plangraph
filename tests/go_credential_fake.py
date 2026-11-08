import pytest


@pytest.fixture
def go_credential(monkeypatch):
    """CI holds no opencode Go key: an isolated review of an opencode-go model needs one installed."""
    monkeypatch.setattr('plangraph.spawner.opencode_isolation.install_go_credential', lambda job_dir: True)
