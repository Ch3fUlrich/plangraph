"""Default apply_batch mode: the whole batch as ONE ``apply_ops`` call (B1).

The keeper applies every op in one transaction (all or nothing) and, on a
refusal, names the failing op as ``op <n> <op> <key>: <code> <message>`` with
``n`` counted inside the submitted slice; ``renumber`` maps it back onto the
batch file's own numbering (``--from``).
"""
import json
import re

_OP_PREFIX = re.compile(r"^op (\d+) ")


def payload(ops, start, source, digest):
    """The ``apply_ops`` body for ``ops[start-1:]`` plus the hashed batch ref."""
    return {"ops": list(ops[start - 1:]), "batch_ref": f"{source}@{digest}"}


def renumber(msg, offset):
    """Shift the leading ``op <n>`` of a keeper refusal by *offset*."""
    return _OP_PREFIX.sub(lambda m: f"op {int(m.group(1)) + offset} ",
                          msg or "", count=1)


def applied_count(raw, default):
    try:
        data = json.loads(raw.decode("utf-8"))
        data = data.get("result", data) if isinstance(data, dict) else {}
        return int(data.get("applied", default))
    except (ValueError, TypeError, AttributeError, UnicodeDecodeError):
        return default


def refusal_lines(exc, offset, first):
    """The report lines for a refused (and fully rolled back) batch.

    A refusal that names no op (a bad token, an unreachable keeper) is
    reported against the first submitted op, *first* = ``(name, key)``.
    """
    text = renumber(exc.msg, offset)
    if not _OP_PREFIX.match(text):
        text = f"op {offset + 1} {first[0]} {first[1]}: {exc.code} {text}"
    return [text, "nothing applied (the whole batch was rolled back)"]
