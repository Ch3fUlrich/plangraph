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
import stat
import subprocess
import sys

RECIPIENT_RE = re.compile(r"^age1[0-9a-z]{58}$")
PREFIX_RE = re.compile(r"^[0-9a-f]{8}$")
DEFAULT_STATE = os.path.join("~", ".local", "share", "plangraph", "secret-transfer.json")


class Refuse(Exception):
    pass


def now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _check_dir(d):
    try:
        st = os.lstat(d)
    except FileNotFoundError:
        return
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise Refuse("state dir is a symlink or not a directory")
    if st.st_uid != os.geteuid() or st.st_mode & 0o077:
        raise Refuse("state dir must be owned by you with mode 0700")


def load(path):
    _check_dir(os.path.dirname(path) or ".")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return {"recipients": {}}
    except OSError:
        raise Refuse("cannot open state file (symlink?)")
    with os.fdopen(fd) as f:
        st = os.fstat(f.fileno())
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid() or st.st_mode & 0o077:
            raise Refuse("state file must be a regular file owned by you with mode 0600")
        return json.load(f)


def save(path, state):
    d = os.path.dirname(path) or "."
    _check_dir(d)
    os.makedirs(d, mode=0o700, exist_ok=True)
    _check_dir(d)
    tmp = os.path.join(d, ".st-%s.tmp" % secrets.token_hex(8))
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(state, f, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
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


def age_encrypt(recipient, data):
    """Encrypt bytes: age -r R, plaintext on stdin, ciphertext on stdout."""
    if not RECIPIENT_RE.match(recipient):
        raise Refuse("invalid age recipient")
    try:
        p = subprocess.run(["age", "-r", recipient], input=data,
                           capture_output=True, shell=False)
    except FileNotFoundError:
        raise Refuse("age binary not found")
    if p.returncode != 0 or not p.stdout:
        raise Refuse("age failed (exit %d)" % p.returncode)
    return p.stdout


def check_out(out):
    try:
        os.lstat(out)
    except FileNotFoundError:
        return
    raise Refuse("output path exists (or is a symlink)")


def write_out(out, blob):
    try:
        fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    except OSError:
        raise Refuse("cannot create output file (exists or symlink)")
    with os.fdopen(fd, "wb") as f:
        f.write(blob)
        f.flush()
        os.fsync(f.fileno())


def open_secret(path, flags=os.O_RDONLY):
    """Open O_NOFOLLOW, verify the descriptor (regular, 0600, owned by euid)."""
    try:
        fd = os.open(path, flags | os.O_NOFOLLOW)
    except OSError:
        raise Refuse("cannot open secret file (missing or symlink)")
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise Refuse("secret file is not a regular file")
        if stat.S_IMODE(st.st_mode) != 0o600:
            raise Refuse("secret file mode must be 0600, is %o" % stat.S_IMODE(st.st_mode))
        if st.st_uid != os.geteuid():
            raise Refuse("secret file not owned by current user")
    except BaseException:
        os.close(fd)
        raise
    return fd, st


def read_fd(fd):
    chunks = []
    while True:
        c = os.read(fd, 65536)
        if not c:
            return b"".join(chunks)
        chunks.append(c)


def parse_ts(ts):
    return datetime.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=datetime.timezone.utc)


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
    check_out(a.out)
    dummy = secrets.token_bytes(32)
    blob = age_encrypt(r["recipient"], dummy)
    write_out(a.out, blob)
    r["pending_test"] = {"expected_prefix": hashlib.sha256(dummy).hexdigest()[:8], "at": now()}
    r.pop("test_passed_at", None)
    print("%s %d" % (a.out, len(blob)))
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
    tp = r.get("test_passed_at")
    if not tp:
        raise Refuse("recipient %s has no passed test; run test and confirm first" % a.name)
    try:
        age_s = (datetime.datetime.now(datetime.timezone.utc) - parse_ts(tp)).total_seconds()
    except ValueError:
        raise Refuse("bad test timestamp in state")
    if age_s > a.max_test_age * 3600:
        raise Refuse("passed test is older than %g h; re-run test and confirm" % a.max_test_age)
    check_out(a.out)
    fd, sst = open_secret(a.infile)
    try:
        data = read_fd(fd)
    finally:
        os.close(fd)
    blob = age_encrypt(r["recipient"], data)
    write_out(a.out, blob)
    digest = hashlib.sha256(data).hexdigest()
    r["sent_at"] = now()
    r["sent"] = {"sha256": digest, "dev": sst.st_dev, "ino": sst.st_ino}
    r.pop("sent_and_shredded_at", None)
    print("%s %d %s" % (a.out, len(blob), digest[:8]))
    return 1


def cmd_shred(a, st):
    r = get_rec(st, a.name)
    sent = r.get("sent")
    if not sent:
        raise Refuse("shred only after send")
    fd, fst = open_secret(a.infile, os.O_RDWR)
    try:
        if (fst.st_dev, fst.st_ino) != (sent["dev"], sent["ino"]):
            raise Refuse("file is not the one that was sent (inode differs)")
        data = read_fd(fd)
        if hashlib.sha256(data).hexdigest() != sent["sha256"]:
            raise Refuse("file content differs from what was sent")
        # overwrite in place via the verified descriptor, then unlink
        os.lseek(fd, 0, os.SEEK_SET)
        for pattern in (b"\xff", b"\x00"):
            os.lseek(fd, 0, os.SEEK_SET)
            left = fst.st_size
            while left > 0:
                n = os.write(fd, pattern * min(left, 65536))
                left -= n
            os.fsync(fd)
        cur = os.lstat(a.infile)
        if (cur.st_dev, cur.st_ino) != (fst.st_dev, fst.st_ino):
            raise Refuse("file replaced during shred; not unlinked")
        os.unlink(a.infile)
    finally:
        os.close(fd)
    r["sent_and_shredded_at"] = now()
    print("shredded")
    return 1


def parser():
    p = argparse.ArgumentParser(prog="secret_transfer")
    p.add_argument("--state", default=None)
    sub = p.add_subparsers(dest="cmd", required=True)
    ra = sub.add_parser("recipient", help="manage recipients")
    rs = ra.add_subparsers(dest="rcmd", required=True)
    x = rs.add_parser("add", help="store a receiver age recipient", description="Store a receiver public age1... recipient (a different key resets its test).")
    x.add_argument("name")
    x.add_argument("recipient")
    x.set_defaults(fn=cmd_add)
    x = sub.add_parser("test", help="write a dummy encrypted test file", description="Encrypt 32 random bytes to the recipient; the receiver reports the sha256 8-hex prefix independently.")
    x.add_argument("name")
    x.add_argument("--out", required=True)
    x.set_defaults(fn=cmd_test)
    x = sub.add_parser("confirm", help="confirm the receiver reported prefix", description="Compare the receiver-reported 8-hex prefix with the pending test; mismatch exits 2.")
    x.add_argument("name")
    x.add_argument("prefix")
    x.set_defaults(fn=cmd_confirm)
    x = sub.add_parser("send", help="encrypt the secret (needs fresh passed test)", description="Encrypt a 0600 regular file to the confirmed recipient; prints out path, size, sha256 8-hex prefix.")
    x.add_argument("name")
    x.add_argument("--in", dest="infile", required=True)
    x.add_argument("--out", required=True)
    x.add_argument("--max-test-age", type=float, default=24.0,
                   help="max age in hours of the passed test (default 24)")
    x.set_defaults(fn=cmd_send)
    x = sub.add_parser("shred", help="overwrite and unlink the sent plaintext", description="Only the exact file (inode and hash) recorded at send is overwritten and unlinked.")
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
