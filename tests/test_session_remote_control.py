"""RC-required launches have bounded, fake-only bridge checks on both platforms."""
import json
import pytest
from sessions_support import site, FakeRun, UUID1, make, go, write_config


@pytest.mark.parametrize('platform', ['linux', 'win32'])
@pytest.mark.parametrize('bridge', [None, 'bridge-fixture'])
def test_required_rc_launch_is_bounded(site, platform, bridge):
    config = json.loads(site.cfg.read_text())
    config['require_remote_control'] = True
    write_config(site.cfg, json.dumps(config))
    run = FakeRun(agents_rows=[{'sessionId': UUID1, 'state': 'working', 'bridgeSessionId': bridge}])
    service = make(site, run, platform=platform, which=lambda _: str(site.tmp_path / 'claude.exe'), resolve_budget=1)
    result = go(site, service)
    record = json.loads((site.state / 'sessions/alpha.json').read_text())
    if bridge:
        assert result['ok'] is True
        assert record['bridgeSessionId'] == bridge
    else:
        assert result['ok'] is False
        assert 'RC-MISSING' in result['error']
        assert record['state'] == 'rc_missing'
        assert record['session_id'] == UUID1
    assert len(run.calls) <= 4
