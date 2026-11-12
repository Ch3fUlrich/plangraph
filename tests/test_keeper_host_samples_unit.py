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
