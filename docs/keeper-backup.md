# Keeper schema backup (pg-keeper-backup)

A user timer dumps the keeper schema once a day, keeps the newest 14 dumps and proves the newest one restores.
Install and run on the keeper host only; nothing here touches the live keeper beyond read-only `SELECT max(seq)` and `pg_dump`.

## What a run does

`pg-keeper-backup dump --verify --dir ~/.local/share/plangraph/backups`:

1. reads `max(seq)` of `keeper.events`;
2. runs `pg_dump -Fc -n keeper` in `docker run postgres:17`, streaming it into `.keeper-schema-seq<SEQ>-<YYYYMMDDTHHMMZ>.dump.part` (created `0600`, never world-readable) and fsyncs it;
3. with `--verify`, runs the restore test on that `.part` file **before it has any retention-eligible name**;
4. appends one line `<stamp> seq=<n> size=<bytes> sha256=<hex> verified=<yes|no|skipped> file=<name>` to `keeper-schema-ledger.txt` (mode `0600`; the ledger keeps its history when a dump is pruned). The line comes **before** the archive gets its name, so an interrupted run never leaves an unrecorded `.dump` that could count toward retention (a line without a file is harmless);
5. publishes the `.part` file by hard link, which refuses to overwrite: as `keeper-schema-seq<SEQ>-<stamp>.dump` when it passed (or verification was not asked), as `<name>.dump.unverified` when it failed. A failed run exits non-zero and prunes **no verified dump**; only the newest 3 `.unverified` files are kept, for diagnosis, and they never count as backups;
6. keeps the newest `--keep` (default 14) verified dump files and deletes the rest; only names that fully match the dump pattern are ever deleted.

A failed or empty dump leaves no file and no ledger line.

## Restore test

`pg-keeper-backup restore-test --file DUMP` refuses, before any Postgres call, a dump whose name carries a seq below 19096 (D-5796: no restore, import or migration loads older keeper state), then lists it (`pg_restore -l`, must show 43 tables, `--tables N` to change), loads it into a scratch database `pgkb_scratch_<seq>_<pid>` on the same server, checks 43 tables and that the restored `max(seq)` is not below the seq of the name (a renamed old dump fails here), and drops the scratch database it created. `CREATE DATABASE` fails when the name is taken, and then nothing is dropped, so a database this run did not create is never touched.
The scratch is a database, not a schema in the live database: `pg_restore` cannot rename the dumped schema, and loading `keeper` into the live database would collide with the live schema. The connecting role needs `CREATEDB`.

## Secrets

The connection comes from `PGHOST PGPORT PGUSER PGPASSWORD PGDATABASE PGSSLMODE` in the process environment, handed to the container by name (`-e PGPASSWORD`), so no value is on a command line. The container runs with `--log-driver none` (the docker daemon keeps no copy of its output), and its stderr is redacted for the `PGPASSWORD` value before it is printed. The values live in `~/.config/plangraph/keeper-backup.env` (mode `0600`, template `deploy/systemd/keeper-backup.env.example`), read by `EnvironmentFile=`.

## Install (keeper host, after the merge)

```sh
(cd rust && cargo build --release -p pg-keeper-backup) && install rust/target/release/pg-keeper-backup ~/.local/bin/
install -m 0600 deploy/systemd/keeper-backup.env.example ~/.config/plangraph/keeper-backup.env   # then fill in the values
cp deploy/systemd/pg-keeper-backup.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now pg-keeper-backup.timer
systemctl --user start pg-keeper-backup.service && journalctl --user -u pg-keeper-backup.service -n 5
```

First real run (the only test against a real server; hermetic tests cannot run SQL): `pg-keeper-backup dump --verify --dir "$(mktemp -d)"` on the keeper host with the env file loaded, expecting `verified=yes` and `restore ok: seq <n> tables 43`; it reads the live schema and creates and drops one scratch database, nothing else.

Test: `cargo test -p pg-keeper-backup`. Rules run against a fake `Pg`; `tests/cli.rs` runs the real command line against a fake `docker` executable that checks the commands, the `0600` modes and that a sentinel password never reaches output or logs; `tests/units.rs` runs `systemd-analyze verify` when it is installed. No test needs a Postgres server.
