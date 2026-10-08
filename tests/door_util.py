"""Drive every door (HTTP API, pg CLI) against one in-process keeper."""
from __future__ import annotations

import contextlib
import io
import json
import os
import threading
import urllib.error
import urllib.request

from plangraph.keeper import api, cli, config


class Doors:
    """A live API server over ``world`` plus helpers for the HTTP and CLI doors."""

    def __init__(self, world, tmp_path):
        path = tmp_path / "keeper.json"
        path.write_text(json.dumps({
            "db": str(world.path), "snapshot": str(tmp_path / "snap.json"),
            "bind": "127.0.0.1:0"}), encoding="utf-8")
        os.chmod(path, 0o600)
        self.world = world
        self.tmp = tmp_path
        self.config_path = path
        self.server = api.make_server(world.store, config.load_config(path),
                                      port=0)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.token_files = {}

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def http(self, who, method, path, body=None):
        """``(status, parsed json or text)`` for one request as principal *who*."""
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.url + path, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.world.tok(who)}")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req) as resp:
                status, raw = resp.status, resp.read().decode()
        except urllib.error.HTTPError as exc:
            status, raw = exc.code, exc.read().decode()
        try:
            return status, json.loads(raw)
        except ValueError:
            return status, raw

    def pg(self, who, *argv, json_out=False):
        """``(exit code, stdout, stderr)`` of ``pg [--json] argv`` as *who*."""
        token_file = self.token_files.get(who)
        if token_file is None:
            token_file = self.tmp / f"{who}.token"
            token_file.write_text(self.world.tok(who), encoding="utf-8")
            os.chmod(token_file, 0o600)
            self.token_files[who] = token_file
        env = {"PG_URL": self.url, "PG_CONFIG": str(self.config_path),
               "PG_TOKEN_FILE": str(token_file)}
        saved = {k: os.environ.get(k) for k in env}
        os.environ.update(env)
        out, err = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = cli.main((["--json"] if json_out else []) + list(argv))
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        return code, out.getvalue(), err.getvalue()
