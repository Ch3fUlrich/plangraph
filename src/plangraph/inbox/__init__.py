"""Question/answer inbox for the keeper (RT-P1-INBOX-CODE).

* :mod:`plangraph.inbox.routes` - the ``questions``, ``ask_question``,
  ``answer_question`` and ``resignal_question`` HTTP doors, thin over the
  existing ``Store.ask_question`` / ``Store.answer_question`` verbs.
* :mod:`plangraph.inbox.temporal_signal` - the stdlib-only Temporal HTTP-API
  client that wakes a waiting workflow when a question is answered.
* :mod:`plangraph.inbox.form` - the static ``GET /inbox`` page.

The keeper runs no question deadline timer: that timer lives only in Temporal.
"""
from __future__ import annotations

from . import routes, temporal_signal  # noqa: E402

__all__ = ["routes", "temporal_signal"]
