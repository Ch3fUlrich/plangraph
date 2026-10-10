"""Hermetic tests for tools/secret_transfer.py (fake `age` first on PATH)."""
import hashlib
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

TOOL = Path(__file__).resolve().parent.parent / "tools" / "secret_transfer.py"
REC = "age1" + "q" * 58
REC2 = "age1" + "p" * 58
SECRET = b"TOP-SECRET-CONTENT-xyzzy-12345"

FAKE_AGE = """#!{py}
import sys, os
a = sys.argv[1:]
open(os.path.join(os.path.dirname(__file__), "argv.log"), "a").write(repr(a) + "\\n")
if a[:1] != ["-r"] or len(a) != 2:
    sys.exit(9)
sys.stdout.buffer.write(b"FAKEAGE " + a[1].encode() + b"\\n" + sys.stdin.buffer.read())
"""

FAKE_SHRED = """#!{py}
import os
open(os.path.join(os.path.dirname(__file__), "shred.called"), "w").write("x")
"""


@pytest.fixture
def env(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    f = bindir / "age"
    f.write_text(FAKE_AGE.format(py=sys.executable))
    f.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    s2 = bindir / "shred"
    s2.write_text(FAKE_SHRED.format(py=sys.executable))
    s2.chmod(0o755)
    e = {"PATH": "%s:%s" % (bindir, os.path.dirname(sys.executable)), "HOME": str(home)}
    return e, tmp_path


def run(env, *args):
    e, tp = env
    p = subprocess.run([sys.executable, str(TOOL), "--state", str(tp / "st" / "s.json"), *args],
                       env=e, capture_output=True, cwd=tp)
    return p


def secret_file(tp, mode=0o600):
    s = tp / "secret"
    s.write_bytes(SECRET)
    s.chmod(mode)
    return s


def state(tp):
    return json.loads((tp / "st" / "s.json").read_text())


def dummy_prefix(tp, out="t.age"):
    blob = (tp / out).read_bytes()
    return hashlib.sha256(blob.split(b"\n", 1)[1]).hexdigest()[:8]


def confirmed(env):
    _, tp = env
    assert run(env, "recipient", "add", "bob", REC).returncode == 0
    p = run(env, "test", "bob", "--out", str(tp / "t.age"))
    assert p.returncode == 0
    assert run(env, "confirm", "bob", dummy_prefix(tp)).returncode == 0


def test_recipient_validation(env):
    for bad in ["age1short", "AGE1" + "q" * 58, "age1" + "Q" * 58, "x" * 62, REC + "q"]:
        assert run(env, "recipient", "add", "bob", bad).returncode == 2
    assert run(env, "recipient", "add", "bob", REC).returncode == 0


def test_state_perms(env):
    _, tp = env
    run(env, "recipient", "add", "bob", REC)
    assert stat.S_IMODE((tp / "st" / "s.json").stat().st_mode) == 0o600
    assert stat.S_IMODE((tp / "st").stat().st_mode) == 0o700


def test_test_prints_no_prefix_and_confirm_flow(env):
    _, tp = env
    run(env, "recipient", "add", "bob", REC)
    p = run(env, "test", "bob", "--out", str(tp / "t.age"))
    pre = dummy_prefix(tp)
    assert pre not in (p.stdout + p.stderr).decode()
    assert state(tp)["recipients"]["bob"]["pending_test"]["expected_prefix"] == pre
    assert b"FAKEAGE " + REC.encode() in (tp / "t.age").read_bytes()


def test_send_refused_before_confirm(env):
    _, tp = env
    run(env, "recipient", "add", "bob", REC)
    p = run(env, "send", "bob", "--in", str(secret_file(tp)), "--out", str(tp / "o.age"))
    assert p.returncode == 2 and not (tp / "o.age").exists()


def test_send_refused_after_mismatch(env):
    _, tp = env
    run(env, "recipient", "add", "bob", REC)
    run(env, "test", "bob", "--out", str(tp / "t.age"))
    wrong = "00000000" if dummy_prefix(tp) != "00000000" else "11111111"
    assert run(env, "confirm", "bob", wrong).returncode == 2
    assert "test_passed_at" not in state(tp)["recipients"]["bob"]
    p = run(env, "send", "bob", "--in", str(secret_file(tp)), "--out", str(tp / "o.age"))
    assert p.returncode == 2 and not (tp / "o.age").exists()


def test_send_allowed_after_match_and_no_leak(env):
    _, tp = env
    confirmed(env)
    assert "pending_test" not in state(tp)["recipients"]["bob"]
    p = run(env, "send", "bob", "--in", str(secret_file(tp)), "--out", str(tp / "o.age"))
    assert p.returncode == 0
    out = p.stdout + p.stderr
    assert SECRET not in out
    assert hashlib.sha256(SECRET).hexdigest()[:8] in out.decode()
    assert SECRET in (tp / "o.age").read_bytes()  # fake age only
    assert stat.S_IMODE((tp / "o.age").stat().st_mode) == 0o644


def test_no_dummy_in_output(env):
    _, tp = env
    run(env, "recipient", "add", "bob", REC)
    p = run(env, "test", "bob", "--out", str(tp / "t.age"))
    dummy = (tp / "t.age").read_bytes().split(b"\n", 1)[1]
    assert dummy not in p.stdout + p.stderr and dummy.hex() not in (p.stdout + p.stderr).decode()


def test_readd_different_resets(env):
    _, tp = env
    confirmed(env)
    run(env, "recipient", "add", "bob", REC)  # same: kept
    assert "test_passed_at" in state(tp)["recipients"]["bob"]
    run(env, "recipient", "add", "bob", REC2)
    assert "test_passed_at" not in state(tp)["recipients"]["bob"]
    p = run(env, "send", "bob", "--in", str(secret_file(tp)), "--out", str(tp / "o.age"))
    assert p.returncode == 2


def test_file_mode_checks(env):
    _, tp = env
    confirmed(env)
    bad = secret_file(tp, 0o644)
    assert run(env, "send", "bob", "--in", str(bad), "--out", str(tp / "o.age")).returncode == 2
    good = secret_file(tp)
    link = tp / "lnk"
    link.symlink_to(good)
    assert run(env, "send", "bob", "--in", str(link), "--out", str(tp / "o.age")).returncode == 2
    (tp / "d").mkdir(mode=0o600)
    assert run(env, "send", "bob", "--in", str(tp / "d"), "--out", str(tp / "o.age")).returncode == 2
    assert not (tp / "o.age").exists()


def test_shred_order(env):
    _, tp = env
    confirmed(env)
    s = secret_file(tp)
    assert run(env, "shred", "bob", "--in", str(s)).returncode == 2 and s.exists()
    assert run(env, "send", "bob", "--in", str(s), "--out", str(tp / "o.age")).returncode == 0
    p = run(env, "shred", "bob", "--in", str(s))
    assert p.returncode == 0 and not s.exists()
    assert SECRET not in p.stdout + p.stderr
    assert "sent_and_shredded_at" in state(tp)["recipients"]["bob"]


def test_guard_hermetic(env):
    """Tool uses PATH age only (fake), no network modules, HOME is tmp."""
    src = TOOL.read_text()
    assert "shell=True" not in src
    for mod in ("socket", "urllib", "http", "requests"):
        assert "import " + mod not in src
    e, tp = env
    assert e["HOME"].startswith(str(tp))
    run(env, "recipient", "add", "bob", REC)
    assert not (Path(e["HOME"]) / ".config").exists()


def argv_log(tp):
    f = tp / "bin" / "argv.log"
    return f.read_text() if f.exists() else ""


def test_secret_path_never_in_argv_and_real_tools_not_used(env):
    _, tp = env
    confirmed(env)
    s = secret_file(tp)
    run(env, "send", "bob", "--in", str(s), "--out", str(tp / "o.age"))
    log = argv_log(tp)
    assert log and str(s) not in log and "secret" not in log
    assert "'-o'" not in log
    run(env, "shred", "bob", "--in", str(s))
    assert not (tp / "bin" / "shred.called").exists()
    assert env[0]["PATH"].split(":")[0] == str(tp / "bin") and len(env[0]["PATH"].split(":")) == 2


def test_dash_leading_secret_name(env):
    _, tp = env
    confirmed(env)
    s = tp / "-r"
    s.write_bytes(SECRET)
    s.chmod(0o600)
    p = run(env, "send", "bob", "--in", str(s), "--out", str(tp / "o.age"))
    assert p.returncode == 0 and "-r" not in argv_log(tp).replace("['-r', '", "")


def set_passed_at(tp, iso):
    f = tp / "st" / "s.json"
    d = json.loads(f.read_text())
    d["recipients"]["bob"]["test_passed_at"] = iso
    f.write_text(json.dumps(d))


def test_stale_test_refused(env):
    _, tp = env
    confirmed(env)
    set_passed_at(tp, "2020-01-01T00:00:00Z")
    s = secret_file(tp)
    p = run(env, "send", "bob", "--in", str(s), "--out", str(tp / "o.age"))
    assert p.returncode == 2 and not (tp / "o.age").exists()
    p = run(env, "send", "bob", "--in", str(s), "--out", str(tp / "o.age"),
            "--max-test-age", "100000")
    assert p.returncode == 0


def test_out_symlink_or_existing_refused(env):
    _, tp = env
    confirmed(env)
    s = secret_file(tp)
    victim = tp / "victim"
    victim.write_text("keep")
    (tp / "lo").symlink_to(victim)
    assert run(env, "send", "bob", "--in", str(s), "--out", str(tp / "lo")).returncode == 2
    assert victim.read_text() == "keep"
    (tp / "ex").write_text("keep")
    assert run(env, "send", "bob", "--in", str(s), "--out", str(tp / "ex")).returncode == 2
    assert (tp / "ex").read_text() == "keep"
    assert run(env, "test", "bob", "--out", str(tp / "ex")).returncode == 2
    assert run(env, "test", "bob", "--out", str(tp / "lo")).returncode == 2
    assert victim.read_text() == "keep"


def test_state_symlink_refused(env):
    e, tp = env
    real = tp / "real.json"
    real.write_text('{"recipients": {}}')
    (tp / "st").mkdir()
    (tp / "st" / "s.json").symlink_to(real)
    assert run(env, "recipient", "add", "bob", REC).returncode == 2
    assert real.read_text() == '{"recipients": {}}'


def test_state_dir_symlink_refused(env):
    _, tp = env
    (tp / "realdir").mkdir()
    (tp / "st").symlink_to(tp / "realdir")
    assert run(env, "recipient", "add", "bob", REC).returncode == 2
    assert not list((tp / "realdir").iterdir())


def test_shred_requires_sent_file(env):
    _, tp = env
    confirmed(env)
    s = secret_file(tp)
    run(env, "send", "bob", "--in", str(s), "--out", str(tp / "o.age"))
    other = tp / "other"
    other.write_bytes(b"precious")
    other.chmod(0o600)
    assert run(env, "shred", "bob", "--in", str(other)).returncode == 2
    assert other.exists()
    # same inode, content changed after send
    s.write_bytes(b"changed")
    assert run(env, "shred", "bob", "--in", str(s)).returncode == 2 and s.exists()
    s.write_bytes(SECRET)
    # same content, different inode (replaced)
    os.link(s, tp / "keep")
    s.unlink()
    s.write_bytes(SECRET)
    s.chmod(0o600)
    assert run(env, "shred", "bob", "--in", str(s)).returncode == 2 and s.exists()


def test_help_per_subcommand(env):
    e, tp = env
    for sub in (["recipient", "add"], ["test"], ["confirm"], ["send"], ["shred"]):
        p = subprocess.run([sys.executable, str(TOOL), *sub, "--help"], env=e, capture_output=True)
        assert p.returncode == 0 and len(p.stdout) > 40
