# Fleet bus wire contract

VERSION: `fleet-bus/2`. This specification is transport- and language-neutral.
The executable conformance vectors live in `tests/fleet_bus/vectors/`.
All signing strings are UTF-8, without a trailing newline.

## Envelope and caps

POST is a UTF-8 JSON object. Required fields are `from`, `to`, `ts`, `nonce`,
`msg_id`, `text`. Optional fields are `trace_id`, `key`, `run_id`, `attempt_n`,
`kid`; unknown fields are rejected.

Identifiers are nonempty strings, capped at 64 Unicode code points except
`nonce` (128). They contain no whitespace controls, spaces, pipe delimiters or
surrogates. `attempt_n` is an integer from 1 through 1000, never a boolean.
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
7. Rate: 30 authenticated requests per rolling 60 seconds per sender.
8. Dedupe by `(from, msg_id)`.

Malformed JSON has no shape: if oversized it returns `too_large`, otherwise
`bad_request`. Valid JSON is shape-checked even when oversized.
Failed authentication never consumes a nonce or rate allowance. Authenticated
nonces are globally unique across POST and READ and stored in insertion order,
evicting the oldest above 10000 entries. An authenticated rate rejection consumes
its nonce; clients retry with a fresh nonce. The limiter removes events at or
before `now - 60`; rejected requests do not add a rate event. Its retry delay is
at least one second, rounded up to the first available slot.

Duplicate messages still pass replay and rate gates, then return the original
`id`, `ts`, `msg_id` with `dup: true`, without appending or replacing text.
New acknowledgements contain exactly `id`, `ts`, `msg_id`, `dup: false`.
Acknowledgement `ts` is server acceptance time in seconds. Retain 1000 records;
dedupe acknowledgements are retired with their records. Replay protection is
bounded by the nonce cap, not a durable exactly-once guarantee.

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
  a string or that contains `|` with **400 `bad_request`**, once a signature
  header is present and before authentication. Such a cursor cannot be bound
  unambiguously into the signed canonical form. Real record IDs never contain
  `|`. A missing or empty signature header returns `bad_signature` first;
  timestamp and identifier shape checks also precede the cursor shape check.
- `check_cursor`, the pure ring lookup called by `read_records` after
  authentication, treats every nonempty malformed, evicted or unknown value
  (including one containing `|`) as **410 `cursor_gone`** and never replays the
  ring for such a value.

Recovery from lost history requires an explicit client decision to restart
with an empty cursor.

READ requires `X-Fleet-From`, `X-Fleet-Ts`, `X-Fleet-Nonce`, `X-Fleet-Sig`, and
optionally `X-Fleet-Kid` (header names case-insensitive). Its signature binds the
exact cursor. Apply skew, key, signature, replay and per-reader rate gates
before disclosing records. There is **no open GET**. `check_read` is the
authentication seam; `read_records` only shapes data and must not be exposed
directly by a transport.

Page limit defaults to 100, accepts integers or ASCII decimal query strings,
and must be 1..200; booleans are invalid. Response is exactly
`{"cursor": "...", "records": [...]}`. The cursor is the last delivered ID,
or the input cursor when no rows are delivered. Records contain `id`, server
`ts`, `from`, `to`, `msg_id`, scrubbed `text` and any supplied `trace_id`, `key`,
`run_id`, `attempt_n`; never nonce, key identifier or signature.

## Errors

Errors serialize as `{"error":{"code":"...","message":"..."}}`.
Messages must not echo request payloads, signatures or secret material.
Rate errors additionally carry an internal `retry_after_s` for the adapter to
publish as a Retry-After header; it is not an extra JSON response field.

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
| `rate_limited` | 429 |
