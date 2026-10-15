"""Review prompt assembly: preamble, vectors, splits, map, tail."""
from pathlib import Path

import pytest

from plangraph.review import prompt as p

TAIL_PATH = Path(p.__file__).with_name("tail.md")
FORBIDDEN = ("Auto" + "OS", "Omni" + "Route", "omni" + "route")


def test_preamble_first_line():
    """Preamble on: exact first line; off: vectors header first."""
    with_pre = p.assemble(["v"], [("A", "text")])
    assert with_pre.splitlines()[0] == p.NO_TOOLS_PREAMBLE
    without = p.assemble(["v"], [("A", "text")], preamble=False)
    assert without.splitlines()[0] == "=== VECTORS"


def test_vector_numbering():
    """Vectors render as V1..Vn under the header line."""
    block = p.vector_block(["alpha", "beta", "gamma"])
    assert block.splitlines() == [
        "=== VECTORS", "V1. alpha", "V2. beta", "V3. gamma",
    ]


def test_empty_vectors_error():
    """Empty vectors fail closed everywhere."""
    with pytest.raises(ValueError):
        p.vector_block([])
    with pytest.raises(ValueError):
        p.assemble([], [("A", "text")])
    with pytest.raises(ValueError):
        p.assemble_all([], {"a.txt": "x"})


def test_tail_is_last():
    """Tail reminder closes every prompt, preamble or not."""
    tail = p.load_tail()
    assert TAIL_PATH.is_file() and tail
    for prompt in (p.assemble(["v"], [("A", "x")]),
                   p.assemble(["v"], [("A", "x")], preamble=False),
                   *p.assemble_all(["v"], {"a.txt": "x"})):
        assert prompt.endswith(tail.rstrip())


def test_split_boundaries():
    """Three 40KB files at a 60KB limit need three single-file parts."""
    files = {name: letter * 40_000
             for name, letter in (("a.txt", "a"), ("b.txt", "b"), ("c.txt", "c"))}
    parts = p.split_parts(files, 60_000)
    assert len(parts) == 3
    for part in parts:
        total = sum(len(text.encode("utf-8")) for text in part.values())
        assert total <= 60_000
    assert sorted(k for part in parts for k in part) == ["a.txt", "b.txt", "c.txt"]


def test_oversized_file_resplit():
    """One oversized file splits on line boundaries and reassembles."""
    text = "".join("line %04d content padding xxxxxxxxxx\n" % i for i in range(5000))
    assert len(text.encode("utf-8")) > 60_000
    parts = p.split_parts({"big.txt": text}, 60_000)
    assert len(parts) > 1
    assert [k for part in parts for k in part] == [
        "big.txt#%d" % i for i in range(1, len(parts) + 1)]
    for part in parts[:-1]:
        chunk = next(iter(part.values()))
        assert len(chunk.encode("utf-8")) <= 60_000
        assert chunk.endswith("\n")
    assert "".join(next(iter(part.values())) for part in parts) == text


def test_determinism():
    """Dict order never changes the split or the prompts."""
    first = {"b.txt": "b" * 100, "a.txt": "a" * 100, "c.txt": "c" * 100}
    second = {"c.txt": "c" * 100, "b.txt": "b" * 100, "a.txt": "a" * 100}
    assert p.split_parts(first, 60_000) == p.split_parts(second, 60_000)
    assert p.assemble_all(["v"], first) == p.assemble_all(["v"], second)


def test_part_map_text():
    """Map lines name each part's files in order."""
    parts = [{"a.txt": "a", "b.txt": "b"}, {"c.txt": "c"}]
    assert p.part_map(parts) == "part 1: a.txt, b.txt\npart 2: c.txt"


def test_assemble_all_carries_map():
    """Every per-part prompt holds the map block and ends with tail."""
    files = {"a.txt": "a" * 40_000, "b.txt": "b" * 40_000}
    prompts = p.assemble_all(["do X"], files, 60_000)
    assert len(prompts) == 2
    tail = p.load_tail()
    for prompt in prompts:
        assert prompt.splitlines()[0] == p.NO_TOOLS_PREAMBLE
        assert "\n=== FILE-PART MAP\n" in prompt
        assert "part 1:" in prompt and "part 2:" in prompt
        assert "V1. do X" in prompt
        assert prompt.endswith(tail.rstrip())


def test_sha256():
    """Digest matches hashlib over utf-8 bytes."""
    assert p.sha256("abc") == (
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")


def test_tail_hygiene():
    """tail.md carries no site strings."""
    text = TAIL_PATH.read_text(encoding="utf-8")
    for word in FORBIDDEN:
        assert word not in text
