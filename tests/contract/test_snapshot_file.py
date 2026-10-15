"""Contract 12: the on-disk snapshot (``plan.snapshot.json``) and its signature.

The file is the body of ``GET /v1/snapshot`` rewritten atomically after every
committed write.  With a signing key configured a detached ed25519 signature
sits next to it (``<snapshot>.sig``); ``pg admin verify-snapshot`` checks it.
Local keeper only (the file lives on the keeper host).
"""
import base64
import json

import pytest

pytest.importorskip("cryptography")

import contract_harness as harness  # noqa: E402
from contract_schema import assert_valid  # noqa: E402
from contract_world import make_plan, ok, snapshot  # noqa: E402


@pytest.fixture(scope="module")
def signed(tmp_path_factory):
    if harness.from_env() is not None:
        pytest.skip("snapshot file checks need a local keeper")
    keeper = harness.start_local(tmp_path_factory.mktemp("signed") / "k", signing=True)
    yield keeper
    keeper.stop()


def test_snapshot_file_follows_every_write_and_matches_the_http_body(signed):
    plan = make_plan(signed)
    path = signed.dir / "plan.snapshot.json"
    on_disk = json.loads(path.read_bytes())
    assert_valid(on_disk, "Snapshot")
    live = snapshot(signed)
    assert on_disk["head"] == live["head"] and on_disk["head_seq"] == live["head_seq"]
    assert {i["key"] for i in on_disk["items"]} == {i["key"] for i in live["items"]}
    assert plan.key in {i["key"] for i in on_disk["items"]}
    ok(signed.post("claim", "worker1", {"key": plan.key}), "Claim")
    again = json.loads(path.read_bytes())
    assert again["head_seq"] > on_disk["head_seq"]
    assert not list(signed.dir.glob("plan.snapshot.json.*.tmp"))        # atomic: no temp leftovers


def test_signature_is_ed25519_over_the_domain_separated_bytes(signed):
    from cryptography.hazmat.primitives import serialization
    make_plan(signed)
    body = (signed.dir / "plan.snapshot.json").read_bytes()
    sig = base64.b64decode((signed.dir / "plan.snapshot.json.sig").read_text().strip(), validate=True)
    assert len(sig) == 64
    pub = serialization.load_pem_public_key((signed.dir / "snap.pub").read_bytes())
    pub.verify(sig, b"plangraph-snapshot-v1\n" + body)          # raises on mismatch
    good = signed.admin("verify-snapshot")
    assert good.returncode == 0 and "signature ok" in good.stdout


def test_tampered_snapshot_fails_verification(signed):
    make_plan(signed)
    path = signed.dir / "plan.snapshot.json"
    original = path.read_bytes()
    try:
        path.write_bytes(original.replace(b'"items"', b'"itemz"', 1))
        bad = signed.admin("verify-snapshot")
        assert bad.returncode == 1 and "NOT verified" in bad.stderr
    finally:
        path.write_bytes(original)
    assert signed.admin("verify-snapshot").returncode == 0
