"""Host admission lists the run's own config, not the shared background server's (register 47)."""
import os

import pytest

from plangraph.routing_support import resolver

pytestmark = [pytest.mark.real_probe, pytest.mark.skipif(os.name == 'nt', reason='sh stub')]
MODEL = 'omniroute/cx-gpt-6.1-sol'
# `opencode models` answers from the background server, which ignores OPENCODE_CONFIG_CONTENT; only --standalone reads it.
FAKE_OPENCODE = f"""#!/bin/sh
case " $* " in
  *" --standalone "*) echo {MODEL}; echo opencode/big ;;
  *) echo opencode/big ;;
esac
"""


@pytest.fixture
def host(tmp_path, monkeypatch):
    fake = tmp_path / 'bin' / 'opencode'
    fake.parent.mkdir()
    fake.write_text(FAKE_OPENCODE)
    fake.chmod(0o755)
    (tmp_path / 'opencode.jsonc').write_text('{"providers": {"omniroute": {"env": ["PLANGRAPH_OMNIROUTE_KEY"], "models": {}}}}')
    monkeypatch.setenv('AUTOOS_ROOT', str(tmp_path))
    monkeypatch.setenv('PATH', f"{fake.parent}{os.pathsep}{os.environ['PATH']}")
    resolver.served_models.cache_clear()
    yield {'OMNIROUTE_BASE_URL': 'http://127.0.0.1:9/v1'}
    resolver.served_models.cache_clear()


def test_probe_reads_the_standalone_listing(host):
    assert MODEL in resolver._probe_served_models('{"providers": {}}', env=dict(os.environ))


def test_gateway_leg_admitted_though_the_background_server_lacks_it(host, real_served_models):
    assert resolver.host_skip_reason({'harness': 'opencode', 'model': MODEL}, env=host) is None


def test_model_the_config_does_not_serve_is_still_refused(host, real_served_models):
    reason = resolver.host_skip_reason({'harness': 'opencode', 'model': 'omniroute/not-there'}, env=host)
    assert reason == 'not_served_on_host: omniroute/not-there'
