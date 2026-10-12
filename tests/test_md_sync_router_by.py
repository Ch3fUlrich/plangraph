"""md-sync reads router D-rows whose 'by' cell says router OR routing-00 (D-722)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import md_sync_dclose as m  # noqa: E402

TEXT = """| D | scope | decision | by | src |
|---|---|---|---|---|
| D-901 | x | a. Closes: demo:A | router | t |
| D-902 | x | b. Closes: demo:B | routing-00 | t |
| D-903 | x | c. Closes: demo:C | operator | t |
"""


def test_router_rows_accept_both_spellings():
    nums = sorted(r[0] for r in m.router_drows(TEXT))
    assert nums == [901, 902]
