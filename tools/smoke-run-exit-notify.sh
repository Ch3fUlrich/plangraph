#!/usr/bin/env bash
# Build-L1 only: real local harness child, live keeper write and read-back.
set -euo pipefail
: "${PG_URL:?set live keeper URL}"
: "${PG_TOKEN_FILE:?set spawner-role token file}"
: "${PLANGRAPH_SPAWN_ITEM:?set claimed/in-review item key}"
export PYTHONPATH="${PWD}/src${PYTHONPATH:+:$PYTHONPATH}"
python3 - <<'PY'
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from unittest.mock import patch

from plangraph.spawner.runner import SpawnerRunner, spawn_service


async def main():
    # A real, cheap harness child, not --dry-run (which starts no process).
    # Keep records for build-L1 diagnostics; never print credentials.
    state = Path(tempfile.mkdtemp(prefix='.notify-smoke-', dir=Path.cwd()))
    service = spawn_service(runner=SpawnerRunner(), state=state)
    with patch('plangraph.spawner.runner.build_command',
               return_value=[sys.executable, '-c', 'print("notify smoke OK")']):
        record = await service.spawn(
            card={'estimate_tokens': 1000},
            prompt='Reply OK only', model='omniroute/cx-gpt-6.1-sol',
            override_reason='build-L1 cheap local harness notification smoke', wait_seconds=0)
        record = await service.wait(record['run_id'], timeout=30)
    if record['exit_code'] != 0:
        raise RuntimeError('smoke child failed: ' + json.dumps({k: record.get(k) for k in ('status', 'exit_code', 'reason', 'error', 'events')}, default=str)[:1500])
    marker = state / 'runs' / record['run_id'] / 'keeper.delivered'
    token = Path(os.environ['PG_TOKEN_FILE']).read_text().strip()
    query = urllib.parse.urlencode({'key': os.environ['PLANGRAPH_SPAWN_ITEM']})
    request = urllib.request.Request(os.environ['PG_URL'].rstrip('/') + '/v1/show?' + query,
                                   headers={'Authorization': 'Bearer ' + token})
    deadline = time.monotonic() + float(os.environ.get('PLANGRAPH_NOTIFY_SMOKE_TIMEOUT', '30'))
    while True:
        service.retry_completions()
        with urllib.request.urlopen(request, timeout=10) as response:
            info = json.load(response)
        # show.run_ids is projected from the keeper's run_recorded item event.
        if marker.is_file() and record['run_id'] in info.get('run_ids', []):
            print(json.dumps({'run_id': record['run_id'], 'state': str(state),
                              'keeper_delivered': True, 'keeper_read_back': True}))
            return
        if time.monotonic() >= deadline:
            raise RuntimeError('missing keeper marker or live run_recorded read-back')
        await asyncio.sleep(.2)


asyncio.run(main())
PY
