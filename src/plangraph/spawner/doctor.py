"""Host readiness: python3 -m plangraph.spawner.doctor [--json] [--probe].

Default checks never call a provider or deliver an event. --probe opts into
the existing liveness probes (up to three routed legs per role). Any failed
check makes the command exit 1; missing optional harnesses are skipped.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
from pathlib import Path
import shutil
import stat
import sys
import urllib.request

TIMEOUT = 10
_watchdog_values = set()


def load_watchdog_env():
    """Load service defaults silently, without replacing shell overrides."""
    try:
        lines = (Path.home() / '.config/plangraph/watchdog.env').read_text().splitlines()
    except (OSError, UnicodeError):
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = (part.strip() for part in line.split('=', 1))
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key and '\0' not in key and '\0' not in value and key not in os.environ:
            os.environ.setdefault(key, value)
            _watchdog_values.add(value)


def clean(value):
    from .runner import redact
    from .env_redact import redact_env_values
    text = redact_env_values(str(value), os.environ)
    # Also scrub unstructured subprocess/HTTP errors, including opaque tokens.
    for secret in sorted(_watchdog_values, key=len, reverse=True):
        if secret:
            text = re.sub(r'(?<![A-Za-z0-9_-])' + re.escape(secret)
                          + r'(?![A-Za-z0-9_-])', '[REDACTED]', text)
    try:
        secret = Path(os.environ['PG_TOKEN_FILE']).read_text().strip()
        if secret:
            text = text.replace(secret, '[REDACTED]')
    except (KeyError, OSError, UnicodeError):
        pass
    return ' '.join(redact(text).split())[:600]


async def mcp(module, command=None):
    child = await asyncio.create_subprocess_exec(
        *(command or [sys.executable, '-m', module]),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL)
    async def exchange():
        async def send(message):
            child.stdin.write((json.dumps(dict(jsonrpc='2.0', **message)) + '\n').encode())
            await child.stdin.drain()
        async def receive(identity):
            while True:
                line = await child.stdout.readline()
                if not line:
                    raise RuntimeError('MCP child exited before reply')
                reply = json.loads(line)
                if reply.get('id') == identity:
                    if 'error' in reply:
                        raise RuntimeError('MCP protocol error')
                    return reply['result']
        await send(dict(id=1, method='initialize', params=dict(
            protocolVersion='2024-11-05', capabilities={},
            clientInfo=dict(name='host-doctor', version='1'))))
        await receive(1)
        await send(dict(method='notifications/initialized'))
        await send(dict(id=2, method='tools/list', params={}))
        tools = (await receive(2)).get('tools', [])
        if not tools:
            raise ValueError('tools/list returned no tools')
        return 'pass', f'{len(tools)} tools'
    try:
        return await asyncio.wait_for(exchange(), TIMEOUT)
    finally:
        if child.returncode is None:
            try:
                child.kill()
            except ProcessLookupError:
                pass
        await child.wait()


def whoami():
    from .notify import _NoRedirectHandler
    url, filename = os.environ.get('PG_URL'), os.environ.get('PG_TOKEN_FILE')
    if not url or not filename:
        raise ValueError('PG_URL and PG_TOKEN_FILE required')
    with open(filename) as stream:
        if os.name == 'posix' and stat.S_IMODE(os.fstat(stream.fileno()).st_mode) & ~0o600:
            raise PermissionError('token file mode wider than 0600')
        token = stream.read().strip()
    if not token:
        raise ValueError('empty token file')
    request = urllib.request.Request(url.rstrip('/') + '/v1/whoami',
                                     headers={'Authorization': 'Bearer ' + token})
    with urllib.request.build_opener(_NoRedirectHandler()).open(request, timeout=TIMEOUT) as response:
        if response.status != 200:
            raise ValueError(f'HTTP {response.status}')
        identity = json.load(response)
    return 'pass', f"principal={identity['name']} role={identity['role']}"


def completion():
    from .notify import KeeperSink, KeeperNotConfigured
    sink = KeeperSink()
    if not sink.url or not sink.token_file:
        raise KeeperNotConfigured('completion requires PG_URL and PG_TOKEN_FILE')
    return 'pass', 'keeper completion sink configured'


async def harness(name):
    binary = shutil.which(name)
    if binary is None:
        return 'skip', 'not on PATH'
    child = await asyncio.create_subprocess_exec(binary, '--version',
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        output, _ = await asyncio.wait_for(child.communicate(), TIMEOUT)
        if child.returncode:
            raise RuntimeError(f'version exited {child.returncode}')
        return 'pass', (output.decode(errors='replace').splitlines() or [''])[0]
    finally:
        if child.returncode is None:
            try:
                child.kill()
            except ProcessLookupError:
                pass
        await child.wait()


async def diagnose(probe=False):
    from plangraph.planner.legs import HARNESSES
    from .service import SpawnService
    from .leg_probe import probe_many
    checks, plans = [], {}

    async def check(name, operation):
        try:
            result = operation()
            status, detail = await result if hasattr(result, '__await__') else result
        except Exception as exc:
            status, detail = 'fail', f'{type(exc).__name__}: {exc}'
        checks.append(dict(name=name, status=status, detail=clean(detail)))

    for component in ('spawner', 'keeper'):
        await check('mcp_' + component, lambda: mcp('plangraph.' + component + '.mcp'))
    await check('keeper_whoami', whoami)
    await check('completion_delivery', completion)
    for name in HARNESSES:
        await check('harness_' + name, lambda: harness(name))
    if not any(shutil.which(name) for name in HARNESSES):
        await check('harness_any', lambda: ('fail', 'no harness on PATH'))

    service = None
    def route(role):
        nonlocal service
        if service is None:
            service = SpawnService()
        plan = service.route(dict(title='Host readiness', estimate_tokens=100,
                                  tier='STD'), role=role,
                             writer_families=('openai',) if role == 'review' else ())
        if not plan.legs:
            raise ValueError('no routed leg')
        plans[role] = [leg.id for leg in plan.legs[:3]]
        return 'pass', plan.legs[0].id

    async def probe_role(role):
        if not probe:
            return 'skip', 'enable with --probe'
        if role not in plans:
            raise ValueError('no routed legs to probe')
        result = await probe_many(service.runner, legs=plans[role])
        rows = result['probes']
        outcomes = {row['id']: row for row in rows}
        detail = '; '.join(leg + ': ' + ('answered' if outcomes.get(leg, {}).get('ok')
            else str(outcomes.get(leg, {}).get('reason', 'no probe result'))) for leg in plans[role])
        return ('pass' if any(outcomes.get(leg, {}).get('ok') for leg in plans[role]) else 'fail'), detail

    for role in ('implement', 'review'):
        await check('route_' + role, lambda: route(role))
        await check('probe_' + role, lambda: probe_role(role))
    return dict(checks=checks, ok=not any(row['status'] == 'fail' for row in checks))


def main(argv=None):
    load_watchdog_env()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--probe', action='store_true')
    args = parser.parse_args(argv)
    result = asyncio.run(diagnose(args.probe))
    if args.json:
        print(json.dumps(result))
    else:
        for row in result['checks']:
            print(f"{row['name']}: {row['status']} {row['detail']}")
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    sys.exit(main())
