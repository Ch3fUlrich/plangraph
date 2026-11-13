# Rust MCP contract — slice 1

`rust/pg-mcp` is a workspace library, not a replacement stdio server or door.
`tools_list() -> serde_json::Value` embeds the committed
`tests/fixtures/keeper_mcp_tools.json` with `include_str!`; there are no runtime
files, credentials, keeper processes or HTTP calls.
`handle(&Value) -> Option<String>` returns a UTF-8 JSON-RPC reply with Python's
key order, comma/colon spacing and trailing newline; notifications are silent.
Initialize defaults to `2025-06-18` and echoes a nonempty requested version.
Every `tools/call` returns an MCP result with `isError: true` and text
`not_implemented: tools/call`, including mutating and fleet-bus tools.

The Python snapshot test checks the fixture against the unmodified
`keeper.mcp.TOOLS`, and captures recorded reply bytes by calling the production
`serve()` with in-memory streams and a keeper client that refuses all calls.
It also checks decoded responses against `handle()` and exercises a mixed
stream, including blank lines, checking both output bytes and reply flushes.
Rust tests compare the schema and response bytes (numeric/string ids,
default/echoed/invalid versions, Unicode and escaping, ping, invalid requests,
unknown/non-string methods, missing/null ids and explicit notifications).
An empty fixture response means zero bytes from Python and `None` from Rust,
not a serialized JSON null. Error responses include exactly one trailing LF.
Non-string methods use Python's string/repr spelling in method-not-found errors.
`tools/call` reply contents deliberately differ from Python in this local slice;
the Rust tests check every advertised tool's `not_implemented` result instead.
Neither test regenerates fixtures. After reviewing an intentional contract
change, explicitly regenerate once from the repository root:

```sh
PYTHONPATH=src:tests python3 -c 'from test_keeper_mcp_snapshot import FIXTURES, write_fixtures; write_fixtures(FIXTURES)'
python3 -m pytest -q tests/test_keeper_mcp_snapshot.py
(cd rust && cargo test -p pg-mcp)
```

Existing Python and Rust CI jobs discover these tests. Production keeps using
Python MCP; the snapshot also advertises the router-only `close_merged` GO close.
Python tool schemas live in `mcp_tools.py`, separate from the stdio transport's 800-line budget; existing tool contracts are unchanged.
HTTP proxying, auth parity, shadow diffs, seeded mutation replay,
role canary configuration and deleting Python MCP are later slices.
