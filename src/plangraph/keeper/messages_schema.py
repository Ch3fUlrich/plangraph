"""Append-only durable bus schema."""
TABLE = '''
CREATE TABLE IF NOT EXISTS messages (
    seq BIGINT PRIMARY KEY,
    "from" TEXT NOT NULL,
    "to" TEXT NOT NULL,
    body TEXT NOT NULL,
    trace_id TEXT NOT NULL,
    key TEXT NOT NULL,
    run_id TEXT NOT NULL,
    attempt_n BIGINT NOT NULL CHECK (attempt_n >= 0),
    ack_state TEXT NOT NULL CHECK (ack_state IN ('pending','delivered','failed')),
    ts BIGINT NOT NULL,
    prev_hash TEXT NOT NULL,
    hash TEXT NOT NULL,
    posted_by TEXT
);
CREATE INDEX IF NOT EXISTS idx_messages_trace_id ON messages(trace_id);
CREATE INDEX IF NOT EXISTS idx_messages_key_ts ON messages(key,ts);
CREATE INDEX IF NOT EXISTS idx_messages_ts ON messages(ts);
CREATE TABLE IF NOT EXISTS message_nonces (
    nonce TEXT PRIMARY KEY,
    host TEXT NOT NULL,
    ts BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS message_envelopes (
    msg_id TEXT PRIMARY KEY,
    seq BIGINT NOT NULL,
    body_hash TEXT NOT NULL,
    host TEXT NOT NULL,
    ts BIGINT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_message_envelopes_msg_id ON message_envelopes(msg_id);
'''
SQLITE = TABLE + '''
CREATE TRIGGER IF NOT EXISTS trg_messages_no_update BEFORE UPDATE ON messages
BEGIN SELECT RAISE(ABORT, 'messages are append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_messages_no_delete BEFORE DELETE ON messages
BEGIN SELECT RAISE(ABORT, 'messages are append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_message_nonces_no_update BEFORE UPDATE ON message_nonces
BEGIN SELECT RAISE(ABORT, 'message_nonces are append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_message_nonces_no_delete BEFORE DELETE ON message_nonces
BEGIN SELECT RAISE(ABORT, 'message_nonces are append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_message_envelopes_no_update BEFORE UPDATE ON message_envelopes
BEGIN SELECT RAISE(ABORT, 'message_envelopes are append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_message_envelopes_no_delete BEFORE DELETE ON message_envelopes
BEGIN SELECT RAISE(ABORT, 'message_envelopes are append-only'); END;
'''
PG = '''
CREATE TABLE messages (
    seq BIGINT PRIMARY KEY,
    "from" TEXT COLLATE "C" NOT NULL,
    "to" TEXT COLLATE "C" NOT NULL,
    body TEXT COLLATE "C" NOT NULL,
    trace_id TEXT COLLATE "C" NOT NULL,
    key TEXT COLLATE "C" NOT NULL,
    run_id TEXT COLLATE "C" NOT NULL,
    attempt_n BIGINT NOT NULL CHECK (attempt_n >= 0),
    ack_state TEXT COLLATE "C" NOT NULL CHECK (ack_state IN ('pending','delivered','failed')),
    ts BIGINT NOT NULL,
    prev_hash TEXT COLLATE "C" NOT NULL,
    hash TEXT COLLATE "C" NOT NULL,
    posted_by TEXT COLLATE "C"
);
CREATE INDEX idx_messages_trace_id ON messages(trace_id);
CREATE INDEX idx_messages_key_ts ON messages(key,ts);
CREATE INDEX idx_messages_ts ON messages(ts);
CREATE TABLE message_nonces (
    nonce TEXT COLLATE "C" PRIMARY KEY,
    host TEXT COLLATE "C" NOT NULL,
    ts BIGINT NOT NULL
);
CREATE TABLE message_envelopes (
    msg_id TEXT COLLATE "C" PRIMARY KEY,
    seq BIGINT NOT NULL,
    body_hash TEXT COLLATE "C" NOT NULL,
    host TEXT COLLATE "C" NOT NULL,
    ts BIGINT NOT NULL
);
CREATE UNIQUE INDEX idx_message_envelopes_msg_id ON message_envelopes(msg_id);
CREATE FUNCTION messages_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'messages are append-only' USING ERRCODE = '23000'; END $$;
CREATE TRIGGER trg_messages_no_update BEFORE UPDATE ON messages
FOR EACH STATEMENT EXECUTE FUNCTION messages_immutable();
CREATE TRIGGER trg_messages_no_delete BEFORE DELETE OR TRUNCATE ON messages
FOR EACH STATEMENT EXECUTE FUNCTION messages_immutable();
CREATE FUNCTION messages_hint() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN PERFORM pg_notify('keeper_messages', NEW.seq::text); RETURN NEW; END $$;
CREATE TRIGGER trg_messages_notify AFTER INSERT ON messages
FOR EACH ROW EXECUTE FUNCTION messages_hint();
CREATE FUNCTION message_nonces_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'message_nonces are append-only' USING ERRCODE = '23000'; END $$;
CREATE TRIGGER trg_message_nonces_no_update BEFORE UPDATE ON message_nonces
FOR EACH STATEMENT EXECUTE FUNCTION message_nonces_immutable();
CREATE TRIGGER trg_message_nonces_no_delete BEFORE DELETE OR TRUNCATE ON message_nonces
FOR EACH STATEMENT EXECUTE FUNCTION message_nonces_immutable();
CREATE FUNCTION message_envelopes_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'message_envelopes are append-only' USING ERRCODE = '23000'; END $$;
CREATE TRIGGER trg_message_envelopes_no_update BEFORE UPDATE ON message_envelopes
FOR EACH STATEMENT EXECUTE FUNCTION message_envelopes_immutable();
CREATE TRIGGER trg_message_envelopes_no_delete BEFORE DELETE OR TRUNCATE ON message_envelopes
FOR EACH STATEMENT EXECUTE FUNCTION message_envelopes_immutable();
'''
