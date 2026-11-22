CREATE TABLE IF NOT EXISTS landing_claim_bases (
    key        TEXT PRIMARY KEY REFERENCES items(key),
    epoch      BIGINT NOT NULL,
    repo       TEXT NOT NULL,
    pr         BIGINT NOT NULL,
    head       TEXT NOT NULL,
    witnesses  TEXT NOT NULL,
    FOREIGN KEY (repo, pr) REFERENCES landing_trains(repo, pr)
);
CREATE INDEX IF NOT EXISTS idx_landing_claim_bases_repo ON landing_claim_bases(repo);
