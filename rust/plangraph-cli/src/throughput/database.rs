use super::{runs::Inputs, Arrival, Hour, HOUR};
use serde_json::{json, Value};
use std::{
    collections::BTreeMap,
    fmt::Write as _,
    io::{Seek, Write as _},
    path::PathBuf,
    process::{Command, Stdio},
};

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
CREATE TABLE IF NOT EXISTS throughput_quota_hourly (
    repo TEXT NOT NULL, bucket INTEGER NOT NULL, through INTEGER NOT NULL, provider TEXT NOT NULL,
    remaining REAL NOT NULL, PRIMARY KEY(repo,bucket,provider)
);
CREATE TABLE IF NOT EXISTS throughput_accept (
    repo TEXT NOT NULL, sha TEXT NOT NULL, landed INTEGER NOT NULL,
    seconds INTEGER NOT NULL, model TEXT, tokens INTEGER, commits INTEGER NOT NULL,
    PRIMARY KEY(repo,sha)
);";

fn sql_text(text: &str) -> String {
    // A hex literal cannot inject SQL or become a sqlite3 dot command.
    let hex: String = text.bytes().map(|byte| format!("{byte:02x}")).collect();
    format!("CAST(X'{hex}' AS TEXT)")
}

impl Db {
    fn execute(&self, sql: &str, readonly: bool) -> Result<Vec<u8>, String> {
        if !self.0.is_file() {
            return Err("landing database unavailable; initialize it through the lander".into());
        }
        let mut input = tempfile::tempfile().map_err(|e| e.to_string())?;
        input
            .write_all(sql.as_bytes())
            .and_then(|_| input.rewind())
            .map_err(|e| e.to_string())?;
        let mut command = Command::new("sqlite3");
        command.args([
            "-bail",
            "-batch",
            "-ifexists",
            "-json",
            "-cmd",
            ".timeout 30000",
        ]);
        if readonly {
            command.arg("-readonly");
        }
        let output = command
            .arg(&self.0)
            .stdin(Stdio::from(input))
            .output()
            .map_err(|e| format!("cannot run sqlite3: {e}"))?;
        if !output.status.success() {
            return Err(format!(
                "landing database: {}",
                String::from_utf8_lossy(&output.stderr).trim()
            ));
        }
        Ok(output.stdout)
    }

    fn query(&self, sql: &str) -> Result<Value, String> {
        let output = self.execute(sql, true)?;
        if output.is_empty() {
            return Ok(json!([]));
        }
        serde_json::from_slice(&output).map_err(|e| format!("invalid SQLite output: {e}"))
    }

    pub fn check_landing(&self) -> Result<(), String> {
        let keys =
            self.query("SELECT name FROM pragma_table_info('landing') WHERE pk > 0 ORDER BY pk;")?;
        if keys != json!([{"name":"repo"},{"name":"landed_sha"}]) {
            return Err("landing database must use the lander's (repo, landed_sha) schema".into());
        }
        Ok(())
    }

    pub fn through(&self, repo: &str) -> Result<Option<i64>, String> {
        if self.query(
            "SELECT name FROM sqlite_schema WHERE type='table' AND name='throughput_hourly';",
        )? == json!([])
        {
            return Ok(None);
        }
        let result = self.query(&format!(
            "SELECT MAX(through) AS last FROM throughput_hourly WHERE repo={};",
            sql_text(repo)
        ))?;
        Ok(result[0]["last"].as_i64())
    }

    pub fn store(
        &self,
        repo: &str,
        end: i64,
        hours: &BTreeMap<i64, Hour>,
        arrivals: &[Arrival],
        inputs: &Inputs,
    ) -> Result<(), String> {
        let mut sql = format!("BEGIN IMMEDIATE;\n{SCHEMA}\n");
        let repo = sql_text(repo);
        for (&bucket, value) in hours {
            let through = end.min(bucket + HOUR);
            let (commits, items) = (value.commits, value.items_closed);
            writeln!(sql, "INSERT INTO throughput_hourly VALUES({repo},{bucket},{through},{commits},{items})
                ON CONFLICT(repo,bucket) DO UPDATE SET through=excluded.through, commits=excluded.commits,
                items_closed=excluded.items_closed WHERE excluded.through >= throughput_hourly.through;").unwrap();
            for (host, time) in inputs.writer_time(bucket, through) {
                let host = sql_text(host);
                let (running, measured, active) = (time.running, time.measured, time.active);
                writeln!(sql, "INSERT INTO throughput_writer_hourly VALUES({repo},{bucket},{through},{host},{running},{measured},{active})
                    ON CONFLICT(repo,bucket,host) DO UPDATE SET through=excluded.through,running=excluded.running,
                    measured=excluded.measured,active=excluded.active WHERE excluded.through >= throughput_writer_hourly.through;").unwrap();
            }
            for (provider, remaining) in inputs.quota(through - 1) {
                let provider = sql_text(provider);
                writeln!(sql, "INSERT INTO throughput_quota_hourly VALUES({repo},{bucket},{through},{provider},{remaining})
                    ON CONFLICT(repo,bucket,provider) DO UPDATE SET through=excluded.through,remaining=excluded.remaining
                    WHERE excluded.through >= throughput_quota_hourly.through;").unwrap();
            }
        }
        for arrival in arrivals {
            for item in &arrival.items {
                writeln!(sql, "INSERT INTO throughput_item_landing VALUES({repo},{},{})
                    ON CONFLICT(repo,item) DO UPDATE SET landed=MIN(throughput_item_landing.landed,excluded.landed);",
                    sql_text(item), arrival.at).unwrap();
            }
            if let Some(accepted) = inputs.accepted(arrival) {
                let sha = sql_text(&arrival.sha);
                let model = accepted.model.map_or_else(|| "NULL".into(), sql_text);
                let tokens = accepted
                    .tokens
                    .map_or_else(|| "NULL".into(), |n| n.to_string());
                let commits = accepted.commits;
                writeln!(sql, "INSERT INTO throughput_accept VALUES({repo},{sha},{},{},{model},{tokens},{commits})
                    ON CONFLICT(repo,sha) DO UPDATE SET seconds=excluded.seconds,
                    model=excluded.model,tokens=excluded.tokens,commits=excluded.commits;",
                    arrival.at, accepted.seconds).unwrap();
            }
        }
        let first = hours.first_key_value().ok_or("no throughput hours")?.0;
        let last = end - 1;
        writeln!(
            sql,
            "INSERT INTO throughput_daily
            SELECT repo,date(bucket,'unixepoch'),SUM(through-bucket),SUM(commits),SUM(items_closed)
            FROM throughput_hourly WHERE repo={repo}
            AND bucket BETWEEN ({first}/86400)*86400 AND (({last}/86400)+1)*86400-1
            GROUP BY repo,date(bucket,'unixepoch')
            ON CONFLICT(repo,day) DO UPDATE SET observed_seconds=excluded.observed_seconds,
            commits=excluded.commits,items_closed=excluded.items_closed;\nCOMMIT;"
        )
        .unwrap();
        self.execute(&sql, false)?;
        Ok(())
    }

    pub fn report(&self, repo: &str) -> Result<Value, String> {
        self.check_landing()?;
        let repo = sql_text(repo);
        let days = self.query(&format!("WITH train AS (
            SELECT date(landed_at) AS day, COUNT(*) AS trains, SUM(json_array_length(parts)) AS train_parts,
            SUM(CASE WHEN unixepoch(landed_at)>=unixepoch(build_started_at)
                THEN unixepoch(landed_at)-unixepoch(build_started_at) END) AS train_seconds
            FROM landing l WHERE repo={repo} AND EXISTS(SELECT 1 FROM throughput_hourly h
                WHERE h.repo=l.repo AND unixepoch(l.landed_at)>=h.bucket AND unixepoch(l.landed_at)<h.through)
            GROUP BY date(landed_at)
        ), accepted AS (
            SELECT date(landed,'unixepoch') AS day, COUNT(*) AS accept_samples, AVG(seconds) AS accept_to_main_seconds
            FROM throughput_accept WHERE repo={repo} GROUP BY date(landed,'unixepoch')
        ), items AS (
            SELECT date(landed,'unixepoch') AS day, COUNT(*) AS items_landed
            FROM throughput_item_landing WHERE repo={repo} GROUP BY date(landed,'unixepoch')
        ) SELECT d.day,observed_seconds,commits,items_closed,COALESCE(items_landed,0) AS items_landed,COALESCE(trains,0) AS trains,
            COALESCE(train_parts,0) AS train_parts,train_seconds,COALESCE(accept_samples,0) AS accept_samples,accept_to_main_seconds
            FROM throughput_daily d LEFT JOIN train USING(day) LEFT JOIN accepted USING(day) LEFT JOIN items USING(day)
            WHERE repo={repo} ORDER BY d.day;"))?;
        let writers = self.query(&format!(
            "SELECT date(bucket,'unixepoch') AS day,host,
            SUM(running) AS writer_running_seconds, SUM(measured) AS writer_measured_seconds,
            1.0*SUM(running)/NULLIF(SUM(through-bucket),0) AS mean_running_writers,
            1.0*SUM(active)/NULLIF(SUM(measured),0) AS writer_active_share
            FROM throughput_writer_hourly WHERE repo={repo} GROUP BY day,host ORDER BY day,host;"
        ))?;
        let models = self.query(&format!("SELECT date(landed,'unixepoch') AS day,model,SUM(tokens) AS tokens,
            SUM(commits) AS landed_commits, 1.0*SUM(tokens)/NULLIF(SUM(commits),0) AS tokens_per_landed_commit
            FROM throughput_accept WHERE repo={repo} AND tokens IS NOT NULL AND model IS NOT NULL
            GROUP BY day,model ORDER BY day,model;"))?;
        let quota = self.query(&format!("SELECT date(bucket,'unixepoch') AS day,provider,MIN(remaining) AS quota_headroom_percent
            FROM throughput_quota_hourly WHERE repo={repo} GROUP BY day,provider ORDER BY day,provider;"))?;
        let weeks = self.query(&format!("WITH latest AS (SELECT MAX(day) AS last FROM throughput_daily WHERE repo={repo}),
            periods AS (SELECT d.*,CASE WHEN d.day>date(last,'-7 days') THEN 'current' ELSE 'previous' END AS week
            FROM throughput_daily d CROSS JOIN latest WHERE repo={repo} AND day>date(last,'-14 days'))
            SELECT week,COUNT(*) AS days,SUM(observed_seconds) AS observed_seconds,
            SUM(commits) AS commits,SUM(items_closed) AS items_closed,
            86400.0*SUM(commits)/NULLIF(SUM(observed_seconds),0) AS commits_per_observed_day
            FROM periods GROUP BY week ORDER BY week;"))?;
        let trains = self.query(&format!("SELECT l.train,l.landed_sha,l.landed_at,json_array_length(l.parts) AS parts,
            CASE WHEN unixepoch(l.landed_at)>=unixepoch(l.build_started_at)
                THEN unixepoch(l.landed_at)-unixepoch(l.build_started_at) END AS build_seconds,
            a.seconds AS accept_to_main_seconds
            FROM landing l LEFT JOIN throughput_accept a ON a.repo=l.repo AND a.sha=l.landed_sha
            WHERE l.repo={repo} AND EXISTS(SELECT 1 FROM throughput_hourly h
                WHERE h.repo=l.repo AND unixepoch(l.landed_at)>=h.bucket AND unixepoch(l.landed_at)<h.through)
            ORDER BY l.landed_at,l.landed_sha;"))?;
        Ok(
            json!({"days":days,"trains":trains,"writers":writers,"models":models,"quota":quota,"week_over_week":weeks}),
        )
    }
}
