"""planner/context_pack.py: layout, held-out secrecy, expansion, safety, budget, tail."""
import os

import pytest

from plangraph.planner.context_pack import (
    PackTooBig, build_pack, estimate_tokens, pack_budget)

BIG = 10**6


def put(root, rel, text="x = 1\n"):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def card(**kw):
    base = {"outcome": "do the thing", "read_set": [], "write_set": [], "exports": [],
            "forbid_paths": [], "smoke_checks": [], "check_cmd": "pytest -q",
            "heldout_check": None, "window_class": "128k", "risk_tags": [],
            "estimate_tokens": None}
    base.update(kw)
    return base


def build(tmp_path, c, **kw):
    kw.setdefault("budget_tokens", BIG)
    return build_pack(c, repo_root=tmp_path, **kw)


def test_estimate_tokens_ceil():
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("abcde") == 2


def test_layout_order_and_header(tmp_path):
    put(tmp_path, "b_read.py", "R2\n")
    put(tmp_path, "a_read.py", "R1\n")
    put(tmp_path, "z_write.py", "W2\n")
    put(tmp_path, "y_write.py", "W1\n")
    c = card(read_set=["b_read.py", "a_read.py"], write_set=["z_write.py", "y_write.py"],
             exports=["foo", "bar"], forbid_paths=["secret/"], smoke_checks=["smoke a"],
             risk_tags=["auth"])
    t = build(tmp_path, c, key="OS-1", check_tail="boom").text
    assert t.startswith("# CARD OS-1\noutcome: do the thing\n")
    for line in ("check_cmd: pytest -q", "window_class: 128k", "risk_tags: auth",
                 "exports: foo, bar", "forbid: secret/", "smoke: smoke a"):
        assert line in t
    order = [t.index(s) for s in (
        "# CARD", "# CHECK OUTPUT", "# FILE y_write.py (write)", "# FILE z_write.py (write)",
        "# FILE a_read.py (read)", "# FILE b_read.py (read)")]
    assert order == sorted(order)
    assert "# FILE y_write.py (write)\n```\nW1\n\n```\n" in t


def test_heldout_check_never_in_text(tmp_path):
    c = card(heldout_check="HELDOUT_SECRET_CMD --hidden")
    p = build(tmp_path, c, check_tail="HELDOUT")
    assert "HELDOUT_SECRET_CMD" not in p.text
    assert "heldout" not in p.text.lower().replace("heldout\n", "")


def test_glob_and_dir_expansion(tmp_path):
    put(tmp_path, "pkg/a.py")
    put(tmp_path, "pkg/sub/b.py")
    put(tmp_path, "pkg/sub/deep/c.py")
    put(tmp_path, "other/d.txt")
    put(tmp_path, "other/e.md")
    p = build(tmp_path, card(read_set=["pkg", "other/*.md"]))
    got = {f.path for f in p.files}
    assert got == {"pkg/a.py", "pkg/sub/b.py", "other/e.md"}  # depth <= 2, glob filter


def test_expansion_cap_50(tmp_path):
    for i in range(60):
        put(tmp_path, f"many/f{i:02d}.py", "x\n")
    p = build(tmp_path, card(read_set=["many/*.py"]))
    assert len(p.files) == 50
    assert p.files[0].path == "many/f00.py" and p.files[-1].path == "many/f49.py"


def test_symlink_escape_skipped(tmp_path):
    outside = tmp_path / "outside"
    root = tmp_path / "repo"
    put(outside, "secret.txt", "TOPSECRET\n")
    root.mkdir()
    os.symlink(outside / "secret.txt", root / "link.txt")
    os.symlink(outside, root / "linkdir")
    p = build_pack(card(read_set=["link.txt", "linkdir"]), repo_root=root,
                   budget_tokens=BIG)
    assert "TOPSECRET" not in p.text
    assert ("link.txt", "outside_root") in p.skipped
    assert ("linkdir", "outside_root") in p.skipped or not p.files


def test_dotdot_and_absolute_skipped(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    put(tmp_path, "evil.txt", "EVIL\n")
    p = build_pack(card(read_set=["../evil.txt", str(tmp_path / "evil.txt")]),
                   repo_root=root, budget_tokens=BIG)
    assert "EVIL" not in p.text
    assert {r for _, r in p.skipped} == {"outside_root"}


def test_forbidden_skipped(tmp_path):
    put(tmp_path, "src/ok.py", "OK\n")
    put(tmp_path, "src/gen/x.py", "GEN\n")
    put(tmp_path, "keys.env", "KEY\n")
    c = card(read_set=["src", "keys.env"], forbid_paths=["src/gen", "*.env"])
    p = build(tmp_path, c)
    assert [f.path for f in p.files] == ["src/ok.py"]
    assert ("src/gen/x.py", "forbidden") in p.skipped
    assert ("keys.env", "forbidden") in p.skipped
    assert "GEN" not in p.text and "KEY" not in p.text


def test_binary_skipped(tmp_path):
    (tmp_path / "blob.bin").write_bytes(b"ab\x00cd")
    (tmp_path / "latin.txt").write_bytes(b"caf\xe9\n")
    p = build(tmp_path, card(read_set=["blob.bin", "latin.txt"]))
    assert p.files == ()
    assert set(p.skipped) == {("blob.bin", "binary"), ("latin.txt", "binary")}


def test_missing_read_skipped_new_write_listed(tmp_path):
    put(tmp_path, "old.py", "OLD\n")
    p = build(tmp_path, card(read_set=["nope.py"], write_set=["old.py", "fresh.py"]))
    assert ("nope.py", "missing") in p.skipped
    assert all(path != "fresh.py" for path, _ in p.skipped)
    assert "# FILE fresh.py (write, new)\n" in p.text
    assert "# FILE old.py (write)\n```\nOLD\n\n```\n" in p.text
    assert "fresh.py" in [f.path for f in p.files]


def test_truncation(tmp_path):
    put(tmp_path, "big.py", "a" * 400)
    p = build(tmp_path, card(read_set=["big.py"]), max_file_tokens=10)
    (f,) = p.files
    assert f.truncated is True
    assert "a" * 40 in p.text and "a" * 41 not in p.text
    assert "\n... [truncated 90 tokens]\n```\n" in p.text


def test_not_truncated_flag_false(tmp_path):
    put(tmp_path, "small.py", "tiny\n")
    (f,) = build(tmp_path, card(read_set=["small.py"])).files
    assert f.truncated is False


def test_over_budget_read_skipped_and_later_reads_too(tmp_path):
    put(tmp_path, "w.py", "w" * 40)
    put(tmp_path, "r1.py", "s\n")
    put(tmp_path, "r2.py", "x" * 4000)
    put(tmp_path, "r3.py", "t\n")
    c = card(write_set=["w.py"], read_set=["r1.py", "r2.py", "r3.py"])
    p = build(tmp_path, c, budget_tokens=200)
    assert [f.path for f in p.files] == ["w.py", "r1.py"]
    assert ("r2.py", "over_budget") in p.skipped
    assert ("r3.py", "over_budget") in p.skipped  # every later read skipped too
    assert p.tokens <= 200


def test_write_overflow_raises_pack_too_big(tmp_path):
    put(tmp_path, "w.py", "w" * 4000)
    with pytest.raises(PackTooBig) as ei:
        build(tmp_path, card(write_set=["w.py"]), budget_tokens=200)
    assert ei.value.budget == 200
    assert ei.value.needed > 1000
    assert isinstance(ei.value, ValueError)


def test_check_tail_clamped_and_last_lines(tmp_path):
    tail = "\n".join(f"line{i:03d}" for i in range(200))
    t = build(tmp_path, card(), check_tail=tail, tail_lines=500).text
    assert "# CHECK OUTPUT (last 60 lines)" in t
    assert "line199" in t and "line140" in t and "line139" not in t
    t = build(tmp_path, card(), check_tail=tail, tail_lines=5).text
    assert "# CHECK OUTPUT (last 40 lines)" in t
    assert "line199" in t and "line160" in t and "line159" not in t
    t = build(tmp_path, card(), check_tail=tail).text  # default 50
    assert "line150" in t and "line149" not in t


def test_no_tail_section_without_check_tail(tmp_path):
    assert "CHECK OUTPUT" not in build(tmp_path, card()).text


def test_deterministic_and_as_dict(tmp_path):
    put(tmp_path, "a.py", "A\n")
    put(tmp_path, "b.py", "B\n")
    c = card(read_set=["b.py", "a.py", "gone.py"], write_set=["n.py"])
    p1, p2 = build(tmp_path, c, key="K"), build(tmp_path, c, key="K")
    assert p1 == p2 and p1.as_dict() == p2.as_dict()
    d = p1.as_dict()
    assert d["tokens"] == estimate_tokens(d["text"]) == p1.tokens
    assert d["files"][0] == {"path": "n.py", "tokens": p1.files[0].tokens,
                             "truncated": False, "role": "write"}
    assert d["skipped"] == [["gone.py", "missing"]]


def test_file_in_both_sets_shown_once_as_write(tmp_path):
    put(tmp_path, "both.py", "BOTH\n")
    p = build(tmp_path, card(read_set=["both.py"], write_set=["both.py"]))
    assert [(f.path, f.role) for f in p.files] == [("both.py", "write")]
    assert p.text.count("BOTH") == 1


def test_bad_card_refused(tmp_path):
    with pytest.raises(ValueError):
        build(tmp_path, card(outcome=""))
    with pytest.raises(ValueError):
        build(tmp_path, card(read_set="a.py"))
    with pytest.raises(ValueError):
        build(tmp_path, card(risk_tags=["nope"]))
    with pytest.raises(ValueError):
        build(tmp_path, card(), budget_tokens=0)


def test_pack_budget_numbers():
    assert pack_budget("128k") == int(131072 * 0.55) - 8000 == 64089
    assert pack_budget("256k") == int(262144 * 0.55) - 8000 == 136179
    assert pack_budget("1m") == int(1048576 * 0.55) - 8000 == 568716
    assert pack_budget("128k", fraction=0.5, reserve_tokens=0) == 65536
    with pytest.raises(ValueError):
        pack_budget("2m")
    with pytest.raises(ValueError):
        pack_budget("128k", fraction=0)


# ------------------------------------------------------- F5 too-large write file
def test_too_large_write_file_raises_pack_too_big(tmp_path, monkeypatch):
    from plangraph.planner import context_pack as cp
    monkeypatch.setattr(cp, "_MAX_READ_BYTES", 100)
    put(tmp_path, "big.py", "x" * 400)
    with pytest.raises(PackTooBig) as info:
        build(tmp_path, card(write_set=["big.py"]), budget_tokens=1234)
    assert info.value.needed == 400 // cp.CHARS_PER_TOKEN
    assert info.value.budget == 1234 and info.value.path == "big.py"
    p = build(tmp_path, card(read_set=["big.py"]))  # read-set file stays skipped
    assert p.skipped == (("big.py", "too_large"),)


def test_binary_write_file_still_skipped(tmp_path):
    (tmp_path / "blob.bin").write_bytes(b"\0\1\2")
    p = build(tmp_path, card(write_set=["blob.bin"]))
    assert p.skipped == (("blob.bin", "binary"),)


# ------------------------------------------------------- F6 entry cap visible
def test_entry_cap_omissions_recorded(tmp_path):
    for i in range(60):
        put(tmp_path, f"many/f{i:02d}.py", "x\n")
        put(tmp_path, f"wr/g{i:02d}.py", "x\n")
    p = build(tmp_path, card(read_set=["many/*.py"], write_set=["wr"]))
    omitted = sorted(s for s in p.skipped if s[1] == "entry_cap")
    want = sorted([(f"many/f{i:02d}.py", "entry_cap") for i in range(50, 60)]
                  + [(f"wr/g{i:02d}.py", "entry_cap") for i in range(50, 60)])
    assert omitted == want
    assert len(p.files) == 100
