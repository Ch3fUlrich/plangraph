#!/usr/bin/env python3
"""Export public combo fields through the gateway API only."""
import argparse
import ipaddress
import json
from pathlib import Path
import sys
import urllib.parse
import urllib.request


def normalize(data):
    rows = data['combos'] if isinstance(data, dict) else data
    result = []
    for row in rows:
        name, members = row['name'], row.get('members', row.get('models'))
        if not isinstance(name, str) or not name or not isinstance(members, list) or not members:
            raise ValueError()
        if not all(isinstance(m, str) and m for m in members):
            raise ValueError()
        public = {'name': name, 'members': members}
        if 'strategy' in row:
            if not isinstance(row['strategy'], str):
                raise ValueError()
            public['strategy'] = row['strategy']
        result.append(public)
    if len({r['name'] for r in result}) != len(result):
        raise ValueError()
    return {'combos': sorted(result, key=lambda r: r['name'])}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--gateway', required=True)
    parser.add_argument('--key-file', required=True, type=Path)
    parser.add_argument('--output', type=Path, default=Path(__file__).parent / 'combo-set/combos.json')
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    try:
        url = urllib.parse.urlsplit(args.gateway)
        if url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError()
        if url.scheme == 'http' and not ipaddress.ip_address(url.hostname).is_loopback:
            raise ValueError()
        key = args.key_file.read_text().strip()
        if not key or any(c.isspace() or ord(c) < 33 or ord(c) > 126 for c in key):
            raise ValueError()
        request = urllib.request.Request(args.gateway.rstrip('/') + '/api/combos', headers={'Authorization': 'Bearer ' + key})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        with opener.open(request, timeout=15) as response:
            data = normalize(json.load(response))
        if key in json.dumps(data, ensure_ascii=True):
            raise ValueError()
        if args.check:
            return int(json.loads(args.output.read_text()) != data)
        args.output.write_text('{"combos": [\n' + ',\n'.join('  ' + json.dumps(r) for r in data['combos']) + '\n]}\n')
        return 0
    except Exception:
        print('Combo export refused or failed.', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
