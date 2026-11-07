import importlib.util
import json
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location("claude_usage_compare", Path(__file__).parents[1] / "tools/claude_usage.py")
usage = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(usage)


@pytest.fixture
def projects(tmp_path):
    project = tmp_path / "projects" / "example"
    project.mkdir(parents=True)
    for role, amounts in {"shared": [("01", 1000000), ("02", 3000000)],
                          "before-only": [("01", 3000000)],
                          "after-only": [("02", 1000000)]}.items():
        records = [dict(type="custom-title", customTitle=role)]
        for day, amount in amounts:
            records.append(dict(type="assistant", timestamp=f"2026-01-{day}T00:00:00Z",
                                message=dict(id=day, usage=dict(input_tokens=amount))))
        (project / f"{role}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in records))
    return project.parent


def run(projects, capsys, before="2026-01-01", after="2026-01-02", json_output=True):
    args = ["--projects", str(projects), "compare", "--before-since", before,
            "--before-until", "2026-01-02", "--after-since", after,
            "--after-until", "2026-01-03"]
    assert usage.main(args + (["--json"] if json_output else [])) == 0
    output = capsys.readouterr().out
    return json.loads(output) if json_output else output


def test_compare_windows_and_json_shape(projects, capsys):
    result = run(projects, capsys)
    assert result == [
        dict(role="shared", before_weighted_m=1, before_share_pct=25,
             after_weighted_m=3, after_share_pct=75, share_delta_points=50),
        dict(role="after-only", before_weighted_m=0, before_share_pct=0,
             after_weighted_m=1, after_share_pct=25, share_delta_points=25),
        dict(role="before-only", before_weighted_m=3, before_share_pct=75,
             after_weighted_m=0, after_share_pct=0, share_delta_points=-75),
    ]


@pytest.mark.parametrize("before,after", [("2025-12-31", "2026-01-02"),
                                         ("2026-01-01", "2026-01-02T00:00:01Z")])
def test_empty_window(projects, capsys, before, after):
    # Move the before window wholly before the fixture when testing that side.
    if before == "2025-12-31":
        for path in projects.glob("*/*.jsonl"):
            path.write_text(path.read_text().replace("2026-01-01", "2025-12-30"))
    result = run(projects, capsys, before, after)
    side = "before" if before == "2025-12-31" else "after"
    assert result
    assert all(row[f"{side}_weighted_m"] == row[f"{side}_share_pct"] == 0 for row in result)


def test_no_transcripts(tmp_path, capsys):
    assert run(tmp_path, capsys) == []


def test_compare_text(projects, capsys):
    output = run(projects, capsys, json_output=False)
    assert "before weighted (M)" in output
    assert "delta (pp)" in output
    rows = output.splitlines()[1:4]
    assert [row.split()[0] for row in rows] == ["shared", "after-only", "before-only"]
    assert [float(value) for value in rows[0].split()[1:]] == [1, 25, 3, 75, 50]
