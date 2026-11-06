"""Local fake HTTP server tests; no external service or credentials required."""

import base64
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location(
    "oc_lane_wake", Path(__file__).resolve().parents[1] / "tools" / "oc_lane_wake.py")
wake = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(wake)


class WakeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.secret = "synthetic-" + "test-value"
        self.posts = []
        self.status = 200
        self.messages = [{"info": {"id": "msg_one", "role": "assistant",
                                  "time": {"created": time.time() - 900,
                                           "completed": time.time() - 800}}}]
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                owner.assertEqual(self.path, "/api/session/ses_test/message?order=desc&limit=5")
                expected = base64.b64encode(("opencode:" + owner.secret).encode()).decode()
                owner.assertEqual(self.headers["Authorization"], "Basic " + expected)
                self.send_response(owner.status)
                self.end_headers()
                data = {"data": {"items": owner.messages}} if owner.status == 200 else owner.secret
                self.wfile.write(json.dumps(data).encode())

            def do_POST(self):
                owner.assertEqual(self.path, "/api/session/ses_test/prompt")
                owner.posts.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                self.send_response(owner.status)
                self.end_headers()
                self.wfile.write(b"{}")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.addCleanup(self.close_server)
        (self.root / "password").write_text(self.secret)
        (self.root / "password").chmod(0o600)
        (self.root / "inbox").write_text("first task\nsecond task\n")
        self.args = ["--port", str(self.server.server_port), "--password-file",
                     str(self.root / "password"), "--session-id", "ses_test"]
        for name in ("inbox", "cursor", "log"):
            self.args += ["--" + name, str(self.root / name)]

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def run_wake(self, command="once", extra=()):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = wake.main([command, *self.args, *extra])
        artifacts = out.getvalue() + err.getvalue()
        for name in ("log", "cursor", "cursor.lock"):
            if (self.root / name).exists():
                artifacts += (self.root / name).read_text()
        self.assertNotIn(self.secret, artifacts)
        encoded = base64.b64encode(("opencode:" + self.secret).encode()).decode()
        self.assertNotIn(encoded, artifacts)
        return code

    def test_idle_posts_once_and_persists(self):
        self.assertEqual(self.run_wake(), 0)
        self.assertEqual(self.run_wake(), 0)
        self.assertEqual(self.posts, [{"text": "New task from the L1: first task"}])
        self.assertEqual(json.loads((self.root / "cursor").read_text())["consumed"], 1)
        self.messages[0]["info"]["id"] = "msg_two"
        self.assertEqual(self.run_wake(), 0)
        self.assertEqual(self.posts[-1]["text"], "New task from the L1: second task")

    def test_busy_no_post(self):
        for status in ("running", "pending"):
            self.messages[0]["info"]["status"] = status
            self.assertEqual(self.run_wake(), 0)
        self.assertFalse(self.posts)

    def test_incomplete_assistant_and_recent_message(self):
        del self.messages[0]["info"]["time"]["completed"]
        self.assertEqual(self.run_wake(), 0)
        self.messages[0]["info"]["time"]["completed"] = time.time()
        self.assertEqual(self.run_wake(), 0)
        self.assertFalse(self.posts)

    def test_empty_inbox(self):
        (self.root / "inbox").write_text("")
        self.assertEqual(self.run_wake(), 0)
        self.assertFalse(self.posts)
        self.assertEqual(json.loads((self.root / "log").read_text())["result"], "empty")

    def test_stop_loop(self):
        (self.root / "inbox").write_text("STOP\n")
        with patch.object(wake.time, "sleep", side_effect=AssertionError("must stop")):
            self.assertEqual(self.run_wake("loop"), 0)
        self.assertFalse(self.posts)

    def test_password_scrub_401(self):
        self.status = 401
        self.assertEqual(self.run_wake(), 2)
        self.assertFalse(self.posts)

    def test_server_down(self):
        # A bound but non-listening socket cannot accept a request.
        import socket
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.args[1] = str(sock.getsockname()[1])
            self.assertEqual(self.run_wake(), 3)

    def test_crash_after_post_never_replays(self):
        with patch.object(wake, "event", side_effect=RuntimeError("simulated crash")):
            with self.assertRaises(RuntimeError):
                self.run_wake()
        self.messages[0]["info"]["id"] = "msg_after_crash"
        self.assertEqual(self.run_wake(), 0)
        self.assertEqual(len(self.posts), 2)
        self.assertNotEqual(self.posts[0], self.posts[1])

    def test_atomic_reservation_failure_prevents_post(self):
        with patch.object(wake.os, "replace", side_effect=OSError("disk failure")):
            self.assertEqual(self.run_wake(), 2)
        self.assertFalse(self.posts)
        self.assertFalse((self.root / "cursor").exists())

    def test_password_mode_and_state(self):
        (self.root / "password").chmod(0o644)
        self.assertEqual(self.run_wake(), 2)
        (self.root / "password").chmod(0o600)
        (self.root / "state").write_text(json.dumps({"session_id": "ses_test"}))
        self.args[-8:-6] = ["--state", str(self.root / "state")]
        self.assertEqual(self.run_wake(), 0)


if __name__ == "__main__":
    unittest.main()
