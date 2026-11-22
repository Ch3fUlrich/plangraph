import importlib.util
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "skill-rules.py"


def load_tool():
    spec = importlib.util.spec_from_file_location("skill_rules", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


tool = load_tool()


def write_rules(tmp_path, *lines):
    path = tmp_path / "rules.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def rule(rule_id, imperative, why="it works", source="test_a"):
    return f"{rule_id}: {imperative} (why: {why}; source: {source})"


def problems_for(path):
    return tool.check_files([str(path)])[1]


def skill_md(name, total_lines, description="A demo skill."):
    head = ["---", f"name: {name}", f"description: {description}", "---"]
    body = ["body"] * (total_lines - len(head))
    return "\n".join(head + body) + "\n"


def write_skill(base, name, total_lines=10, content=None):
    directory = base / name
    directory.mkdir()
    (directory / "SKILL.md").write_text(
        content if content is not None else skill_md(name, total_lines),
        encoding="utf-8",
    )
    return directory


def test_duplicate_id_fails(tmp_path):
    path = write_rules(
        tmp_path,
        rule("R-a-01", "Do alpha."),
        rule("R-a-01", "Do beta."),
    )
    assert any(p[3] == "duplicate id" for p in problems_for(path))


def test_missing_source_fails(tmp_path):
    path = write_rules(tmp_path, "R-a-01: Do alpha. (why: it works)")
    assert any("missing tail" in p[3] for p in problems_for(path))


def test_why_over_12_words_fails(tmp_path):
    why = "one two three four five six seven eight nine ten eleven twelve thirteen"
    path = write_rules(tmp_path, rule("R-a-01", "Do alpha.", why=why))
    assert any(p[3] == "why too long" for p in problems_for(path))


def test_line_over_200_fails(tmp_path):
    path = write_rules(tmp_path, rule("R-a-01", "x" * 201))
    assert any(p[3] == "line too long" for p in problems_for(path))


def test_near_duplicate_fails(tmp_path):
    path = write_rules(
        tmp_path,
        rule("R-a-01", "Validate the change carefully now."),
        rule("R-a-02", "Validate the change carefully now!"),
    )
    assert any("near-duplicate" in p[3] for p in problems_for(path))


def test_continuation_wrap_passes(tmp_path):
    path = write_rules(
        tmp_path,
        "R-a-01: Do a thing that is long enough to wrap",
        "  onto a second line. (why: it works; source: test_a)",
    )
    assert problems_for(path) == []


def test_lint_501_lines_fails(tmp_path):
    directory = write_skill(tmp_path, "demo", total_lines=501)
    problems = tool.lint_dirs([str(directory)])
    assert any("lines (max 500)" in p[3] for p in problems)


def test_lint_500_lines_passes(tmp_path):
    directory = write_skill(tmp_path, "demo", total_lines=500)
    assert tool.lint_dirs([str(directory)]) == []


def test_lint_missing_or_mismatched_name_fails(tmp_path):
    no_name = write_skill(
        tmp_path,
        "no-name",
        content="---\ndescription: A demo skill.\n---\n",
    )
    wrong_name = write_skill(
        tmp_path,
        "wrong-name",
        content="---\nname: other\ndescription: A demo skill.\n---\n",
    )
    problems = tool.lint_dirs([str(no_name), str(wrong_name)])
    assert any("missing name" in p[3] for p in problems)
    assert any("!= directory" in p[3] for p in problems)


def test_lint_valid_passes(tmp_path):
    directory = write_skill(tmp_path, "demo", total_lines=20)
    (directory / "notes.md").write_text("note\n" * 500, encoding="utf-8")
    assert tool.lint_dirs([str(directory)]) == []


def test_all_against_real_skills_passes():
    result = subprocess.run(
        [sys.executable, str(TOOL), "all"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


OLD = "rout" + "ing"


def test_lint_old_routing_path_fails(tmp_path):
    for index, path in enumerate(
        ("/srv/code/" + OLD + "/status/x", "/srv/code/" + OLD + "-worktrees/a/b")
    ):
        base = tmp_path / str(index)
        base.mkdir()
        content = skill_md("demo", 8) + f"See {path}.\n"
        directory = write_skill(base, "demo", content=content)
        problems = tool.lint_dirs([str(directory)])
        assert any("retired routing folder" in p[3] for p in problems), path


def test_lint_other_code_paths_pass(tmp_path):
    content = skill_md("demo", 8) + "See /srv/code/other-repo/bin.\n"
    directory = write_skill(tmp_path, "demo", content=content)
    assert tool.lint_dirs([str(directory)]) == []


def test_real_skills_name_no_old_routing_path():
    problems = tool.lint_dirs([str(p) for p in tool.default_skill_dirs()])
    assert [p for p in problems if "retired routing folder" in p[3]] == []


LEARN_SKILLS = ("unattended-orchestration", "batch-writer", "review", "coding-principles")


def learn_section(name):
    text = (ROOT / "skills" / name / "SKILL.md").read_text(encoding="utf-8")
    assert "\n## Learn as you go\n" in text, name
    return text.split("\n## Learn as you go\n", 1)[1].split("\n## ", 1)[0].strip()


def test_four_skills_carry_the_same_learn_as_you_go_section():
    sections = {name: learn_section(name) for name in LEARN_SKILLS}
    assert len(set(sections.values())) == 1, sections
    text = sections["review"]
    for needle in ("`LESSON <what happened> <evidence", "`LE-*` keeper item", "same beat",
                   "never the fix", "read the open and done `LE-*` items", "happens twice"):
        assert needle in text, needle


def test_batch_writer_result_format_has_a_lesson_line():
    text = (ROOT / "skills" / "batch-writer" / "SKILL.md").read_text(encoding="utf-8")
    end = text.split("\n## End\n", 1)[1]
    assert "`LESSON <what happened> <evidence" in end
