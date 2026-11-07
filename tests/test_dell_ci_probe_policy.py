"""Dell-only fail-closed policy and marker propagation regression tests."""
import subprocess

import pytest

from test_dell_ci_isolation import real_sandbox
from test_dell_ci_run import Fake, invoke


@pytest.mark.parametrize("dell", [True, False])
@pytest.mark.parametrize("missing", [True, False])
def test_real_sandbox_requires_bwrap(tmp_path, monkeypatch, dell, missing):
    if dell:
        monkeypatch.setenv("PG_DELL_CI_HOST", "1")
    else:
        monkeypatch.delenv("PG_DELL_CI_HOST", raising=False)
    monkeypatch.setattr("shutil.which", lambda _: None if missing else "/usr/bin/bwrap")
    monkeypatch.setattr(subprocess, "check_output", lambda *a, **kw: "/usr\n")
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw:
                        subprocess.CompletedProcess(a, 1, "", "nested namespace denied"))
    reason = ("bwrap unavailable" if missing else "bwrap probe failed: nested namespace denied")
    if not dell:
        reason = r"real bwrap probes run only in Dell CI \(PG_DELL_CI_HOST\)"
    with pytest.raises(pytest.fail.Exception if dell else pytest.skip.Exception, match=reason):
        real_sandbox.__wrapped__(tmp_path)


def test_dell_pytest_marker_inside_sandbox(tmp_path):
    fake = Fake()
    code, _ = invoke(tmp_path, fake, jobs="pytest,browser")
    assert code == 0
    commands = [c for c in fake.calls if c.startswith("timeout ")]
    assert "--clearenv" in commands[0]
    assert "PG_DELL_CI_HOST=1 python3 -m pytest" in commands[0]
    assert "PG_DELL_CI_HOST" not in commands[1]
