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
    hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_trace_id ON messages(trace_id);
CREATE INDEX IF NOT EXISTS idx_messages_key_ts ON messages(key,ts);
CREATE INDEX IF NOT EXISTS idx_messages_ts ON messages(ts);
'''
SQLITE = TABLE + '''
CREATE TRIGGER IF NOT EXISTS trg_messages_no_update BEFORE UPDATE ON messages
BEGIN SELECT RAISE(ABORT, 'messages are append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_messages_no_delete BEFORE DELETE ON messages
BEGIN SELECT RAISE(ABORT, 'messages are append-only'); END;
'''
PG = TABLE + '''
CREATE FUNCTION messages_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'messages are append-only' USING ERRCODE = '23000'; END $$;
CREATE TRIGGER trg_messages_no_update BEFORE UPDATE OR DELETE OR TRUNCATE ON messages
FOR EACH STATEMENT EXECUTE FUNCTION messages_immutable();
CREATE FUNCTION messages_hint() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN PERFORM pg_notify('keeper_messages', NEW.seq::text); RETURN NEW; END $$;
CREATE TRIGGER trg_messages_notify AFTER INSERT ON messages
FOR EACH ROW EXECUTE FUNCTION messages_hint();
'''
