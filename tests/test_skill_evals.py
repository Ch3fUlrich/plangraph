"""Hermetic, stdlib-only checks for skills/*/evals/evals.json."""
import json
from pathlib import Path

SKILLS = Path(__file__).resolve().parents[1] / "skills"
KEYS = {"id", "prompt", "must", "must_not"}


def _skills():
    return sorted(p for p in SKILLS.iterdir() if p.is_dir())


def _load(skill):
    path = skill / "evals" / "evals.json"
    assert path.is_file(), f"missing {path}"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(data, list), f"{path}: not a JSON list"
    return data


def _md_text(skill):
    parts = [p.read_text(encoding="utf-8") for p in sorted(skill.rglob("*.md"))]
    assert parts, f"{skill}: no markdown files"
    return "\n".join(parts).lower()


def test_evals_shape_and_unique_ids():
    seen = {}
    for skill in _skills():
        items = _load(skill)
        assert 2 <= len(items) <= 3, f"{skill.name}: need 2-3 evals, got {len(items)}"
        for item in items:
            assert set(item) == KEYS, f"{skill.name}: keys must be exactly {sorted(KEYS)}"
            assert isinstance(item["id"], str) and item["id"], f"{skill.name}: bad id"
            assert isinstance(item["prompt"], str) and item["prompt"].strip(), (
                f"{skill.name}/{item['id']}: empty prompt")
            for field in ("must", "must_not"):
                phrases = item[field]
                assert isinstance(phrases, list) and 1 <= len(phrases) <= 3, (
                    f"{skill.name}/{item['id']}: {field} needs 1-3 phrases")
                assert all(isinstance(p, str) and p.strip() for p in phrases), (
                    f"{skill.name}/{item['id']}: {field} has an empty phrase")
            assert item["id"] not in seen, f"duplicate id {item['id']} ({seen[item['id']]})"
            seen[item["id"]] = skill.name


def test_must_phrases_appear_in_skill_text():
    for skill in _skills():
        text = _md_text(skill)
        for item in _load(skill):
            for phrase in item["must"]:
                assert phrase.lower() in text, (
                    f"{skill.name}/{item['id']}: must phrase {phrase!r} not in skill text")


if __name__ == "__main__":
    test_evals_shape_and_unique_ids()
    test_must_phrases_appear_in_skill_text()
    print("ok")
