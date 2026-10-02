"""TOK scoping — router decision D-276 (binding).

``Waives:`` and ``Closes:`` tokens count ONLY inside the DECISION cell (3rd
column) of their D-row.  The decisions-log table header is
``| id | project | decision | by | why / evidence |``, so a token quoted in
the project, by, or why/evidence cell carries no authority.
"""

from plangraph.keeper.evidence_decisions import DecisionsAdapter

HEADER = ("| id | project | decision | by | why / evidence |\n"
          "|---|---|---|---|---|\n")
DID = "D-276"
KEY = "demo:A"


def _adapter(tmp_path, body):
    path = tmp_path / "DECISIONS.md"
    path.write_text(HEADER + body, encoding="utf-8")
    return DecisionsAdapter(path)


def _row(project="demo", decision="-", by="router", why="-", did=DID):
    return f"| {did} | {project} | {decision} | {by} | {why} |\n"


def test_token_in_decision_cell_matches(tmp_path):
    adapter = _adapter(tmp_path, _row(decision="Waives: demo:A"))
    assert adapter.waives(DID, KEY) is True
    closer = _adapter(tmp_path, _row(decision="Closes: demo:A"))
    assert closer.closes(DID) == {"demo:A"}


def test_token_only_in_evidence_cell_does_not_match(tmp_path):
    body = _row(decision="-", why="waived per Waives: demo:A, see thread")
    assert _adapter(tmp_path, body).waives(DID, KEY) is False
    assert _adapter(tmp_path, _row(
        decision="-", why="Closes: demo:A on request")).closes(DID) == set()


def test_token_in_project_cell_does_not_match(tmp_path):
    body = _row(project="Waives: demo:A")
    assert _adapter(tmp_path, body).waives(DID, KEY) is False
    assert _adapter(tmp_path, body).closes(DID) == set()


def test_token_in_by_cell_does_not_match(tmp_path):
    body = _row(by="router Waives: demo:A")
    assert _adapter(tmp_path, body).waives(DID, KEY) is False
    assert _adapter(tmp_path, body).closes(DID) == set()


def test_several_tokens_in_decision_cell_each_match(tmp_path):
    adapter = _adapter(tmp_path, _row(decision="Waives: demo:A, demo:B"))
    assert adapter.waives(DID, "demo:A") is True
    assert adapter.waives(DID, "demo:B") is True
    assert adapter.waives(DID, "demo:C") is False
    closer = _adapter(tmp_path, _row(decision="Closes: demo:Z, demo:A"))
    assert closer.closes(DID) == {"demo:Z", "demo:A"}


def test_whole_token_rules_still_hold_in_decision_cell(tmp_path):
    nospace = _adapter(tmp_path, _row(decision="Waives:demo:A"))
    assert nospace.waives(DID, "demo:A") is True
    prefixed = _adapter(tmp_path, _row(decision="Waives: demo:AB"))
    assert prefixed.waives(DID, "demo:A") is False
    assert prefixed.waives(DID, "demo:AB") is True
    assert prefixed.closes(DID) == set()
    listed = _adapter(tmp_path, _row(decision="Closes: demo:B, demo:A"))
    assert listed.closes(DID) == {"demo:B", "demo:A"}
