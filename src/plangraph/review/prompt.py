"""Assemble tool-less review prompts (stdlib only)."""
from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path
from runpy import run_path
from string import Template

__all__ = [
    "NO_TOOLS_PREAMBLE",
    "render_brief",
    "load_tail",
    "vector_block",
    "assemble",
    "sha256",
    "split_parts",
    "part_map",
    "assemble_all",
    "assemble_diff",
]

NO_TOOLS_PREAMBLE = (
    "You have NO tools and no repository access. "
    "The full material is inline below. "
    "Answer in plain text; never emit tool calls."
)


def _claude_writer_trailer(rules: str, launch_record: dict | None) -> str:
    """Validate declared trailers and render the Claude fleet launch identity."""
    declared = [line for line in rules.splitlines()
                if ":" in line and line.split(":", 1)[0].strip().lower() == "writer"]
    if launch_record is None and not declared:
        return ""

    # Callers can render a brief from outside this checkout.
    gate = run_path(str(Path(__file__).resolve().parents[3] / "tools" / "ci_commit_gate.py"))

    for line in declared:
        violations = gate["message_violations"](f"Writer brief\n\n{line}")
        if violations:
            raise ValueError(f"Writer brief refused by CLAUDE_WRITER_RE: {', '.join(violations)}")
        if launch_record is not None:
            raise ValueError("Writer brief must take its Writer trailer from the launch record")

    if launch_record is None:
        return ""
    model = launch_record.get("model")
    session_id = launch_record.get("session_id")
    refusal = f"Writer brief refused by CLAUDE_WRITER_RE: {gate['CLAUDE_WRITER']}"
    if not all(isinstance(part, str) and part and not any(map(str.isspace, part))
               for part in (model, session_id)):
        raise ValueError(refusal)
    if not model.startswith("claude-"):
        model = f"claude-{model}"
    identity = f"{model} {session_id}"
    if not gate["CLAUDE_WRITER_RE"].fullmatch(identity):
        raise ValueError(refusal)
    return f"\nWriter: {identity}"


def _writer_artifacts(record: dict) -> str:
    sections = ["\n=== WRITER RUN EVIDENCE"]
    for label, field in (("writer status", "writer_status_path"),
                         ("writer slices", "writer_slices_path")):
        value = record.get(field)
        if value is None:
            sections.append(f"{label}: missing")
            continue
        try:
            path = Path(value)
            if not path.is_absolute() or path.parent != Path(record["review_dir"]):
                raise ValueError("artifact must be directly inside the review directory")
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                         | getattr(os, "O_NONBLOCK", 0))
            with os.fdopen(fd, "rb") as artifact:
                if not stat.S_ISREG(os.fstat(artifact.fileno()).st_mode):
                    raise ValueError("expected a regular artifact file")
                data = artifact.read(1024 * 1024 + 1)
            if len(data) > 1024 * 1024:
                raise ValueError("artifact exceeds 1 MiB")
            content = data.decode("utf-8")
        except FileNotFoundError:
            sections.append(f"{label}: missing")
        except (KeyError, OSError, TypeError, ValueError):
            sections.append(f"{label}: unreadable")
        else:
            sections.append(f"{label}: included\n"
                            + "\n".join("> " + line for line in content.splitlines()))
    return "\n".join(sections) + "\n\n"


def render_brief(role: str, context: dict | None = None, *, rules: str = "",
                 launch_record: dict | None = None) -> str:
    """Render a versioned brief, optionally bound to a Claude fleet writer record."""
    if role not in ("review", "writer"):
        raise ValueError("role must be review or writer")
    fields = ("item_key", "done_criteria", "invariants", "base_sha", "head_sha",
              "public_symbols", "callers", "test_output_paths")
    values = {field: "unavailable (request before acceptance)" for field in fields}
    context = context or {}
    values.update({key: str(value) for key, value in context.items()
                   if key in fields})
    text = Path(__file__).with_name(role + "_brief.md").read_text(encoding="utf-8")
    prefix, marker, item = text.partition("=== ITEM CONTEXT")
    writer = _claude_writer_trailer(rules, launch_record) if role == "writer" else ""
    writer_run = context.get("writer_run")
    evidence = _writer_artifacts(writer_run) if role == "review" and isinstance(writer_run, dict) else ""
    return prefix + rules + "\n" + evidence + marker + writer + Template(item).substitute(values)


def load_tail() -> str:
    """Tail reminder text stored next to this module."""
    return Path(__file__).with_name("tail.md").read_text(encoding="utf-8")


def vector_block(vectors: list[str]) -> str:
    """Numbered V1..Vn lines under an === VECTORS header."""
    if not vectors:
        raise ValueError("vectors must not be empty")
    lines = ["=== VECTORS"]
    lines.extend("V%d. %s" % (i, text) for i, text in enumerate(vectors, 1))
    return "\n".join(lines)


def assemble(vectors: list[str], parts: list[tuple[str, str]],
             *, preamble: bool = True, context: dict | None = None) -> str:
    """Fixed preamble/rules/output contract first, item context and material last."""
    if not vectors:
        raise ValueError("vectors must not be empty")
    out = ""
    if preamble:
        out += NO_TOOLS_PREAMBLE + "\n"
    out += render_brief("review", context, rules=load_tail().rstrip()) + "\n"
    out += vector_block(vectors)
    for label, text in parts:
        out += "\n=== %s\n" % label + text
    return out


def sha256(prompt: str) -> str:
    """Hex digest of the prompt utf-8 bytes."""
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


def _split_text(text: str, limit: int) -> list[str]:
    """Split text into chunks of whole lines each <= limit bytes."""
    lines = text.splitlines(keepends=True)
    if not lines:
        return [text]
    chunks: list[str] = []
    buf: list[str] = []
    size = 0
    for line in lines:
        width = len(line.encode("utf-8"))
        if width > limit:
            if buf:
                chunks.append("".join(buf))
                buf = []
                size = 0
            chunks.append(line)
        elif buf and size + width > limit:
            chunks.append("".join(buf))
            buf = [line]
            size = width
        else:
            buf.append(line)
            size += width
    if buf:
        chunks.append("".join(buf))
    return chunks or [text]


def split_parts(files: dict[str, str], limit: int = 60_000) -> list[dict[str, str]]:
    """Pack path->text map into parts of <= limit bytes by whole files."""
    if limit <= 0:
        raise ValueError("limit must be positive")
    parts: list[dict[str, str]] = []
    current: dict[str, str] = {}
    current_size = 0
    for path, text in sorted(files.items()):
        size = len(text.encode("utf-8"))
        if size > limit:
            if current:
                parts.append(current)
                current = {}
                current_size = 0
            for i, chunk in enumerate(_split_text(text, limit), 1):
                parts.append({"%s#%d" % (path, i): chunk})
            continue
        if current and current_size + size > limit:
            parts.append(current)
            current = {}
            current_size = 0
        current[path] = text
        current_size += size
    if current:
        parts.append(current)
    return parts


def part_map(parts: list[dict[str, str]]) -> str:
    """One 'part N: path1, path2' line per part."""
    return "\n".join("part %d: %s" % (i, ", ".join(sorted(part)))
                     for i, part in enumerate(parts, 1))


def assemble_all(vectors: list[str], files: dict[str, str],
                 limit: int = 60_000, preamble: bool = True,
                 context: dict | None = None) -> list[str]:
    """One full prompt per split part, each carrying the file-part map."""
    if not vectors:
        raise ValueError("vectors must not be empty")
    parts = split_parts(files, limit)
    if not parts:
        parts = [{}]
    mapping = part_map(parts)
    prompts = []
    for part in parts:
        labelled = [("FILE-PART MAP", mapping)]
        labelled.extend(sorted(part.items()))
        prompts.append(assemble(vectors, labelled, preamble=preamble, context=context))
    return prompts


def assemble_diff(vectors: list[str], diff_text: str,
                  max_bytes: int = 60_000, preamble: bool = True,
                  context: dict | None = None) -> list[str]:
    """One prompt per module part of a diff, each carrying the file map.

    Parts come from split.split_diff_by_module: grouped by module, never
    truncated (a file above max_bytes is alone in its own part).
    """
    from . import split

    if not vectors:
        raise ValueError("vectors must not be empty")
    parts = split.split_diff_by_module(diff_text, max_bytes)
    if not parts:
        return assemble_all(vectors, {}, max_bytes, preamble, context)
    mapping = split.render_part_map(parts)
    return [assemble(vectors, [("FILE-PART MAP", mapping),
                               ("DIFF PART %d [%s]" % (p.index, p.module), p.text)],
                     preamble=preamble, context=context) for p in parts]
