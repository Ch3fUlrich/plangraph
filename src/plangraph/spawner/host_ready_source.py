"""Probe the source paths actually configured for spawner and remote ps."""
import shlex

from .remote_ps import configured_hosts


def check_sources(name, config, command, fail):
    sources = [(config['ssh'], config['spawner_src'], True)]
    ok = True
    try:
        for host in configured_hosts():
            if host['name'] != name:
                continue
            if host.get('invalid') or 'win_src' in host or 'wsl_distro' in host:
                fail('src_config', 'unsupported_or_invalid')
                ok = False
                continue
            sources.append((host['target'], host.get('src'), False))
    except (OSError, ValueError, KeyError, TypeError):
        fail('src_config', 'unreadable')
        ok = False
    for target, source, checkout in dict.fromkeys(sources):
        if target != config['ssh'] or not source:
            fail('src_config', 'route_or_path_unknown')
            ok = False
            continue
        # Expand only the conventional home prefix, never shell syntax from config.
        path = ('"$HOME"/' + shlex.quote(source[2:]) if source.startswith('~/')
                else shlex.quote(source))
        import_path = path + '/src' if checkout else path
        script = (f'if [ ! -d {path} ]; then echo src_missing; '
                  f'elif (cd / && PYTHONPATH={import_path} python3 -c '
                  + shlex.quote('import plangraph') +
                  ') >/dev/null 2>&1; then echo ok; else echo src_import; fi')
        result = command('src', script)
        if result is None:
            ok = False
        elif result.strip() != 'ok':
            label = result.strip() if result.strip() in ('src_missing', 'src_import') else 'src'
            fail(label, 'failed')
            ok = False
    return ok
