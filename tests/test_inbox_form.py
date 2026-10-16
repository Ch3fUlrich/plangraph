import re

from plangraph.inbox.form import PAGE


def test_page_is_self_contained_and_safe():
    assert isinstance(PAGE, str)
    assert PAGE.startswith("<!doctype html>")
    assert len(PAGE.encode()) < 8192
    for banned in ("innerHTML", "localStorage", "sessionStorage", "document.cookie",
                   "http://", "https://", "<link", "setInterval", "setTimeout"):
        assert banned not in PAGE, banned
    assert not re.search(r"\bon\w+\s*=", PAGE)
    assert not re.search(r"\bsrc\s*=", PAGE)
    assert "b.msg" in PAGE
    assert "/v1/questions" in PAGE
    assert "/v1/answer_question" in PAGE
    assert 'type="password"' in PAGE
