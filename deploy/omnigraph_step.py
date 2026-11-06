"""Fail-soft, secret-silent Omnigraph bootstrap shared by both launchers."""
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import subprocess
import sys


def run(args, **kwargs):
    return subprocess.run(args, capture_output=True, text=True, timeout=60, **kwargs)


def operator(name):
    print('OPERATOR: place private ' + name + ' outside the repo')


def private_token(path):
    if os.name == 'nt':
        # Compare SIDs, not localized account names; never emit ACLs or paths.
        script = '''$a=Get-Acl -LiteralPath $env:PG_TOKEN_PATH;
$o=$a.GetOwner([System.Security.Principal.SecurityIdentifier]).Value;
$bad=@($a.GetAccessRules($true,$true,[System.Security.Principal.SecurityIdentifier]) |
Where-Object { $_.AccessControlType -eq 'Allow' -and $_.IdentityReference.Value -ne $o });
if ($bad.Count) { exit 1 }'''
        env = dict(os.environ, PG_TOKEN_PATH=str(path))
        if run(['powershell.exe', '-NoProfile', '-Command', script], env=env).returncode:
            raise ValueError('permissions')
        return path.read_text().strip()
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError('permissions')
        return stream.read().strip()


def update(path, text, mode=None):
    if path.exists() and path.read_text() == text:
        if mode is None or stat.S_IMODE(path.stat().st_mode) == mode:
            print('Omnigraph skipped: session file unchanged')
            return
    path.parent.mkdir(parents=True, exist_ok=True)
    if mode is not None:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        if os.name != 'nt':
            os.fchmod(fd, mode)
        with os.fdopen(fd, 'w') as stream:
            stream.write(text)
    else:
        path.write_text(text)
    print('Omnigraph session file installed')


def sessions(base, token, token_path, home):
    if os.name == 'nt':
        import winreg
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, 'Environment') as key:
            for name, value in [('OMNIGRAPH_BASE_URL', base), ('OMNIGRAPH_TOKEN', token)]:
                try:
                    old = winreg.QueryValueEx(key, name)[0]
                except FileNotFoundError:
                    old = None
                if old != value:
                    winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
                    print('Omnigraph user environment installed')
                else:
                    print('Omnigraph skipped: user environment unchanged')
        return
    profile = home / '.config/plangraph/omnigraph.profile.sh'
    # Disable shell tracing before expanding any private data, including on errors.
    reader = ('import os,stat,sys; f=os.open(sys.argv[1],os.O_RDONLY|os.O_NOFOLLOW); '
              's=os.fstat(f); '
              'assert stat.S_ISREG(s.st_mode) and s.st_uid==os.getuid() and not s.st_mode&63; '
              'print(os.read(f,65536).decode().strip())')
    text = ('case $- in *x*) set +x; _pg_trace=1;; esac\n'
            'export OMNIGRAPH_BASE_URL=' + shlex.quote(base) + '\n'
            'export OMNIGRAPH_TOKEN="$(python3 -c ' + shlex.quote(reader) + ' '
            + shlex.quote(str(token_path)) + ' 2>/dev/null)"\n'
            'if [ "${_pg_trace:-}" = 1 ]; then unset _pg_trace; set -x; fi\n')
    update(profile, text, 0o600)
    line = '[ ! -f "$HOME/.config/plangraph/omnigraph.profile.sh" ] || . "$HOME/.config/plangraph/omnigraph.profile.sh"'
    for path in [home / '.profile', home / '.zshenv']:
        if path.name == '.zshenv' and not path.exists():
            continue
        old = path.read_text() if path.exists() else ''
        update(path, old if line in old.splitlines() else old + ('\n' if old and not old.endswith('\n') else '') + line + '\n')


def main():
    if sys.argv[-1] == '1':
        print('Omnigraph dry-run: read site config; cache client; install session environment; verify health')
        return
    home = Path(os.environ.get('USERPROFILE', str(Path.home()))) if os.name == 'nt' else Path.home()
    site = Path(os.environ.get('PLANGRAPH_SITE_ENV', str(home / '.config/plangraph/site.env')))
    values = {}
    try:
        for line in site.read_text().splitlines():
            name, sep, value = line.partition('=')
            if sep and name.strip() in ('OMNIGRAPH_BASE_URL', 'OMNIGRAPH_TOKEN_FILE'):
                values[name.strip()] = value.strip().strip('"\'')
    except OSError:
        operator('PLANGRAPH_SITE_ENV (site.env with OMNIGRAPH_BASE_URL and OMNIGRAPH_TOKEN_FILE)')
        return
    root = Path(__file__).resolve().parents[1]
    spec = next(arg for arg in json.loads((root / '.mcp.json').read_text())['mcpServers']['omnigraph']['args'] if arg.startswith('@modernrelay/omnigraph-mcp@'))
    npm = shutil.which('npm.cmd' if os.name == 'nt' else 'npm')
    marker = home / '.config/plangraph/omnigraph-cache-spec'
    if not npm:
        print('Omnigraph skipped: npm unavailable')
    elif marker.exists() and marker.read_text() == spec:
        print('Omnigraph skipped: client already cached')
    elif run([npm, 'cache', 'add', spec]).returncode == 0:
        update(marker, spec, 0o600)
    else:
        print('Omnigraph skipped: client cache failed')
    for name in ('OMNIGRAPH_BASE_URL', 'OMNIGRAPH_TOKEN_FILE'):
        if not values.get(name):
            operator(name)
    if not all(values.get(n) for n in ('OMNIGRAPH_BASE_URL', 'OMNIGRAPH_TOKEN_FILE')):
        return
    try:
        token_path = Path(values['OMNIGRAPH_TOKEN_FILE']).expanduser()
        token = private_token(token_path)
        if not token or len(token.splitlines()) != 1:
            raise ValueError('one line required')
    except (OSError, ValueError):
        operator('OMNIGRAPH_TOKEN_FILE (owner-only, one-line token file)')
        return
    base = values['OMNIGRAPH_BASE_URL']
    if any(c in base + token for c in '\r\n'):
        operator('OMNIGRAPH_BASE_URL and OMNIGRAPH_TOKEN_FILE (single-line values)')
        return
    sessions(base, token, token_path, home)
    if not shutil.which('curl'):
        print('Omnigraph skipped: curl unavailable')
        return
    # MCP og.health() delegates to SDK GET /healthz. Never put secrets in argv.
    quote = lambda s: '"' + s.replace('\\', '\\\\').replace('"', '\\"') + '"'
    config = 'url = ' + quote(base.rstrip('/') + '/healthz') + '\nheader = ' + quote('Authorization: Bearer ' + token) + '\n'
    result = run(['curl', '--disable', '--config', '-', '--silent', '--output', os.devnull,
                  '--write-out', '%{http_code}', '--max-time', '15'], input=config)
    status = result.stdout.strip()
    print('ok' if status == '200' else status if re.fullmatch(r'[1-5][0-9]{2}', status) else 'Omnigraph skipped: health unavailable')
    if status == '404':
        print('Omnigraph skipped: health path unconfirmed; no alternate attempted')


if __name__ == '__main__':
    try:
        main()
    except Exception:
        print('Omnigraph skipped: setup unavailable')
