# Fleet bus wire contract

VERSION: `fleet-bus/2`. This specification is transport- and language-neutral.
The executable conformance vectors live in `tests/fleet_bus/vectors/`.
All signing strings are UTF-8, without a trailing newline.

## Envelope and caps

POST is a UTF-8 JSON object. Required fields are `from`, `to`, `ts`, `nonce`,
`msg_id`, `text`. Optional fields are `trace_id`, `key`, `run_id`, `attempt_n`,
`kid`; unknown fields are rejected.

Identifiers are nonempty strings, capped at 64 Unicode code points except
`nonce` (128). They contain no controls, spaces (including all Unicode whitespace
recognized by `char.isspace()`), pipe delimiters or surrogates.
`attempt_n` is an integer from 1 through 1000, never a boolean.
`ts` is nonnegative integral Unix epoch seconds: an integer, finite integral
number, or ASCII decimal digit string; booleans and fractions are invalid.
`text` is a Unicode string encodable as UTF-8.

The complete body, including JSON whitespace, is capped at 4096 bytes.
Stored display text is capped at 2000 Unicode code points after replacing
C0/C1 controls and surrogates with spaces, collapsing whitespace runs to one
space, and trimming. Signing always uses the original text, not display text.

## Canonical forms and signing

Join the following fields with literal `|`, without escaping or added spaces:

```text
fleet-bus/2|post|<ts>|<nonce>|<from>|<to>|<msg_id>|<kid>|<text_sha256>
fleet-bus/2|read|<ts>|<nonce>|<reader>|<kid>|<cursor>
```

`ts` is normalized to base-10 integer seconds. Absent `kid` is the empty
string. `text_sha256` is lowercase hexadecimal SHA-256 of the original UTF-8
text. The signature is lowercase hexadecimal HMAC-SHA256 of the UTF-8 canonical
form using the resolved secret bytes. Compare signatures in constant time.
The resolver receives `kid` (or no-key identifier `None`) and returns secret
bytes or an unknown-key result. Never expose secrets or signatures in errors.

Only fields listed in the canonical form are authenticated; optional tracing,
grouping and attempt metadata are advisory, not authorization evidence.

## Validation and state

The first failing gate wins:

1. JSON object shape, required/unknown fields, types and identifier caps.
2. Body size.
3. Timestamp skew: at most 60 seconds in either direction, inclusive.
4. Key lookup (`kid`).
5. Signature verification.
6. Nonce replay check.
7. Replay capacity: refuse with `replay_window_full` (503) if 10000 live nonces remain globally. There is no per-key cap.
8. Rate: 30 authenticated requests per rolling 60 seconds per sender.
9. Purge expired nonces, record the nonce, then dedupe by `(from, msg_id)`.

Malformed JSON has no shape: if oversized it returns `too_large`, otherwise
`bad_request`. Valid JSON is shape-checked even when oversized.
Failed authentication never consumes a nonce or rate allowance. Authenticated
nonces are globally unique across POST and READ. Store each accepted message's
timestamp, not acceptance time. Only entries with `ts < now - 60` may be purged;
the inclusive boundary remains protected. Never evict an in-window nonce to make
room: a full window returns 503 without changing state. READ and POST share this
window, including capacity and expiry rules.
A holder of a valid signing key can fill the global window and cause 503 for all
senders until entries age out (at most `2*SKEW_S`). Integrity is preferred over
availability. Mitigation needs sender-to-key binding, a follow-up outside this item.
A 503 carries `retry_after_s = max(1, ceil(oldest live ts + 60 - now))`, globally scoped.
Cleanup uses one scan only when global storage reaches its cap, not on every
authenticated request. Expired entries may remain below
capacity; cleanup is committed only on acceptance. Live entries are never evicted.
Rate-rejected (429) requests consume no replay state: they neither insert nor
evict nonces and do not change message-ID dedupe state. Their nonces remain unseen;
clients should retry with a fresh nonce and fresh timestamp. An exact retry can
be accepted only while its original timestamp remains within the skew window
(`abs(now - ts) <= 60`) and all other gates pass. This prevents rejected floods from evicting
accepted nonces. The limiter removes events at or
before `now - 60`; rejected requests do not add a rate event. Its retry delay is
at least one second, rounded up to the first available slot.

Duplicate messages still pass replay and rate gates, then return the original
`id`, `ts`, `msg_id` with `dup: true`, without appending or replacing text.
New acknowledgements contain exactly `id`, `ts`, `msg_id`, `dup: false`.
Acknowledgement `ts` is server acceptance time in seconds. Retain 1000 records;
dedupe acknowledgements are retired with their records. Replay protection is
time-bounded and in-memory, not a durable exactly-once guarantee.

State belongs to one bus instance. Adapters must serialize authentication,
insertion and reads, supply a consistent clock, and enforce their own
transport ingress limits. The contract performs no network or filesystem IO.

## Cursors and signed reads

Record IDs have form `<ts_ms>-<seq6>`: server milliseconds and a six-digit
sequence starting at `000001`. The sequence increments within a millisecond;
clock rollback does not decrease IDs. Sequence overflow advances the logical
millisecond. Cursors are opaque retained record IDs, not timestamps.

An empty cursor starts at the oldest retained record. A known cursor resumes
strictly after that record. Cursor validation has two distinct layers:

- `check_read`, the transport authentication seam, rejects a cursor that is not
  a string or that contains `|` or a surrogate with **400 `bad_request`** (a shape
  error), once a signature
  header is present and before authentication. Such a cursor cannot be bound
  unambiguously into the UTF-8 signed canonical form. Real record IDs never contain
  `|` or surrogates. A missing or empty signature header returns `bad_signature` first;
  timestamp and identifier shape checks also precede the cursor shape check.
- `check_cursor`, the pure ring lookup called by `check_read` after verification,
  treats every nonempty malformed, evicted or unknown value
  (including one containing `|`) as **410 `cursor_gone`** and never replays the
  ring for such a value.

Recovery from lost history requires an explicit client decision to restart
with an empty cursor.

READ requires `X-Fleet-From`, `X-Fleet-Ts`, `X-Fleet-Nonce`, `X-Fleet-Sig`, and
optionally `X-Fleet-Kid` (header names case-insensitive). Its signature binds the
exact cursor. Apply skew, key, signature, replay and per-reader rate gates
before disclosing records. There is **no open GET**. `check_read` is the
authentication seam; pass the query `limit` only to `check_read`. It returns the
validated `(reader, limit, start)` tuple; pass that result to `read_records(state, accepted)`
under the same serialized access, without intervening ring changes.
After signature and replay verification, `check_read` validates
the limit and retained cursor before capacity/rate checks and nonce recording.
Thus a bad signature wins over a stale cursor (401 before 410). Every refused
POST or READ leaves nonce, message-ID dedupe and rate state unchanged, including
expired entries; cleanup occurs only on acceptance. `read_records` only shapes data and must not be exposed
directly by a transport.
It accepts no raw limit or cursor and performs no validation that can refuse
after recording acceptance. The READ signature covers the cursor but **not the
page limit**; changing limit within its valid range does not invalidate a signature.

Page limit defaults to 100, accepts integers or ASCII decimal query strings,
and must be 1..200; booleans are invalid. Strings are restricted to one through
three digits (leading zeros allowed).
Longer leading-zero forms are rejected to keep the query grammar bounded and identical across implementations.
Response is exactly
`{"cursor": "...", "records": [...]}`. The cursor is the last delivered ID,
or the input cursor when no rows are delivered. Records contain `id`, server
`ts`, `from`, `to`, `msg_id`, scrubbed `text` and any supplied `trace_id`, `key`,
`run_id`, `attempt_n`; never nonce, key identifier or signature.

## Errors

Errors serialize as `{"error":{"code":"...","message":"..."}}`; `msg_rate_limited` (429)
and `replay_window_full` (503) add `"retry_after_s": <int>` inside the error object.
Messages must not echo request payloads, signatures or secret material.
Adapters also publish `retry_after_s` as a Retry-After header.

| Code | HTTP status |
| --- | --- |
| `bad_request` | 400 |
| `skew` | 401 |
| `unknown_kid` | 401 |
| `bad_signature` | 401 |
| `replayed_nonce` | 401 |
| `not_found` | 404 |
| `cursor_gone` | 410 |
| `too_large` | 413 |
| `msg_rate_limited` | 429 |
| `replay_window_full` | 503 |

## keeper message envelope

The keeper messages write API accepts an optional signing and idempotency envelope on `POST /v1/post_message`:

- POST `post_message` takes an optional `envelope` = `{host, sig, nonce, ts, msg_id}`.
- A signed envelope requires `body == {"text": <str>}` and verifies HMAC against that string. Any other body shape with an envelope is refused as validation error (`validation`).
- `sig`, `nonce`, and `ts` follow the EXISTING fleet-bus v2 contract and are checked through `fleet_bus.discovery.Registry.validate_post(host_name, body, signature, now, state)` (per-host key file; REUSE it, no second HMAC implementation).
- `msg_id` is client-generated (uuid4 hex), the idempotency key.
- A signed envelope is verified: bad sig, unknown host, or stale ts -> refused with the contract error code (`bad_signature`, `unknown_kid`, `skew`).
- A nonce table (`message_nonces`, SQLite + PG, append-only, like the messages table triggers) refuses a replay (`replayed_nonce`).
- A `msg_id` unique index (`idx_message_envelopes_msg_id` on `message_envelopes`): a retry with the same `msg_id` AND same content returns the existing row (idempotent receipt); the same `msg_id` with different content -> refused (`conflict`).
- `from` must equal the Bearer principal or the signed host (spoof refused).
- Unsigned posts by an authenticated principal still work (back-compat; existing tests stay green).
- When using the PostgreSQL keeper backend, `PG_DSN` must connect directly to PostgreSQL, never through a transaction pooler (such as PgBouncer in transaction pooling mode), because long-poll waiters hold dedicated listener connections using session-level `LISTEN` / `UNLISTEN` semantics.

