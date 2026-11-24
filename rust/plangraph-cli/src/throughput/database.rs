use super::{runs::Inputs, Arrival, Hour, HOUR};
use rusqlite::{params, types::ValueRef, Connection, OpenFlags, TransactionBehavior};
use serde_json::{json, Map, Value};
use std::{collections::BTreeMap, path::PathBuf, time::Duration};

pub(super) struct Db(pub PathBuf);

const SCHEMA: &str = "
CREATE TABLE IF NOT EXISTS throughput_hourly (
    repo TEXT NOT NULL, bucket INTEGER NOT NULL, through INTEGER NOT NULL,
    commits INTEGER NOT NULL, items_closed INTEGER NOT NULL, PRIMARY KEY(repo,bucket)
);
CREATE TABLE IF NOT EXISTS throughput_daily (
    repo TEXT NOT NULL, day TEXT NOT NULL, observed_seconds INTEGER NOT NULL,
    commits INTEGER NOT NULL, items_closed INTEGER NOT NULL, PRIMARY KEY(repo,day)
);
CREATE TABLE IF NOT EXISTS throughput_item_landing (
    repo TEXT NOT NULL, item TEXT NOT NULL, landed INTEGER NOT NULL, PRIMARY KEY(repo,item)
);
CREATE TABLE IF NOT EXISTS throughput_writer_hourly (
    repo TEXT NOT NULL, bucket INTEGER NOT NULL, through INTEGER NOT NULL, host TEXT NOT NULL,
    running INTEGER NOT NULL, measured INTEGER NOT NULL, active INTEGER NOT NULL,
    PRIMARY KEY(repo,bucket,host)
);
CREATE TABLE IF NOT EXISTS throughput_quota_window (
    repo TEXT NOT NULL, bucket INTEGER NOT NULL, through INTEGER NOT NULL, provider TEXT NOT NULL,
    account TEXT NOT NULL, window TEXT NOT NULL, reset INTEGER, remaining REAL,
    PRIMARY KEY(repo,bucket,provider,account,window)
);
CREATE TABLE IF NOT EXISTS throughput_accept_part (
    repo TEXT NOT NULL, head TEXT NOT NULL, sha TEXT NOT NULL, landed INTEGER NOT NULL,
    seconds INTEGER NOT NULL, model TEXT, tokens INTEGER, commits INTEGER NOT NULL,
    legacy INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(repo,head)
);";

fn database_error(error: rusqlite::Error) -> String {
    format!("landing database: {error}")
}

fn query(connection: &Connection, sql: &str, repo: &str) -> Result<Vec<Value>, String> {
    let mut statement = connection.prepare(sql).map_err(database_error)?;
    let names: Vec<_> = statement
        .column_names()
        .into_iter()
        .map(str::to_owned)
        .collect();
    let rows = statement
        .query_map([repo], |row| {
            let mut object = Map::new();
            for (index, name) in names.iter().enumerate() {
                let value = match row.get_ref(index)? {
                    ValueRef::Null => Value::Null,
                    ValueRef::Integer(value) => json!(value),
                    ValueRef::Real(value) => json!(value),
                    ValueRef::Text(_) => json!(row.get::<_, String>(index)?),
                    ValueRef::Blob(_) => {
                        return Err(rusqlite::Error::InvalidColumnType(
                            index,
                            name.clone(),
                            rusqlite::types::Type::Blob,
                        ))
                    }
                };
                object.insert(name.clone(), value);
            }
            Ok(Value::Object(object))
        })
        .map_err(database_error)?;
    rows.collect::<rusqlite::Result<_>>()
        .map_err(database_error)
}

fn table_exists(connection: &Connection, name: &str) -> rusqlite::Result<bool> {
    connection.query_row(
        "SELECT EXISTS(SELECT 1 FROM sqlite_schema WHERE type='table' AND name=?1)",
        [name],
        |row| row.get(0),
    )
}

/// The table a report reads acceptances from: the part table, else the first collector's table read as parts
/// (so a database not yet collected since the upgrade still reports its history), else nothing.
fn accept_source(connection: &Connection) -> rusqlite::Result<&'static str> {
    Ok(if table_exists(connection, "throughput_accept_part")? {
        "throughput_accept_part"
    } else if table_exists(connection, "throughput_accept")? {
        "(SELECT repo,sha,sha AS head,landed,seconds,model,tokens,commits FROM throughput_accept)"
    } else {
        "(SELECT NULL AS repo,NULL AS sha,NULL AS head,0 AS landed,0 AS seconds,NULL AS model,NULL AS tokens,0 AS commits WHERE 0)"
    })
}

/// As `accept_source`, for the quota samples.
fn quota_source(connection: &Connection) -> rusqlite::Result<&'static str> {
    Ok(if table_exists(connection, "throughput_quota_window")? {
        "throughput_quota_window"
    } else if table_exists(connection, "throughput_quota_hourly")? {
        "(SELECT repo,bucket,through,provider,'' AS account,'' AS window,NULL AS reset,remaining FROM throughput_quota_hourly)"
    } else {
        "(SELECT NULL AS repo,0 AS bucket,0 AS through,NULL AS provider,NULL AS account,NULL AS window,NULL AS reset,NULL AS remaining WHERE 0)"
    })
}

fn check_landing_schema(connection: &Connection) -> Result<(), String> {
    let mut statement = connection
        .prepare("SELECT name FROM pragma_table_info('landing') WHERE pk > 0 ORDER BY pk")
        .map_err(database_error)?;
    let rows = statement
        .query_map([], |row| row.get::<_, String>(0))
        .map_err(database_error)?;
    let keys = rows
        .collect::<rusqlite::Result<Vec<_>>>()
        .map_err(database_error)?;
    if keys != ["repo", "landed_sha"] {
        return Err("landing database must use the lander's (repo, landed_sha) schema".into());
    }
    Ok(())
}

impl Db {
    fn open(&self, flags: OpenFlags) -> Result<Connection, String> {
        if !self.0.is_file() {
            return Err("landing database unavailable; initialize it through the lander".into());
        }
        let connection = Connection::open_with_flags(&self.0, flags).map_err(database_error)?;
        connection
            .busy_timeout(Duration::from_secs(30))
            .map_err(database_error)?;
        Ok(connection)
    }

    pub fn check_landing(&self) -> Result<(), String> {
        check_landing_schema(&self.open(OpenFlags::SQLITE_OPEN_READ_ONLY)?)
    }

    pub fn through(&self, repo: &str) -> Result<Option<i64>, String> {
        let connection = self.open(OpenFlags::SQLITE_OPEN_READ_ONLY)?;
        let exists = connection.query_row(
            "SELECT EXISTS(SELECT 1 FROM sqlite_schema WHERE type='table' AND name='throughput_hourly')",
            [], |row| row.get::<_, bool>(0),
        ).map_err(database_error)?;
        if !exists {
            return Ok(None);
        }
        connection
            .query_row(
                "SELECT MAX(through) FROM throughput_hourly WHERE repo=?1",
                [repo],
                |row| row.get(0),
            )
            .map_err(database_error)
    }

    /// The landings the lander recorded for `repo`, oldest first: landed SHA and landing time.
    pub fn landings(&self, repo: &str) -> Result<Vec<(String, i64)>, String> {
        let connection = self.open(OpenFlags::SQLITE_OPEN_READ_ONLY)?;
        let mut statement = connection
            .prepare(
                "SELECT landed_sha, unixepoch(landed_at) AS at FROM landing
                 WHERE repo=?1 AND unixepoch(landed_at) IS NOT NULL ORDER BY at, landed_sha",
            )
            .map_err(database_error)?;
        let rows = statement
            .query_map([repo], |row| Ok((row.get(0)?, row.get(1)?)))
            .map_err(database_error)?;
        rows.collect::<rusqlite::Result<_>>()
            .map_err(database_error)
    }

    pub fn store(
        &self,
        repo: &str,
        end: i64,
        hours: &BTreeMap<i64, Hour>,
        arrivals: &[Arrival],
        trains: &[Arrival],
        inputs: &Inputs,
    ) -> Result<(), String> {
        let first = hours.first_key_value().ok_or("no throughput hours")?.0;
        let mut connection = self.open(OpenFlags::SQLITE_OPEN_READ_WRITE)?;
        let transaction = connection
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(database_error)?;
        check_landing_schema(&transaction)?;
        transaction.execute_batch(SCHEMA).map_err(database_error)?;
        Self::migrate_legacy(&transaction).map_err(database_error)?;
        Self::write_hours(&transaction, repo, end, hours, inputs).map_err(database_error)?;
        Self::write_arrivals(&transaction, repo, arrivals, trains, inputs)
            .map_err(database_error)?;
        transaction
            .execute(
                "INSERT INTO throughput_daily
            SELECT repo,date(bucket,'unixepoch'),SUM(through-bucket),SUM(commits),SUM(items_closed)
            FROM throughput_hourly WHERE repo=?1
            AND bucket BETWEEN (?2/86400)*86400 AND ((?3/86400)+1)*86400-1
            GROUP BY repo,date(bucket,'unixepoch')
            ON CONFLICT(repo,day) DO UPDATE SET observed_seconds=excluded.observed_seconds,
            commits=excluded.commits,items_closed=excluded.items_closed",
                params![repo, first, end - 1],
            )
            .map_err(database_error)?;
        transaction.commit().map_err(database_error)
    }

    /// Carry the samples of the first collector (one acceptance per landing, one percentage per provider) into
    /// the part and window tables; rows already there win, and the old tables are left as they are.
    fn migrate_legacy(connection: &Connection) -> rusqlite::Result<()> {
        if table_exists(connection, "throughput_accept")? {
            connection.execute_batch(
                "INSERT OR IGNORE INTO throughput_accept_part(repo,head,sha,landed,seconds,model,tokens,commits,legacy)
                 SELECT repo,sha,sha,landed,seconds,model,tokens,commits,1 FROM throughput_accept;",
            )?;
        }
        if table_exists(connection, "throughput_quota_hourly")? {
            connection.execute_batch(
                "INSERT OR IGNORE INTO throughput_quota_window(repo,bucket,through,provider,account,window,reset,remaining)
                 SELECT repo,bucket,through,provider,'','',NULL,remaining FROM throughput_quota_hourly;",
            )?;
        }
        Ok(())
    }

    fn write_hours(
        connection: &Connection,
        repo: &str,
        end: i64,
        hours: &BTreeMap<i64, Hour>,
        inputs: &Inputs,
    ) -> rusqlite::Result<()> {
        let mut hour = connection.prepare("INSERT INTO throughput_hourly VALUES(?1,?2,?3,?4,?5)
            ON CONFLICT(repo,bucket) DO UPDATE SET through=excluded.through, commits=excluded.commits,
            items_closed=excluded.items_closed WHERE excluded.through >= throughput_hourly.through")?;
        let mut writer = connection.prepare("INSERT INTO throughput_writer_hourly VALUES(?1,?2,?3,?4,?5,?6,?7)
            ON CONFLICT(repo,bucket,host) DO UPDATE SET through=excluded.through,running=excluded.running,
            measured=excluded.measured,active=excluded.active WHERE excluded.through >= throughput_writer_hourly.through")?;
        let mut quota = connection.prepare(
            "INSERT INTO throughput_quota_window VALUES(?1,?2,?3,?4,?5,?6,?7,?8)
            ON CONFLICT(repo,bucket,provider,account,window) DO UPDATE SET through=excluded.through,
            reset=excluded.reset,remaining=excluded.remaining
            WHERE excluded.through >= throughput_quota_window.through",
        )?;
        for (&bucket, value) in hours {
            let through = end.min(bucket + HOUR);
            hour.execute(params![
                repo,
                bucket,
                through,
                value.commits,
                value.items_closed
            ])?;
            for (host, time) in inputs.writer_time(bucket, through) {
                writer.execute(params![
                    repo,
                    bucket,
                    through,
                    host,
                    time.running,
                    time.measured,
                    time.active
                ])?;
            }
            for row in inputs.quota(through - 1) {
                quota.execute(params![
                    repo,
                    bucket,
                    through,
                    row.provider,
                    row.account,
                    row.window,
                    row.reset,
                    row.remaining
                ])?;
            }
        }
        Ok(())
    }

    fn write_arrivals(
        connection: &Connection,
        repo: &str,
        arrivals: &[Arrival],
        trains: &[Arrival],
        inputs: &Inputs,
    ) -> rusqlite::Result<()> {
        let mut landing = connection.prepare("INSERT INTO throughput_item_landing VALUES(?1,?2,?3)
            ON CONFLICT(repo,item) DO UPDATE SET landed=MIN(throughput_item_landing.landed,excluded.landed)")?;
        // A commit that arrives on its own never replaces the row of the train that carried its part.
        let mut arrival_row = connection.prepare(
            "INSERT INTO throughput_accept_part(repo,head,sha,landed,seconds,model,tokens,commits)
            VALUES(?1,?2,?3,?4,?5,?6,?7,?8)
            ON CONFLICT(repo,head) DO UPDATE SET sha=excluded.sha,landed=excluded.landed,
            seconds=excluded.seconds,model=excluded.model,tokens=excluded.tokens,commits=excluded.commits,legacy=0
            WHERE throughput_accept_part.legacy=1 OR throughput_accept_part.sha=excluded.sha",
        )?;
        let mut train_row = connection.prepare(
            "INSERT INTO throughput_accept_part(repo,head,sha,landed,seconds,model,tokens,commits)
            VALUES(?1,?2,?3,?4,?5,?6,?7,?8)
            ON CONFLICT(repo,head) DO UPDATE SET sha=excluded.sha,landed=excluded.landed,
            seconds=excluded.seconds,model=excluded.model,tokens=excluded.tokens,commits=excluded.commits,legacy=0",
        )?;
        // Verified part rows replace the legacy sample of the same landing: it counted that landing once.
        let mut retire = connection
            .prepare("DELETE FROM throughput_accept_part WHERE repo=?1 AND sha=?2 AND legacy=1")?;
        for arrival in arrivals {
            for item in &arrival.items {
                landing.execute(params![repo, item, arrival.at])?;
            }
        }
        for (set, row) in [(arrivals, &mut arrival_row), (trains, &mut train_row)] {
            for arrival in set {
                let parts = inputs.accepted_parts(arrival);
                for (head, accepted) in &parts {
                    row.execute(params![
                        repo,
                        head,
                        arrival.sha,
                        arrival.at,
                        accepted.seconds,
                        accepted.model,
                        accepted.tokens,
                        accepted.commits
                    ])?;
                }
                if !parts.is_empty() {
                    for (member, _) in &arrival.members {
                        retire.execute(params![repo, member])?;
                    }
                }
            }
        }
        Ok(())
    }

    pub fn report(&self, repo: &str) -> Result<Value, String> {
        let mut connection = self.open(OpenFlags::SQLITE_OPEN_READ_ONLY)?;
        let transaction = connection.transaction().map_err(database_error)?;
        check_landing_schema(&transaction)?;
        let query = |sql: &str| query(&transaction, sql, repo);
        let accept = accept_source(&transaction).map_err(database_error)?;
        let quota_table = quota_source(&transaction).map_err(database_error)?;
        let days = query(&format!("WITH train AS (
            SELECT date(landed_at) AS day, COUNT(*) AS trains, SUM(json_array_length(parts)) AS train_parts,
            SUM(CASE WHEN unixepoch(landed_at)>=unixepoch(build_started_at)
                THEN unixepoch(landed_at)-unixepoch(build_started_at) END) AS train_seconds
            FROM landing l WHERE repo=?1 AND EXISTS(SELECT 1 FROM throughput_hourly h
                WHERE h.repo=l.repo AND unixepoch(l.landed_at)>=h.bucket AND unixepoch(l.landed_at)<h.through)
            GROUP BY date(landed_at)
        ), accepted AS (
            SELECT date(landed,'unixepoch') AS day, COUNT(*) AS accept_samples, AVG(seconds) AS accept_to_main_seconds
            FROM {accept} WHERE repo=?1 GROUP BY date(landed,'unixepoch')
        ), items AS (
            SELECT date(landed,'unixepoch') AS day, COUNT(*) AS items_landed
            FROM throughput_item_landing WHERE repo=?1 GROUP BY date(landed,'unixepoch')
        ) SELECT d.day,observed_seconds,commits,items_closed,COALESCE(items_landed,0) AS items_landed,COALESCE(trains,0) AS trains,
            COALESCE(train_parts,0) AS train_parts,train_seconds,COALESCE(accept_samples,0) AS accept_samples,accept_to_main_seconds
            FROM throughput_daily d LEFT JOIN train USING(day) LEFT JOIN accepted USING(day) LEFT JOIN items USING(day)
            WHERE repo=?1 ORDER BY d.day"))?;
        let writers = query(
            "SELECT date(bucket,'unixepoch') AS day,host,
            SUM(running) AS writer_running_seconds, SUM(measured) AS writer_measured_seconds,
            1.0*SUM(running)/NULLIF(SUM(through-bucket),0) AS mean_running_writers,
            1.0*SUM(active)/NULLIF(SUM(measured),0) AS writer_active_share
            FROM throughput_writer_hourly WHERE repo=?1 GROUP BY day,host ORDER BY day,host",
        )?;
        let models = query(&format!("SELECT date(landed,'unixepoch') AS day,model,SUM(tokens) AS tokens,
            SUM(commits) AS landed_commits, 1.0*SUM(tokens)/NULLIF(SUM(commits),0) AS tokens_per_landed_commit
            FROM {accept} WHERE repo=?1 AND tokens IS NOT NULL AND model IS NOT NULL
            GROUP BY day,model ORDER BY day,model"))?;
        let quota = query(&format!("SELECT date(bucket,'unixepoch') AS day,provider,account,window,
            MIN(remaining) AS quota_headroom_percent, strftime('%Y-%m-%dT%H:%M:%SZ',MAX(reset),'unixepoch') AS reset_at
            FROM {quota_table} WHERE repo=?1 GROUP BY day,provider,account,window
            ORDER BY day,provider,account,window"))?;
        let weeks = query("WITH latest AS (SELECT MAX(day) AS last FROM throughput_daily WHERE repo=?1),
            periods AS (SELECT d.*,CASE WHEN d.day>date(last,'-7 days') THEN 'current' ELSE 'previous' END AS week
            FROM throughput_daily d CROSS JOIN latest WHERE repo=?1 AND day>date(last,'-14 days'))
            SELECT week,COUNT(*) AS days,SUM(observed_seconds) AS observed_seconds,
            SUM(commits) AS commits,SUM(items_closed) AS items_closed,
            86400.0*SUM(commits)/NULLIF(SUM(observed_seconds),0) AS commits_per_observed_day
            FROM periods GROUP BY week ORDER BY week")?;
        // Columns the lander fills only when it knows them; a database without one reports it unknown.
        let optional = |name: &str| -> Result<String, String> {
            let present: bool = transaction
                .query_row(
                    "SELECT EXISTS(SELECT 1 FROM pragma_table_info('landing') WHERE name=?1)",
                    [name],
                    |row| row.get(0),
                )
                .map_err(database_error)?;
            Ok(if present {
                format!("l.{name}")
            } else {
                "NULL".into()
            })
        };
        let (reruns, left_out, fix_tasks) = (
            optional("ci_reruns")?,
            optional("left_out")?,
            optional("fix_tasks")?,
        );
        let in_window = "EXISTS(SELECT 1 FROM throughput_hourly h WHERE h.repo=l.repo
            AND unixepoch(l.landed_at)>=h.bucket AND unixepoch(l.landed_at)<h.through)";
        let build = "CASE WHEN unixepoch(l.landed_at)>=unixepoch(l.build_started_at)
            THEN unixepoch(l.landed_at)-unixepoch(l.build_started_at) END";
        let trains = query(&format!("SELECT l.train,l.landed_sha,l.landed_at,json_array_length(l.parts) AS parts,
            {build} AS build_seconds,
            1+{reruns} AS attempts, {reruns} AS ci_reruns, {left_out} AS drops, {fix_tasks} AS fix_tasks,
            1.0*({build})/60/NULLIF(json_array_length(l.parts),0) AS minutes_per_merged_batch,
            COUNT(a.head) AS accepted_parts, AVG(a.seconds) AS accept_to_main_seconds
            FROM landing l LEFT JOIN {accept} a ON a.repo=l.repo AND a.sha=l.landed_sha
            WHERE l.repo=?1 AND {in_window}
            GROUP BY l.train,l.landed_sha ORDER BY l.landed_at,l.landed_sha"))?;
        let hours = query("SELECT strftime('%Y-%m-%dT%H:00:00Z',h.bucket,'unixepoch') AS hour,h.commits,h.items_closed,
            (SELECT COUNT(*) FROM throughput_item_landing i WHERE i.repo=h.repo AND i.landed>=h.bucket
                AND i.landed<h.through) AS items_landed,
            (SELECT COALESCE(SUM(json_array_length(l.parts)),0) FROM landing l WHERE l.repo=h.repo
                AND unixepoch(l.landed_at)>=h.bucket AND unixepoch(l.landed_at)<h.through) AS batches_merged
            FROM throughput_hourly h WHERE h.repo=?1 ORDER BY h.bucket")?;
        transaction.commit().map_err(database_error)?;
        Ok(
            json!({"days":days,"hours":hours,"trains":trains,"writers":writers,"models":models,"quota":quota,"week_over_week":weeks}),
        )
    }
}
