pg-keeper-backup verifies a dump against the table count it reads from the live keeper catalog together with max(seq), not a fixed 43; `--tables N` stays an override.
