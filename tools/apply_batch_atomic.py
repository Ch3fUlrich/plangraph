"""Default apply_batch mode: the whole batch as ONE ``apply_ops`` call (B1).

The keeper applies every op in one transaction (all or nothing) and, on a
refusal, names the failing op as ``op <n> <op> <key>: <code> <message>`` with
``n`` counted inside the submitted slice; ``renumber`` maps it back onto the
batch file's own numbering (``--from``).
"""
import json
import re

_OP_PREFIX = re.compile(r"^op (\d+) ")


def payload(ops, start, source, digest, base_seq=None, dry_run=False):
    """The ``apply_ops`` body for ``ops[start-1:]`` plus the hashed batch ref."""
    body = {"ops": list(ops[start - 1:]), "batch_ref": f"{source}@{digest}"}
    if base_seq is not None:
        body["base_seq"] = base_seq
    if dry_run:
        body["dry_run"] = True
    return body


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


def dropped_deps(reply, ops, start, op_key):
    """Lines for dependencies dropped during edit ops."""
    if not isinstance(reply, dict):
        return []
    results = reply.get("results") or []
    lines = []
    for i, entry in enumerate(results):
        if not isinstance(entry, dict):
            continue
        deps = entry.get("dropped_deps")
        if isinstance(deps, list):
            idx = start - 1 + i
            if 0 <= idx < len(ops):
                key = op_key(ops[idx])
                for dep in deps:
                    lines.append(f"dropped dependency {dep} from {key}")
    return lines


def refusal_lines(exc, offset, first, batch_label=None):
    """The report lines for a refused (and fully rolled back) batch.

    A refusal that names no op (a bad token, an unreachable keeper) is
    reported against the first submitted op, *first* = ``(name, key)``.
    """
    msg = getattr(exc, "msg", "") or ""
    code = getattr(exc, "code", "") or "error"
    if msg.startswith("base_seq "):
        label = batch_label or "-"
        text = f"batch {label}: {code} {msg}"
    else:
        text = renumber(msg, offset)
        if not _OP_PREFIX.match(text):
            text = f"op {offset + 1} {first[0]} {first[1]}: {code} {text}"
    return [text, "nothing applied (the whole batch was rolled back)"]


def send(session, ops, start, source, digest, errors, op_key,
         base_seq=None, dry_run=False, batch_label=None):
    """Post the batch as one ``apply_ops`` call; ``(exit_code, report_lines)``.

    An unreadable 2xx reply, or one that does not report every op applied, is
    an error: the caller never prints "applied N of N" on a guess.
    """
    sent = len(ops) - start + 1
    try:
        raw = session.post("apply_ops",
                           payload(ops, start, source, digest,
                                   base_seq=base_seq, dry_run=dry_run))
    except errors as exc:
        first = ops[start - 1]
        return 1, refusal_lines(exc, start - 1,
                                (first.get("op"), op_key(first)),
                                batch_label=batch_label)
    reply = parsed(raw)
    got = reply.get("applied") if reply is not None else None
    if got != sent:
        return 1, [f"error: unreadable or short keeper reply (applied {got!r} "
                   f"of {sent}); batch state unknown, check the keeper"]
    lines = dropped_deps(reply, ops, start, op_key) + warnings(reply)
    return 0, lines
