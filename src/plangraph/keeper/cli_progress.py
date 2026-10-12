"""``pg progress`` (PROGRESS-PANEL): the standing per-project progress view."""
from __future__ import annotations

from .. import views_progress
from . import fmt
from .cli_verbs import _emit, read


def cmd_progress(args, session):
    def local():
        return _emit(session, views_progress.progress(session.local_items(), project=args.project),
                     fmt.progress_text)
    return read(session, "progress", {"project": args.project}, fmt.progress_text, local)


def add_parser(sub, make):
    p = make(sub, "progress", help="percent done per project/phase, hours left, running items")
    p.add_argument("--project")
