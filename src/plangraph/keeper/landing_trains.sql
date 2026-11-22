CREATE TABLE IF NOT EXISTS landing_trains (
    repo       TEXT NOT NULL,
    pr         BIGINT NOT NULL CHECK (pr > 0),
    base       TEXT NOT NULL,
    head       TEXT NOT NULL,
    manifest   TEXT NOT NULL,
    merged     TEXT,
    cursor     INTEGER NOT NULL DEFAULT 0 CHECK (cursor BETWEEN 0 AND 4),
    abandoned  INTEGER NOT NULL DEFAULT 0 CHECK (abandoned IN (0, 1)),
    PRIMARY KEY (repo, pr)
);
