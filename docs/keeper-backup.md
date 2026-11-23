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

`pg-keeper-backup restore-test --file DUMP` refuses a name with a seq below 19096 before any Postgres call (D-5796). It lists the archive (`pg_restore -l`, 43 tables by default, or `--tables N`) and reads the archived event sequence as data. Archives with a sequence below the name's seq are refused before anything is loaded. The tool then restores into a scratch database `pgkb_scratch_<seq>_<pid>` on the same server, checks the table count and that the restored sequence matches the archive, then drops the scratch database it created. If the scratch name is taken, `CREATE DATABASE` fails and the existing database is left alone.
The scratch is a database, not a schema in the live database: `pg_restore` cannot rename the dumped schema, and loading `keeper` into the live database would collide with the live schema. The connecting role needs `CREATEDB`.

## Secrets

The values live in `~/.config/plangraph/keeper-backup.env` (mode `0600`, template `deploy/systemd/keeper-backup.env.example`), read by `EnvironmentFile=`. The connection comes from the standard libpq variables of the process environment (the list is `PG_ENV` in `docker.rs`), handed to the container by name only (`-e NAME`, no `=value`), so no value is on a command line. The container runs with `--log-driver none` (the docker daemon keeps no copy of its output), and its stderr is redacted for the password value before it is printed.

## Install (keeper host, after the merge)

Step 1, install the files (nothing runs yet):

```sh
(cd rust && cargo build --release -p pg-keeper-backup) && install rust/target/release/pg-keeper-backup ~/.local/bin/
install -m 0600 deploy/systemd/keeper-backup.env.example ~/.config/plangraph/keeper-backup.env   # then fill in the values
cp deploy/systemd/pg-keeper-backup.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
```

Step 2, the first real run. It is the only test against a real server (hermetic tests cannot run SQL) and it comes BEFORE the timer is enabled or the service started. Load the env file and run `pg-keeper-backup dump --verify --dir "$(mktemp -d)"`. Expect exit status 0 and a ledger line with `verified=yes` (`pg-keeper-backup restore-test --file DUMP` alone prints `restore ok: seq <n> tables 43`). It reads the live schema and creates and drops one scratch database, nothing else. Do not go on to step 3 unless it passed.

Step 3, only after step 2 passed:

```sh
systemctl --user enable --now pg-keeper-backup.timer
systemctl --user start pg-keeper-backup.service && journalctl --user -u pg-keeper-backup.service -n 5
```

Test: `cargo test -p pg-keeper-backup`. Rules run against a fake `Pg`; `tests/cli.rs` runs the real command line against a fake `docker` executable that checks the commands, the `0600` modes and that a sentinel password never reaches output or logs; `tests/units.rs` runs `systemd-analyze verify` when it is installed. No test needs a Postgres server.

The systemd unit verification fixture uses the shared atomic writer for its
stand-in backup executable.
