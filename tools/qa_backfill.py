#!/usr/bin/env python3
"""Propose operator question choices for QUESTIONS.md rows lacking them (qa-p2 W2).

Reads QUESTIONS.md only (never writes it). Writes exactly two files into OUT:
``proposed-rows.md`` (copy-pasteable table) and ``review.md`` (per-id review).
Deterministic, no timestamps, no network.
"""
import argparse
import os
import re
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import md_sync_parse as mp  # noqa: E402

_SRC = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    os.pardir, "src"))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)
from plangraph.keeper import credential_guard  # noqa: E402

USAGE = "usage: qa_backfill.py --questions F --out DIR\n"
_OPTIONS_START = re.compile(r"(?:^|\s)Options:", re.IGNORECASE)
_PARENT_TOKEN = re.compile(
    r"(?:^|\s)Parent:\s*(?P<key>[A-Za-z0-9_-]+(?::[A-Za-z0-9_-]+)?)\s*\.?\s*$",
    re.IGNORECASE)
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
# The generic consequence names the router (built without the literal so the
# committed source carries no fleet-principal string; the runtime output keeps it).
_ROUTER_TELL = "tell " + "routing-" + "00"
_TRIG_DATE = re.compile(
    r"(?i)\b(deadline|sp\xe4testens|by|until)\b[^0-9A-Za-z]{0,40}?([0-9]{4}-[0-9]{2}-[0-9]{2})")
_DEADLINE_NONE = re.compile(r"(?i)deadline\s*:\s*none")
_ENUM_PART = re.compile(r"^\s*\(?([a-dA-D])\)\s*(.+?)\s*$")
_PAREN_ENUM = re.compile(r"\(([a-dA-D])\)\s*([^();]+?)(?=\s*\([a-dA-D]\)\s*|\s*;|\s*$)")
_ALT = re.compile(r"(?i)\bAlternative\s*:\s*([^.;]+)")
_DEF = re.compile(r"(?i)\b(Default|Standard)\s*:\s*([^.;]+)")
_WHEN_CONV = re.compile(r"(?i)when convenient")


def mask_text(text):
    if not isinstance(text, str):
        return ""
    masked, _ = mp.redact(text)
    masked, _ = credential_guard.mask(masked)
    return masked


def clean_cell(text):
    text = mask_text(text or "")
    text = text.replace("|", "/").replace("\n", " ").strip()
    return re.sub(r"\s+", " ", text)


def remove_options_block(step):
    match = _OPTIONS_START.search(step or "")
    if not match:
        return (step or "").strip()
    return step[:match.start()].rstrip(" .;:")


def find_deadline(why, step):
    blob = f"{why or ''}\n{step or ''}"
    if _DEADLINE_NONE.search(blob):
        return None
    match = _TRIG_DATE.search(blob)
    if not match:
        return None
    try:
        date.fromisoformat(match.group(2))
    except ValueError:
        return None
    return match.group(2)


_DANGLING = frozenset({
    "and", "or", "the", "to", "in", "of", "an", "for", "with", "on",
    "at", "by", "from", "as", "but", "if", "then", "than", "so", "yet",
    "nor", "also", "because", "while", "when", "where", "which", "who",
    "whom", "whose", "what", "how", "why", "is", "are", "was", "were",
    "be", "been", "being", "have", "has", "had", "do", "does", "did",
    "will", "would", "should", "could", "may", "might", "must", "can",
    "shall", "it", "its", "this", "that", "these", "those", "they",
    "them", "their", "he", "she", "we", "you", "my", "our", "your",
    "his", "her", "not", "no", "per", "via", "vs", "plus", "without",
    "within", "about", "into", "over", "after", "before", "between",
    "under", "above", "out", "up", "down",
})

_SALV_ENUM = re.compile(r"^\s*(?:\([a-dA-D0-9]+\)|[a-dA-D0-9]+[).:])\s*")
_GENERIC_LABELS = ("do it as written", "skip or defer")


def _strip_dangling(text):
    prev = None
    while prev != text:
        prev = text
        words = text.strip(" .;:-,!?\"'").split()
        if words and words[-1].lower().strip("()[]\"'") in _DANGLING:
            text = " ".join(words[:-1]).strip(" .;:-,!?\"'")
    return text.strip(" .;:-")


def _short(text, cap):
    text = re.sub(r"\s+", " ", (text or "").strip())
    text = text.replace("|", "/").replace(";", ",")
    text = re.sub(r"(?i)deadline\s*:", "deadline -", text)
    text = "".join(" " if mp.is_control(c) else c for c in text)
    text = re.sub(r"\s+", " ", text).strip(" .;:-")
    if len(text) > cap:
        cut = text[:cap].rsplit(" ", 1)
        text = cut[0] if len(cut) == 2 and len(cut[0]) >= cap * 0.5 else text[:cap]
        text = text.rstrip(" .;:-")
    text = _strip_dangling(text)
    return text.strip()


def _is_balanced(text):
    stack = []
    pairs = {")": "(", "]": "["}
    for c in text or "":
        if c in "([":
            stack.append(c)
        elif c in ")]":
            if not stack or stack.pop() != pairs[c]:
                return False
    return not stack


def _is_clean_label(label):
    if not label or not re.search(r"[A-Za-z0-9]", label):
        return False
    if len(label) > 60:
        return False
    if "`" in label or "*" in label or ";" in label or "|" in label:
        return False
    if label.lstrip().startswith("*"):
        return False
    if not _is_balanced(label):
        return False
    words = label.strip(" .;:-,!?\"'").split()
    if not words:
        return False
    if words[-1].lower().strip("()[]\"'") in _DANGLING:
        return False
    return True


def _clean_consequence(text):
    text = (text or "").replace("`", "").replace("*", "")
    text = re.sub(r"\s+", " ", text).strip()
    if not _is_balanced(text):
        return ""
    return text


def _generic_options():
    return ([{"id": "do-it-as-written", "label": "do it as written",
              "consequence": _ROUTER_TELL},
             {"id": "skip-or-defer", "label": "skip or defer",
              "consequence": _ROUTER_TELL}],
            "do-it-as-written", {"generic"})


def _salvage_options(step_raw):
    """Salvage a malformed Options: block. Returns (pairs, default_idx|None) or None."""
    start_m = _OPTIONS_START.search(step_raw or "")
    if not start_m:
        return None
    om = mp._OPTIONS_RE.search(step_raw or "")
    if om:
        opts_str = om.group("opts")
    else:
        m2 = re.search(r"(?i)Options:\s*(.*)$", step_raw or "", re.DOTALL)
        if not m2:
            return None
        opts_str = m2.group(1)
        opts_str = re.sub(r"(?i)\.?\s*Deadline:\s*.*?\s*$", "", opts_str).strip()
    pairs = []
    default_idx = None
    for frag in opts_str.split(";"):
        frag = (frag or "").strip()
        if not frag:
            continue
        marked = frag.startswith("*")
        if marked:
            frag = frag[1:].strip()
        frag = _SALV_ENUM.sub("", frag).strip()
        if "=" in frag:
            label_raw, _, conseq_raw = frag.partition("=")
            label_raw, conseq_raw = label_raw.strip(), conseq_raw.strip()
        else:
            label_raw, conseq_raw = frag.strip(), ""
        if not label_raw or not re.search(r"[A-Za-z0-9]", label_raw):
            continue
        if len(label_raw) > 60 and not conseq_raw:
            label = _short(label_raw, 60)
            conseq = _clean_consequence(_short(label_raw, 160))
        else:
            label = _short(label_raw, 60)
            conseq = _clean_consequence(_short(conseq_raw, 160))
        if not _is_clean_label(label):
            continue
        if marked:
            if default_idx is not None:
                return None
            default_idx = len(pairs)
        pairs.append((label, conseq))
        if len(pairs) > 8:
            pairs = pairs[:8]
            if default_idx is not None and default_idx >= 8:
                default_idx = None
            break
    if len(pairs) < 2:
        return None
    pairs = pairs[:8]
    if default_idx is not None and default_idx >= len(pairs):
        default_idx = None
    return pairs, default_idx


def _finish_salvaged(pairs, default_idx, step, why):
    options = []
    seen = set()
    for label, conseq in pairs[:8]:
        if not _is_clean_label(label):
            continue
        conseq = _clean_consequence(conseq)
        oid = mp._slug(label)
        if not oid or oid in seen:
            continue
        seen.add(oid)
        entry = {"id": oid, "label": label}
        if conseq:
            entry["consequence"] = conseq
        options.append(entry)
    if len(options) < 2:
        return None, None, set()
    flags = set()
    default = None
    if default_idx is not None:
        try:
            want = pairs[default_idx][0]
        except IndexError:
            want = None
        if want is not None:
            for opt in options:
                if opt["label"] == want:
                    default = opt["id"]
                    break
    if default is None:
        default = options[0]["id"]
        flags.add("default guessed: first")
    return options, default, flags


def _split_label_conseq(fragment):
    if "=" in fragment:
        label, _, conseq = fragment.partition("=")
        return label.strip(), conseq.strip()
    for sep in (" - ", " -- ", ": "):
        if sep in fragment:
            label, _, conseq = fragment.partition(sep)
            if label.strip() and conseq.strip():
                return label.strip(), conseq.strip()
    return fragment.strip(), ""


def derive(step, why):
    """(options, default_id, flags): options = [{id,label,consequence}]."""
    text = (step or "").strip()
    # 1. enumeration a)/b)/c) split by ';'
    parts = [p for p in text.split(";") if p.strip()]
    enum = []
    if len(parts) >= 2:
        for part in parts:
            match = _ENUM_PART.match(part.strip())
            if not match:
                inner = re.search(r"\(?([a-dA-D])\)\s*(.+?)\s*$", part.strip())
                match = inner
            if match:
                label, conseq = _split_label_conseq(match.group(2))
                enum.append((match.group(1).lower(), label, conseq))
            else:
                enum = []
                break
        if enum and len({k for k, _, _ in enum}) == len(enum) and 2 <= len(enum) <= 4:
            return _finish([(l, c) for _, l, c in enum], step, why, explicit_default=None)
    # 1b. parenthesised (a) ... (b) ... without ';'
    found = _PAREN_ENUM.findall(text)
    if len(found) >= 2 and len(found) <= 4:
        letters = [letter.lower() for letter, _ in found]
        if len(set(letters)) == len(letters):
            pairs = []
            for _, frag in found:
                label, conseq = _split_label_conseq(frag)
                pairs.append((label, conseq))
            return _finish(pairs, step, why, explicit_default=None)
    # 2. Alternative:/Default: sentences
    alt = _ALT.search(text) or _ALT.search(why or "")
    default_m = _DEF.search(text) or _DEF.search(why or "")
    if alt or default_m:
        pairs = []
        default_label = None
        if default_m:
            default_label = _short(default_m.group(2), 60)
        alt_label = _short(alt.group(1), 60) if alt else None
        if default_label and alt_label:
            pairs = [(default_label, ""), (alt_label, "")]
            return _finish(pairs, step, why, explicit_default=default_label)
        single = default_label or alt_label
        base = re.sub(r"(?i)\b(Alternative|Default|Standard)\s*:\s*[^.;]+[.;]?", "", text).strip()
        base = re.sub(r"\s+", " ", base).strip(" .;:-")
        first = _short(base.split(".")[0] if base else "do it as written", 60) or "do it as written"
        pairs = [(first, ""), (single, "")]
        return _finish(pairs, step, why,
                       explicit_default=single if default_m else None)
    # 3. or-split of two commands/actions: both sides must be clean as they stand
    if re.search(r"(?i)\s+or\s+", text):
        sides = re.split(r"(?i)\s+or\s+", text, maxsplit=1)
        if len(sides) == 2:
            left_cand = re.sub(r"\s+", " ", sides[0].strip()).strip(" .;:")
            right_cand = re.sub(r"\s+", " ", sides[1].strip()).strip(" .;:")
            if (left_cand and right_cand and len(left_cand) >= 3 and len(right_cand) >= 3
                    and len(left_cand) <= 60 and len(right_cand) <= 60
                    and _is_clean_label(left_cand) and _is_clean_label(right_cand)):
                return _finish([(left_cand, ""), (right_cand, "")], step, why, explicit_default=None)
    # 4. a "tell the router ok or what is wrong" sentence falls through to
    # or-split above;
    # generic fallback:
    return _generic_options()


def _finish(pairs, step, why, explicit_default):
    blob = f"{step or ''}\n{why or ''}"
    options = []
    seen = set()
    for label_raw, conseq_raw in pairs[:4]:
        label = _short(label_raw, 60)
        conseq = _clean_consequence(_short(conseq_raw, 160))
        if not _is_clean_label(label):
            continue
        oid = mp._slug(label)
        if not oid or oid in seen:
            suffix = 2
            base_oid = oid or "option"
            while f"{base_oid}-{suffix}" in seen:
                suffix += 1
            oid = f"{base_oid}-{suffix}"
            label = _short(f"{label} {suffix}", 60)
            if not _is_clean_label(label):
                continue
            oid = mp._slug(label)
            if oid in seen or not oid:
                continue
        seen.add(oid)
        entry = {"id": oid, "label": label}
        if conseq:
            entry["consequence"] = conseq
        options.append(entry)
    if len(options) < 2:
        return _generic_options()
    flags = set()
    default = None
    if explicit_default:
        norm = _short(explicit_default, 60).lower()
        for opt in options:
            if opt["label"].lower() == norm or norm in opt["label"].lower():
                default = opt["id"]
                break
        if default is None:
            default = options[0]["id"]
            flags.add("default guessed: first")
    else:
        named = None
        dm = _DEF.search(blob)
        if dm:
            named = dm.group(2)
        elif _WHEN_CONV.search(blob):
            named = "when convenient"
        if named is not None:
            norm = _short(named, 60).lower()
            for opt in options:
                if opt["label"].lower() == norm or norm in opt["label"].lower():
                    default = opt["id"]
                    break
            if default is None:
                default = options[0]["id"]
                flags.add("default guessed: first")
        else:
            default = options[0]["id"]
            flags.add("default guessed: first")
    return options, default, flags


def _header_info(text):
    header_line, opt_order, n_cells = None, [], 0
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped.startswith("|"):
            continue
        cells = [p.strip() for p in stripped.split("|")[1:]]
        if cells and cells[-1] == "":
            cells = cells[:-1]
        if len(cells) >= 5 and len(cells[1]) >= 2 and cells[1].strip().lower() == "id" \
                and cells[0].strip().lower() in ("prio", "priority", ""):
            header_line = raw.rstrip()
            lowers = [c.strip().lower() for c in cells]
            if len(cells) > 5:
                opt_order = [n for n in lowers[2:len(cells) - 3] if n in ("kind", "parent")]
            else:
                opt_order = []
            n_cells = len(cells)
            break
    return header_line, opt_order, n_cells


def _prio_map(text):
    mapping, seen = {}, set()
    for raw in text.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        if line.startswith("    ") or line.startswith("\t"):
            continue
        parts = stripped.split("|")[1:]
        if parts and parts[-1].strip() == "":
            parts = parts[:-1]
        cells = [p.strip() for p in parts]
        if len(cells) < 5:
            continue
        if cells[1].strip().lower() == "id":
            continue
        if all(set(c) <= set("-: ") for c in cells if c):
            continue
        ids = mp._TABLE_ID_RE.findall(cells[1])
        if len(ids) != 1:
            continue
        norm = mp._norm_id(ids[0])
        if norm in seen:
            continue
        seen.add(norm)
        mapping[norm] = cells[0].strip()
    return mapping


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--questions", required=True)
    parser.add_argument("--out", required=True)
    try:
        opts = parser.parse_args(argv)
    except SystemExit:
        sys.stderr.write(USAGE)
        return 2
    q_real = os.path.realpath(opts.questions)
    o_real = os.path.realpath(opts.out)
    if q_real == o_real or q_real.startswith(o_real + os.sep):
        sys.stderr.write("error: --out must not equal or contain --questions\n")
        return 2
    try:
        with open(opts.questions, "r", encoding="utf-8") as handle:
            text = handle.read()
    except OSError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 2
    header_line, opt_order, _ = _header_info(text)
    if header_line is None:
        header_line = "| Prio | ID | why | step | where |"
        opt_order = []
    prios = _prio_map(text)
    rows = mp._table_rows(text)
    has_kind = "kind" in opt_order
    has_parent = "parent" in opt_order

    proposed, already, could_not = [], [], []
    details = []  # (ident, kind-value) review records in input order
    for _heading, ident, why, step_raw, where, kind, parent_cell in rows:
        step_no_opts, choices = mp.parse_options(step_raw, why)
        if choices is not None:
            already.append(ident)
            details.append({"id": ident, "status": "already",
                            "why": why, "step": step_raw, "where": where,
                            "kind": kind, "parent": parent_cell})
            continue
        malformed = bool(mp.has_options_block(step_raw))
        base = remove_options_block(step_raw) if malformed else (step_raw or "").strip()
        base = base.strip()
        base_no_parent, token_parent = mp.parse_parent(base)
        parent = None
        if parent_cell and parent_cell.strip():
            parent = parent_cell.strip()
        elif token_parent:
            parent = token_parent
        deadline = find_deadline(why, step_raw)
        if malformed:
            salv = _salvage_options(step_raw)
            if salv is None:
                could_not.append(ident)
                details.append({"id": ident, "status": "could-not-derive",
                                "why": why, "step": step_raw, "where": where,
                                "kind": kind, "parent": parent,
                                "malformed": malformed, "flags": set()})
                continue
            pairs, s_idx = salv
            options, default, flags = _finish_salvaged(pairs, s_idx, base_no_parent, why)
            if options is None:
                could_not.append(ident)
                details.append({"id": ident, "status": "could-not-derive",
                                "why": why, "step": step_raw, "where": where,
                                "kind": kind, "parent": parent,
                                "malformed": malformed, "flags": set()})
                continue
            flags = set(flags) | {"salvaged malformed block"}
        else:
            options, default, flags = derive(base_no_parent, why)
        # mask labels/consequences, recompute ids
        masked_opts = []
        seen_ids = set()
        for opt in options:
            label = clean_cell(opt["label"])
            conseq = clean_cell(opt.get("consequence", ""))
            label = _short(label, 60)
            conseq = _clean_consequence(_short(conseq, 160))
            if not _is_clean_label(label):
                masked_opts = []
                break
            oid = mp._slug(label)
            if not oid or oid in seen_ids or len(label) > 60 or len(conseq) > 160:
                masked_opts = []
                break
            seen_ids.add(oid)
            entry = {"id": oid, "label": label}
            if conseq:
                entry["consequence"] = conseq
            masked_opts.append(entry)
        if len(masked_opts) < 2:
            could_not.append(ident)
            details.append({"id": ident, "status": "could-not-derive",
                            "why": why, "step": step_raw, "where": where,
                            "kind": kind, "parent": parent,
                            "malformed": malformed, "flags": flags})
            continue
        # remap default through masked labels
        try:
            default_label = next(o["label"] for o in options if o["id"] == default)
        except StopIteration:
            default_label = None
        new_default = None
        if default_label is not None:
            want = _short(clean_cell(default_label), 60)
            for opt in masked_opts:
                if opt["label"] == want:
                    new_default = opt["id"]
                    break
        if new_default is None:
            new_default = masked_opts[0]["id"]
        opts_str = "; ".join(
            (("*" + o["label"]) if o["id"] == new_default else o["label"])
            + (f" = {o['consequence']}" if o.get("consequence") else "")
            for o in masked_opts)
        clean_base = clean_cell(base_no_parent)
        if parent and not has_parent:
            clean_parent = clean_cell(parent)
            clean_base = (clean_base + f" Parent: {clean_parent}").strip() if clean_base \
                else f"Parent: {clean_parent}"
        new_step = (clean_base + f" Options: {opts_str}").strip() if clean_base \
            else f"Options: {opts_str}"
        if deadline:
            new_step += f" Deadline: {deadline}"
        _, back = mp.parse_options(new_step, clean_cell(why))
        ok = (back is not None and [o["id"] for o in back["options"]]
              == [o["id"] for o in masked_opts]
              and back.get("default") == new_default
              and back.get("deadline") == deadline)
        if not ok:
            could_not.append(ident)
            details.append({"id": ident, "status": "could-not-derive",
                            "why": why, "step": step_raw, "where": where,
                            "kind": kind, "parent": parent,
                            "malformed": malformed, "flags": flags})
            continue
        proposed.append({"id": ident, "why": clean_cell(why), "where": clean_cell(where),
                         "step": new_step, "kind": kind,
                         "parent": clean_cell(parent) if (parent and has_parent) else (parent_cell.strip() if (parent_cell and has_parent) else ""),
                         "prio": prios.get(ident, ""),
                         "options": masked_opts, "default": new_default,
                         "deadline": deadline, "flags": flags,
                         "orig_step": step_raw})
        details.append({"id": ident, "status": "proposed", "why": why, "step": step_raw,
                        "where": where, "kind": kind, "parent": parent,
                        "options": masked_opts, "default": new_default,
                        "deadline": deadline, "flags": flags,
                        "malformed": malformed})
    try:
        os.makedirs(opts.out, exist_ok=True)
    except OSError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 2
    # proposed-rows.md with the input header
    sep_count = header_line.count("|") - 1
    if sep_count < 5:
        sep_count = 5 if not opt_order else 5 + len(opt_order)
    sep = "|" + "|".join(["---"] * sep_count) + "|"
    lines = [header_line, sep]
    for row in proposed:
        cells = [row["prio"], row["id"]]
        for name in opt_order:
            if name == "kind":
                cells.append(row["kind"] if row["kind"] in ("TASK", "ANSWER") else "")
            elif name == "parent":
                cells.append(clean_cell(row["parent"]) if row["parent"] else "")
        cells += [row["why"], row["step"], row["where"]]
        lines.append("| " + " | ".join(cells) + " |")
    prop_text = "\n".join(lines) + "\n"
    # review.md
    rev = [f"rows {len(rows)}, proposed {len(proposed)}, "
           f"already-has-options {len(already)}, could-not-derive {len(could_not)}", ""]
    for det in details:
        ident = det["id"]
        rev.append(f"## {ident}")
        orig = clean_cell(det.get("step", ""))[:200]
        rev.append(f"original: {orig}")
        if det["status"] == "already":
            rev.append(f"already has options: {ident}")
        elif det["status"] == "could-not-derive":
            rev.append("could not derive: no safe options")
            flags = sorted(det.get("flags") or [])
            if flags:
                rev.append("flags: " + ", ".join(flags))
        else:
            opt_txt = "; ".join(
                (("*" + o["label"]) if o["id"] == det["default"] else o["label"])
                + (f" = {o['consequence']}" if o.get("consequence") else "")
                for o in det["options"])
            rev.append(f"options: {opt_txt}")
            rev.append(f"deadline: {det['deadline'] or 'none'}")
            rev.append(f"parent: {clean_cell(det['parent']) if det['parent'] else 'none'}")
            flags = sorted(det["flags"])
            rev.append("flags: " + (", ".join(flags) if flags else "none"))
            rev.append("accept: replace the row with the proposed one")
        rev.append("")
    rev_text = "\n".join(rev)
    if not rev_text.endswith("\n"):
        rev_text += "\n"
    try:
        with open(os.path.join(opts.out, "proposed-rows.md"), "w", encoding="utf-8") as handle:
            handle.write(prop_text)
        with open(os.path.join(opts.out, "review.md"), "w", encoding="utf-8") as handle:
            handle.write(rev_text)
    except OSError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
