#!/usr/bin/env python3
"""Sender-side secret transfer helper (DECISIONS 5.9 / D-944 order).

Order enforced: recipient add -> test -> confirm -> send -> shred.
Encryption is done by the `age` binary (argument lists, never a shell).
Secret content is never printed or logged.
"""
import argparse
import datetime
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile

RECIPIENT_RE = re.compile(r"^age1[0-9a-z]{58}$")
PREFIX_RE = re.compile(r"^[0-9a-f]{8}$")
DEFAULT_STATE = os.path.join("~", ".local", "share", "plangraph", "secret-transfer.json")


class Refuse(Exception):
    pass


def now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load(path):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return {"recipients": {}}


def save(path, state):
    d = os.path.dirname(path) or "."
    os.makedirs(d, mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".st-")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(state, f, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def get_rec(state, name):
    r = state["recipients"].get(name)
    if r is None:
        raise Refuse("unknown recipient %r" % name)
    return r


def age_encrypt(recipient, out, infile=None, data=None):
    cmd = ["age", "-r", recipient, "-o", out]
    if infile is not None:
        cmd.append(infile)
    try:
        p = subprocess.run(cmd, input=data, capture_output=True, shell=False)
    except FileNotFoundError:
        raise Refuse("age binary not found")
    if p.returncode != 0:
        raise Refuse("age failed (exit %d)" % p.returncode)


def check_secret_file(path):
    try:
        st = os.lstat(path)
    except OSError:
        raise Refuse("secret file not found")
    if stat.S_ISLNK(st.st_mode):
        raise Refuse("secret file is a symlink")
    if not stat.S_ISREG(st.st_mode):
        raise Refuse("secret file is not a regular file")
    if stat.S_IMODE(st.st_mode) != 0o600:
        raise Refuse("secret file mode must be 0600, is %o" % stat.S_IMODE(st.st_mode))


def sha_prefix_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()[:8]


def cmd_add(a, st):
    if not RECIPIENT_RE.match(a.recipient):
        raise Refuse("invalid age recipient")
    old = st["recipients"].get(a.name)
    if old and old.get("recipient") == a.recipient:
        return 0
    st["recipients"][a.name] = {"recipient": a.recipient, "added_at": now()}
    print("recipient %s stored (untested)" % a.name)
    return 1


def cmd_test(a, st):
    r = get_rec(st, a.name)
    dummy = secrets.token_bytes(32)
    age_encrypt(r["recipient"], a.out, data=dummy)
    r["pending_test"] = {"expected_prefix": hashlib.sha256(dummy).hexdigest()[:8], "at": now()}
    r.pop("test_passed_at", None)
    print("%s %d" % (a.out, os.path.getsize(a.out)))
    return 1


def cmd_confirm(a, st):
    r = get_rec(st, a.name)
    pt = r.get("pending_test")
    if not pt:
        raise Refuse("no pending test")
    if not PREFIX_RE.match(a.prefix) or a.prefix != pt["expected_prefix"]:
        raise Refuse("prefix mismatch; recipient stays unconfirmed")
    r["test_passed_at"] = now()
    r.pop("pending_test", None)
    print("recipient %s confirmed" % a.name)
    return 1


def cmd_send(a, st):
    r = get_rec(st, a.name)
    if not r.get("test_passed_at"):
        raise Refuse("recipient %s has no passed test; run test and confirm first" % a.name)
    check_secret_file(a.infile)
    age_encrypt(r["recipient"], a.out, infile=a.infile)
    r["sent_at"] = now()
    r["sent_sha256_prefix"] = sha_prefix_file(a.infile)
    r.pop("sent_and_shredded_at", None)
    print("%s %d %s" % (a.out, os.path.getsize(a.out), r["sent_sha256_prefix"]))
    return 1


def cmd_shred(a, st):
    r = get_rec(st, a.name)
    if not r.get("sent_at"):
        raise Refuse("shred only after send")
    check_secret_file(a.infile)
    if shutil.which("shred"):
        subprocess.run(["shred", "-u", a.infile], check=True, shell=False)
    else:
        size = os.path.getsize(a.infile)
        with open(a.infile, "r+b") as f:
            f.write(b"\0" * size)
            f.flush()
            os.fsync(f.fileno())
        os.unlink(a.infile)
    r["sent_and_shredded_at"] = now()
    print("shredded")
    return 1


def parser():
    p = argparse.ArgumentParser(prog="secret_transfer")
    p.add_argument("--state", default=None)
    sub = p.add_subparsers(dest="cmd", required=True)
    ra = sub.add_parser("recipient")
    rs = ra.add_subparsers(dest="rcmd", required=True)
    x = rs.add_parser("add")
    x.add_argument("name")
    x.add_argument("recipient")
    x.set_defaults(fn=cmd_add)
    x = sub.add_parser("test")
    x.add_argument("name")
    x.add_argument("--out", required=True)
    x.set_defaults(fn=cmd_test)
    x = sub.add_parser("confirm")
    x.add_argument("name")
    x.add_argument("prefix")
    x.set_defaults(fn=cmd_confirm)
    x = sub.add_parser("send")
    x.add_argument("name")
    x.add_argument("--in", dest="infile", required=True)
    x.add_argument("--out", required=True)
    x.set_defaults(fn=cmd_send)
    x = sub.add_parser("shred")
    x.add_argument("name")
    x.add_argument("--in", dest="infile", required=True)
    x.set_defaults(fn=cmd_shred)
    return p


def main(argv=None):
    a = parser().parse_args(argv)
    path = os.path.expanduser(a.state or DEFAULT_STATE)
    try:
        st = load(path)
        if a.fn(a, st):
            save(path, st)
        return 0
    except Refuse as e:
        # a refused confirm must still persist nothing new; state unchanged
        print("REFUSED: %s" % e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
