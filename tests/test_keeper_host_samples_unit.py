"""Host-sample defaults and request parsing that need no database."""
from io import BytesIO
from types import SimpleNamespace

import pytest

from plangraph.keeper import api, store_host_samples
from plangraph.keeper.errors import Refused


def test_range_default_row_cap():
    assert store_host_samples.RANGE_ROW_CAP == 10000


@pytest.mark.parametrize("raw", [b"", b"null", b" \t\r\nnull\n\r\t "])
def test_null_body_is_empty(raw):
    request = SimpleNamespace(headers={"Content-Length": str(len(raw))},
                              rfile=BytesIO(raw))
    assert api.KeeperHandler._read_body(request) == {}


@pytest.mark.parametrize("raw", [b"\x0cnull\x0c", b"\x0bnull\x0b"])
def test_non_json_whitespace_is_invalid(raw):
    request = SimpleNamespace(headers={"Content-Length": str(len(raw))},
                              rfile=BytesIO(raw))
    with pytest.raises(Refused, match="body is not valid JSON"):
        api.KeeperHandler._read_body(request)


def test_postgres_dsn_is_read_from_the_ci_variable():
    """CI exports PG_DSN; the host-sample modules must not skip with only that set."""
    import os
    import subprocess
    import sys
    from pathlib import Path
    env = {k: v for k, v in os.environ.items() if k not in ("PG_DSN", "PLANGRAPH_TEST_PG_DSN")}
    env["PG_DSN"] = "postgresql://synthetic/keeper"
    tests = str(Path(__file__).resolve().parent)
    env["PYTHONPATH"] = os.pathsep.join([tests, *sys.path])
    out = subprocess.run(
        [sys.executable, "-c",
         "import test_keeper_host_samples as m; print(m.DSN)"],
        env=env, capture_output=True, text=True, check=True).stdout.strip()
    assert out == "postgresql://synthetic/keeper"
