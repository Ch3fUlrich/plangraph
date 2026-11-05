import gen_windows_baseline as baseline
import pytest
from gen_windows_baseline import generate, MARKER


def test_scrub_and_refusal(tmp_path, monkeypatch):
    monkeypatch.setenv("USERNAME", "fake-person")
    monkeypatch.setenv("COMPUTERNAME", "fake-machine")
    out, log = tmp_path / "record", tmp_path / "log"
    log.write_text("FAILED tests/example.py::test_example\n1 failed, 1 passed, 219 skipped in 1.00s\n")
    out.write_text("fake-person fake-machine C:\\Users\\fake-person /c/Users/fake-person /home/fake-person /Users/fake-person\n" + MARKER)
    generate(log, "abc", out)
    assert "fake-" not in out.read_text() and "<home>" in out.read_text()
    assert "tests/example.py::test_example | owner: none yet" in out.read_text()
    original = baseline.re.sub
    for content in ("x" * 40961, "C:/Users/residue"):
        if content.startswith("C:"):
            monkeypatch.setattr(baseline.re, "sub", lambda pattern, replacement, text, **kw: text if pattern == baseline.PATHS else original(pattern, replacement, text, **kw))
        out.write_text(content + MARKER)
        before = out.read_bytes()
        with pytest.raises(ValueError):
            generate(log, "abc", out)
        assert out.read_bytes() == before
