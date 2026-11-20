# Keeper schema backup (pg-keeper-backup)

A user timer dumps the keeper schema once a day, keeps the newest 14 dumps and proves the newest one restores.
Install and run on the keeper host only; nothing here touches the live keeper beyond read-only `SELECT max(seq)` and `pg_dump`.

## What a run does

`pg-keeper-backup dump --verify --dir ~/.local/share/plangraph/backups`:

1. reads `max(seq)` of `keeper.events`;
2. runs `pg_dump -Fc -n keeper` in `docker run postgres:17`, streams it into `.keeper-schema-seq<SEQ>-<YYYYMMDDTHHMMZ>.dump.part` (created `0600`, never world-readable), fsyncs and renames it to `keeper-schema-seq<SEQ>-<stamp>.dump`;
3. appends one line `<stamp> seq=<n> size=<bytes> sha256=<hex> file=<name>` to `keeper-schema-ledger.txt` (mode `0600`; the ledger keeps its history when a dump is pruned);
4. keeps the newest `--keep` (default 14) dump files and deletes the rest; only names that fully match the dump pattern are ever deleted;
5. with `--verify`, runs the restore test on the new dump.

A failed or empty dump leaves no file and no ledger line.

## Restore test

`pg-keeper-backup restore-test --file DUMP` refuses, before any Postgres call, a dump whose name carries a seq below 19096 (D-5796: no restore, import or migration loads older keeper state), then lists it (`pg_restore -l`, must show 43 tables, `--tables N` to change), loads it into a scratch database `pgkb_scratch_<seq>_<pid>` on the same server, checks 43 tables and that the restored `max(seq)` is not below the seq of the name (a renamed old dump fails here), and always drops the scratch database.
The scratch is a database, not a schema in the live database: `pg_restore` cannot rename the dumped schema, and loading `keeper` into the live database would collide with the live schema. The connecting role needs `CREATEDB`.

## Secrets

The connection comes from `PGHOST PGPORT PGUSER PGPASSWORD PGDATABASE PGSSLMODE` in the process environment, handed to the container by name (`-e PGPASSWORD`), so no value is on a command line. Docker stderr is redacted for the `PGPASSWORD` value before it is printed. The values live in `~/.config/plangraph/keeper-backup.env` (mode `0600`, template `deploy/systemd/keeper-backup.env.example`), read by `EnvironmentFile=`.

## Install (keeper host, after the merge)

```sh
cd rust && cargo build --release -p pg-keeper-backup && install target/release/pg-keeper-backup ~/.local/bin/
install -m 0600 deploy/systemd/keeper-backup.env.example ~/.config/plangraph/keeper-backup.env   # then fill in the values
cp deploy/systemd/pg-keeper-backup.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now pg-keeper-backup.timer
systemctl --user start pg-keeper-backup.service && journalctl --user -u pg-keeper-backup.service -n 5
```

Test: `cargo test -p pg-keeper-backup` (fake Postgres; the unit test runs `systemd-analyze verify` when it is installed).
