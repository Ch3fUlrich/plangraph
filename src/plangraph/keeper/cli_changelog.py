"""``pg changelog --since <date|seq>`` (PG-CHANGELOG-GEN): creates, closes, answers, restarts."""
from __future__ import annotations

from . import changelog
from .cli_verbs import read


def cmd_changelog(args, session):
    return read(session, "changelog", {"since": args.since}, changelog.render)


def add_parser(sub, make):
    p = make(sub, "changelog", help="what changed since a date or event seq (creates, closes, answers, restarts)")
    p.add_argument("--since", required=True, metavar="DATE|SEQ",
                   help="YYYY-MM-DD / ISO datetime (inclusive, UTC) or an event seq (exclusive)")
