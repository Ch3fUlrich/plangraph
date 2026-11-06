"""Register 49: every curl download in the workflows retries transient failures."""
import re
from pathlib import Path

import pytest

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"
RETRY = "--retry 5 --retry-all-errors"


def downloads(text):
    """Curl commands that write a file, continuation lines joined, comments ignored."""
    joined = re.sub(r"\\\n\s*", " ", text)
    found = []
    for line in joined.splitlines():
        body = line.strip()
        if body.startswith("#") or not re.match(r"(?:-\s+run:\s+)?curl\b", body):
            continue
        if re.search(r"\s(?:-o|--output)\s", body) or re.search(r"\s>\s*\S", body):
            found.append(body)
    return found


def missing_retry(text):
    return [c for c in downloads(text) if RETRY not in c or len(re.findall(r"--retry\s", c)) != 1]


def test_repository_downloads_retry():
    files = list(WORKFLOWS.glob("*.yml")) + list(WORKFLOWS.glob("*.yaml"))
    commands = [c for f in files for c in downloads(f.read_text())]
    assert len(commands) >= 5
    bad = [c for f in files for c in missing_retry(f.read_text())]
    assert not bad, bad


@pytest.mark.parametrize("text", [
    'curl -fsSL -o "$dir/a" \\\n   https://example.test/a\n',
    'curl -fsSL --retry 5 -o a https://example.test/a\n',
    'curl -fsSL --retry-all-errors -o a https://example.test/a\n',
    'curl -fsSL --retry 2 --retry-all-errors -o a https://example.test/a\n',
    'curl -fsSL --retry 5 --retry-all-errors --retry 1 -o a https://example.test/a\n',
    'curl -fsSL https://example.test/a > a\n',
])
def test_bad_download_is_named(text):
    assert missing_retry(text)


@pytest.mark.parametrize("text", [
    "# never curl -o a https://example.test/a\n",
    "curl -fsS https://example.test/health\n",
    'curl -fsSL --retry 5 --retry-all-errors -o a https://example.test/a\n',
])
def test_allowed_lines_pass(text):
    assert not missing_retry(text)
