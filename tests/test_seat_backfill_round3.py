"""Fail-closed attribution, transport preconditions and batch-mode regressions."""
import json
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from test_seat_backfill import ITEM, SHA, record
from test_seat_backfill_replay import backfill_case
import seat_record as sr
import seat_verdict as sv


@pytest.fixture(autouse=True)
def hermetic(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("PG_URL", raising=False)
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: pytest.fail("network"))


@pytest.mark.parametrize("failure", [OSError("fixture"), subprocess.SubprocessError("fixture"),
                                    subprocess.TimeoutExpired("git", 1), [], [{}], [{"Writer": ""}]])
def test_failed_trailer_lookup_never_uses_partial_writer_set(backfill_case, monkeypatch,
                                                            capsys, failure):
    from plangraph.keeper.evidence_git import GitAdapter
    args, run = backfill_case
    monkeypatch.setattr(GitAdapter, "repo_path", lambda *a: str(args.records_dir))
    lookup = Mock(side_effect=failure) if isinstance(failure, Exception) else Mock(return_value=failure)
    monkeypatch.setattr(GitAdapter, "writer_trailers", lookup)
    monkeypatch.setattr(sr, "merged_pr", lambda *a: None)
    poster = Mock()
    before = {p.name: p.read_bytes() for p in run.iterdir()}
    assert sr.backfill(args, poster) == 0
    row = json.loads(capsys.readouterr().out)[0]
    assert row["status"] == "MISSING"
    assert row["reasons"] == ["unknown writer"]
    assert row["seats"] == []
    lookup.assert_called_once()
    poster.assert_not_called()
    assert before == {p.name: p.read_bytes() for p in run.iterdir()}


@pytest.mark.parametrize("entry", ["helper", "batch"])
@pytest.mark.parametrize("field,value", [("url", None), ("url", ""), ("url", "absent"),
                                         ("token_dir", None), ("token_dir", "absent")])
@pytest.mark.parametrize("journal", [False, True])
def test_missing_transport_leaves_forward_outbox_untouched(backfill_case, capsys, entry,
                                                         field, value, journal):
    args, run = backfill_case
    if journal:
        (run / "verdict.json").write_text('{"status": "retry", "attempts": 2}\n')
    before = {p.name: p.read_bytes() for p in run.iterdir()}
    if value == "absent":
        delattr(args, field)
    else:
        setattr(args, field, value)
    poster = Mock(return_value=0)
    if entry == "batch":
        assert sr.backfill(args, poster) == 1
        row = json.loads(capsys.readouterr().out)[0]
    else:
        seat = sr.eligible_records(ITEM, args.records_dir)[0][0]
        row = {"reasons": []}
        assert sv.apply_backfill(args, ITEM, seat, poster, row) == 1
    assert row["delivery"] == "failed"
    assert row["verifier_exit"] is None
    assert "requires --url and --token-dir" in " ".join(row["reasons"])
    poster.assert_not_called()
    assert before == {p.name: p.read_bytes() for p in run.iterdir()}


def test_library_url_default_matches_cli_environment(backfill_case, monkeypatch, capsys):
    args, _ = backfill_case
    url = args.url
    del args.url
    monkeypatch.setenv("PG_URL", url)
    poster = Mock(return_value=0)
    assert sr.backfill(args, poster) == 0
    row = json.loads(capsys.readouterr().out)[0]
    assert row["delivery"] == "delivered"
    poster.assert_called_once()
    assert poster.call_args.args[0][-2:] == ["--url", url]


@pytest.mark.parametrize("mode", ["--backfill-apply", "--backfill-dry-run"])
@pytest.mark.parametrize("single", [["--key", "demo:A"], ["--seat-dir", "seat"],
                                    ["--key", "demo:A", "--seat-dir", "seat"],
                                    ["--head", SHA], ["--writer-family", "openai"]])
def test_explicit_batch_mode_rejects_single_seat_arguments(backfill_case, monkeypatch,
                                                         capsys, mode, single):
    args, _ = backfill_case
    batch = Mock(return_value=0)
    monkeypatch.setattr(sr, "backfill", batch)
    with pytest.raises(SystemExit) as exc:
        sv.main([mode, "--snapshot", str(args.snapshot), "--url", args.url,
                 "--token-dir", str(args.token_dir), *single])
    assert exc.value.code == 2
    assert "backfill modes cannot use single-seat arguments" in capsys.readouterr().err
    batch.assert_not_called()


@pytest.mark.parametrize("records", ["absent", None])
def test_library_uses_default_records_directory_and_refuses_missing_transport(backfill_case,
                                                                            capsys, records):
    args, run = backfill_case
    default = Path.home() / ".local/share/plangraph/spawner-records"
    default.parent.mkdir(parents=True)
    default.mkdir()
    for path in (run, args.records_dir / "writer"):
        path.rename(default / path.name)
    if records == "absent":
        del args.records_dir
    else:
        args.records_dir = None
    del args.token_dir
    del args.url
    poster = Mock()
    assert sr.backfill(args, poster) == 1
    row = json.loads(capsys.readouterr().out)[0]
    assert row["status"] == "ELIGIBLE"
    assert row["delivery"] == "failed"
    assert "requires --url and --token-dir" in " ".join(row["reasons"])
    poster.assert_not_called()
    assert not list(default.rglob("verdict*"))


@pytest.mark.parametrize("phase", ["writer", "pr"])
@pytest.mark.parametrize("error", [KeyError, AttributeError])
def test_item_resolution_failure_emits_row_and_continues(backfill_case, monkeypatch,
                                                       capsys, phase, error):
    import plangraph.keeper.evidence_repo as repos
    args, run = backfill_case
    snapshot = json.loads(args.snapshot.read_text())
    snapshot["items"].insert(0, dict(snapshot["items"][0], key="broken:A"))
    args.snapshot.write_text(json.dumps(snapshot))
    original, calls = repos.repo_key, []
    def resolve(item, *a):
        if item["key"] == "broken:A":
            calls.append(item["key"])
            if phase == "writer" or len(calls) == 2:
                raise error("fixture-sensitive-details")
        return original(item, *a)
    monkeypatch.setattr(repos, "repo_key", resolve)
    poster = Mock(return_value=0)
    assert sr.backfill(args, poster) == 0
    rows = json.loads(capsys.readouterr().out)
    assert len(rows) == 2
    assert rows[0]["status"] == "MISSING"
    assert rows[0]["seats"] == []
    assert rows[0]["reasons"] == [f"item PR/writer resolution failed: {error.__name__}"]
    assert rows[1]["delivery"] == "delivered"
    poster.assert_called_once()
    assert poster.call_args.args[0][1] == "demo:A"
    assert (run / "verdict.delivered").exists()


@pytest.mark.parametrize("token", ["model:GPT-4", "Branch:MAIN", "Team:AI", "other:UPPER",
                                   "demo:GPT-4", "demo:PASS"])
def test_metadata_tokens_do_not_pollute_tsv_tallies(backfill_case, capsys, token):
    args, run = backfill_case
    args.backfill_apply, args.format = False, "tsv"
    path = run / "record.json"
    path.write_text(json.dumps(dict(json.loads(path.read_text()), item_key=None)))
    (run / "prompt.txt").write_text("Review demo:A " + token)
    assert sr.backfill(args, Mock()) == 0
    output = capsys.readouterr().out
    assert "demo:A\tELIGIBLE" in output
    assert "counts\titems=1\teligible=1\tmissing=0\tfailed=0" in output
    assert "item key mismatch" not in output


@pytest.mark.parametrize("key", ["other:B", "other:lower.case_1", "demo:lower.case_1"])
def test_real_known_project_key_contradictions_still_refuse(backfill_case, capsys, key):
    args, run = backfill_case
    args.backfill_apply = False
    snapshot = json.loads(args.snapshot.read_text())
    snapshot["items"].append({"key": key, "state": "done"})
    args.snapshot.write_text(json.dumps(snapshot))
    (run / "prompt.txt").write_text("Review demo:A " + key)
    assert sr.backfill(args, Mock()) == 0
    row = json.loads(capsys.readouterr().out)[0]
    assert row["status"] == "MISSING"
    assert "item key mismatch" in " ".join(row["reasons"])


def test_shipped_fragment_has_no_review_dialogue():
    root = Path(__file__).resolve().parents[1]
    fragment = (root / "changelog.d/SEAT-VERDICT-TO-KEEPER-r2.md").read_text()
    assert "not reproduced" not in fragment
    assert "427 tests" not in fragment


@pytest.mark.parametrize("repo", [None, ""])
@pytest.mark.parametrize("repo_key", ["demo", "demo/lane"])
def test_configured_missing_repo_never_uses_implementation_writers(backfill_case,
                                                                 monkeypatch, capsys, repo, repo_key):
    from plangraph.keeper.evidence_git import GitAdapter
    args, run = backfill_case
    snapshot = json.loads(args.snapshot.read_text())
    snapshot["items"][0]["project"] = "demo"
    snapshot["items"][0]["track"] = "lane"
    args.snapshot.write_text(json.dumps(snapshot))
    config = Path.home() / ".local/share/plangraph/keeper.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"repos": {repo_key: {"path": str(args.records_dir)}}}))
    assert sr.writer_families(snapshot["items"][0], SHA, args.records_dir, None) == ["openai"]
    monkeypatch.setattr(GitAdapter, "repo_path", lambda *a: repo)
    lookup = Mock(side_effect=AssertionError("missing repository must not reach Git"))
    monkeypatch.setattr(GitAdapter, "writer_trailers", lookup)
    poster = Mock(return_value=0)
    before = {p.name: p.read_bytes() for p in run.iterdir()}
    assert sr.backfill(args, poster) == 0
    row = json.loads(capsys.readouterr().out)[0]
    assert row["status"] == "MISSING"
    assert row["reasons"] == ["unknown writer"]
    assert row["seats"] == []
    lookup.assert_not_called()
    poster.assert_not_called()
    assert before == {p.name: p.read_bytes() for p in run.iterdir()}


@pytest.mark.parametrize("key", ["SEAT-123", "demo/A", "demo:A/b", "demo:A"])
@pytest.mark.parametrize("prefix,suffix", [("", ""), ("-", ""), (".", ""), ("", ".")])
def test_exact_named_prompt_item_still_binds(tmp_path, key, prefix, suffix):
    run = record(tmp_path, item_key=None)
    (run / "prompt.txt").write_text(f"Review {prefix}{key}{suffix}")
    seats, reasons = sr.eligible_records(dict(ITEM, key=key), tmp_path)
    assert len(seats) == 1, reasons
    assert seats[0]["run_id"] == run.name
    assert reasons == []
