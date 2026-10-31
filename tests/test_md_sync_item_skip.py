"""D-3395: md-sync never plans tier 'none' over a seat clause, and one keeper
refusal skips that item instead of halting the whole apply (synthetic only)."""
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_md_sync_dclose import (  # noqa: E402
    build, close_ops, dec_row, decs, item, repo)
import md_sync  # noqa: E402  (tools/ is on sys.path after the import above)
from test_md_sync_pin_retry import (  # noqa: E402
    site, srv, status_of, sync, synthetic, token, world)

__all__ = ["repo", "site", "srv", "token", "world"]


# ------------------------------------------------ dclose: seat clause guard

def seated(ident, done, tier="high", state="in_review"):
    out = item(ident, state=state, tier=tier, kinds=("commit+ci",))
    out["done_criteria"] = done
    return out


def test_seat_clause_item_is_skipped_not_downgraded(repo, tmp_path):
    text, view = decs(tmp_path, dec_row("D-3395", "coverage",
                                        f"demo:COV@{repo.merged[:8]}"))
    p = build([seated("COV", "Merged, CI green, 2 non-Claude seats, judge GO")],
              text, view, repo)
    assert p.ops == []
    assert close_ops(p.ops) == []
    assert p.skipped and p.skipped[0][0] == "COV"
    assert "closes via seat verdicts" in p.skipped[0][1]


def test_seat_clause_decision_only_item_is_skipped(repo, tmp_path):
    text, view = decs(tmp_path, dec_row("D-3395", "coverage", "demo:COV"))
    p = build([seated("COV", "two reviewers approve", tier="standard",
                      state="open")], text, view, repo)
    p_kinds = [o for o in p.ops if o.get("key") == "demo:COV"]
    assert p_kinds == []


def test_item_without_seat_clause_still_closes(repo, tmp_path):
    text, view = decs(tmp_path, dec_row("D-3395", "rules",
                                        f"demo:RULES@{repo.merged[:8]}"))
    p = build([seated("RULES", "applied by routing-00 under a GO",
                      tier="standard")], text, view, repo)
    assert len(close_ops(p.ops)) == 1


def test_tier_none_item_with_seat_words_is_not_blocked(repo, tmp_path):
    text, view = decs(tmp_path, dec_row("D-3395", "done", "demo:DONE"))
    out = item("DONE", tier="none", kinds=("decision",))
    out["done_criteria"] = "2 seats"
    p = build([out], text, view, repo)
    assert len(close_ops(p.ops)) == 1


# ------------------------------------------------ md_sync: refused item skip

def _fake_apply(first_line, seen):
    def fake(path, opts, out, err):
        keys = [md_sync.apply_batch.op_key(op)
                for op in yaml.safe_load(open(path))["ops"]]
        seen.append(keys)
        if len(seen) == 1:
            out.write(first_line + "\n")
            return 1
        return 0
    return fake


def test_refused_item_is_dropped_and_rest_applies(world, srv, token, site,
                                                  tmp_path, monkeypatch):
    synthetic(site)
    seen = []
    status = tmp_path / "md-sync.status.json"

    def fake(path, opts, out, err):
        keys = [md_sync.apply_batch.op_key(op)
                for op in yaml.safe_load(open(path))["ops"]]
        seen.append(keys)
        if len(seen) == 1:
            out.write(f"op 1 edit {keys[0]}: validation synthetic refusal\n")
            return 1
        return 0

    monkeypatch.setattr(md_sync, "_apply", fake)
    rc, out, err = sync(site, srv, token, "--apply", "--status-file", str(status))
    assert len(seen) == 2, out + err
    refused = seen[0][0]
    assert refused not in seen[1]
    assert len(seen[1]) == len([k for k in seen[0] if k != refused])
    assert f"refused {refused}: skipped" in out
    assert rc == 0
    doc = status_of(status)
    assert doc["applied"] is True and doc["refused"] >= 1


def test_forbidden_refusal_still_halts(world, srv, token, site, tmp_path,
                                       monkeypatch):
    synthetic(site)
    seen = []
    monkeypatch.setattr(md_sync, "_apply", _fake_apply(
        "op 1 edit routing:Q-SHAPY: forbidden not yours", seen))
    status = tmp_path / "md-sync.status.json"
    rc, out, err = sync(site, srv, token, "--apply", "--status-file", str(status))
    assert len(seen) == 1
    assert rc == 1
    assert status_of(status)["applied"] is False


def test_refused_key_parses_only_item_codes():
    assert md_sync._refused_key("op 3 legacy_state a:B: bad_state x\n") == "a:B"
    assert md_sync._refused_key("op 3 edit a:B: forbidden x\n") is None
    assert md_sync._refused_key("error: connection refused\n") is None
