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


def parsed(raw):
    """The reply as a mapping, or ``None`` when a 2xx body is unreadable."""
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, AttributeError, UnicodeDecodeError):
        return None
    return data.get("result", data) if isinstance(data, dict) else None


def warnings(reply):
    """Warning lines from a create/apply_ops reply (missing descriptions)."""
    if not isinstance(reply, dict):
        return []
    if isinstance(reply.get("warnings"), list):
        return [f"warning: {warn}" for warn in reply["warnings"]]
    return [f"warning: {warn}" for entry in (reply.get("results") or [])
            if isinstance(entry, dict)
            for warn in (entry.get("warnings") or [])]


def refusal_lines(exc, offset, first):
    """The report lines for a refused (and fully rolled back) batch.

    A refusal that names no op (a bad token, an unreachable keeper) is
    reported against the first submitted op, *first* = ``(name, key)``.
    """
    text = renumber(exc.msg, offset)
    if not _OP_PREFIX.match(text):
        text = f"op {offset + 1} {first[0]} {first[1]}: {exc.code} {text}"
    return [text, "nothing applied (the whole batch was rolled back)"]


def send(session, ops, start, source, digest, errors, op_key):
    """Post the batch as one ``apply_ops`` call; ``(exit_code, report_lines)``.

    An unreadable 2xx reply, or one that does not report every op applied, is
    an error: the caller never prints "applied N of N" on a guess.
    """
    sent = len(ops) - start + 1
    try:
        raw = session.post("apply_ops", payload(ops, start, source, digest))
    except errors as exc:
        first = ops[start - 1]
        return 1, refusal_lines(exc, start - 1,
                                (first.get("op"), op_key(first)))
    reply = parsed(raw)
    got = reply.get("applied") if reply is not None else None
    if got != sent:
        return 1, [f"error: unreadable or short keeper reply (applied {got!r} "
                   f"of {sent}); batch state unknown, check the keeper"]
    return 0, warnings(reply)
