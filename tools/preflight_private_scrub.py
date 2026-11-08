#!/usr/bin/env python3
"""Check private words only on added lines between two committed trees."""
import argparse
import re
import subprocess

from split import scrub_check as sc


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', required=True)
    parser.add_argument('--head', required=True)
    parser.add_argument('--words', required=True)
    args = parser.parse_args(argv)
    try:
        cfg = sc.build_config(words_file=args.words)

        def git(*options):
            return subprocess.check_output(['git', *options], stderr=subprocess.DEVNULL)

        paths = git('diff', '--name-only', '-z', '--no-renames', '--diff-filter=AM',
                    args.base, args.head).split(b'\0')
        hits = []
        for raw in filter(None, paths):
            path = raw.decode('utf-8', 'replace')
            patch = git('diff', '--no-ext-diff', '--no-textconv', '--no-renames',
                        '--unified=0', args.base, args.head, '--', path)
            number = None
            for line in patch.decode('utf-8', 'replace').splitlines():
                hunk = re.match(r'^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@', line)
                if hunk:
                    number = int(hunk[1])
                elif number is not None and line.startswith('+'):
                    if any(word.search(line[1:]) for word in cfg['_words']):
                        hits.append(sc.Hit(path, number, 'private-word'))
                    number += 1
                elif number is not None and line.startswith(' '):
                    number += 1
        sc.report(hits, words=cfg['_words'])
        return int(bool(hits))
    except (OSError, ValueError, re.error, subprocess.CalledProcessError):
        print('private scrub: configuration or diff unavailable')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
