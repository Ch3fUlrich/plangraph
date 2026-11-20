#!/usr/bin/env python3
"""Compatibility entry point: public rules, shapes and private words on committed changes."""
import argparse
from pathlib import Path

from split import scrub_check as sc


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', required=True)
    parser.add_argument('--head', required=True)
    parser.add_argument('--words', required=True)
    args = parser.parse_args(argv)
    split = Path(__file__).resolve().parent / 'split'
    # sc.main uses sc.scan_content, also used by CI and pre-push.
    return sc.main(['.', '--base', args.base, '--head', args.head,
                    '--rules', str(split / 'manifest.toml'),
                    '--rules', str(split / 'scrub_ci.toml'), '--words', args.words])


if __name__ == '__main__':
    raise SystemExit(main())
