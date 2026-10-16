//! Worker. Env: SPIKE_ADDR (default http://127.0.0.1:7233), SPIKE_BUILD_ID,
//! SPIKE_DEPLOYMENT (set both to opt into worker deployment versioning).

use std::str::FromStr;
use temporal_spike::{TASK_QUEUE, activities::CardActivities, card::Card};
use temporalio_client::{Client, ClientOptions, Connection, ConnectionOptions, Url};
use temporalio_common::worker::{
    VersioningBehavior, WorkerDeploymentOptions, WorkerDeploymentVersion,
};
use temporalio_sdk::{Runtime, Worker, WorkerOptions};

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    let addr = std::env::var("SPIKE_ADDR").unwrap_or_else(|_| "http://127.0.0.1:7233".into());
    let runtime = Runtime::from_current_tokio(Default::default())?;
    let tls = temporal_spike::tls::tls_from_env().map_err(|e| anyhow::anyhow!(e))?;
    temporal_spike::tls::require_https(&addr, &tls).map_err(|e| anyhow::anyhow!(e))?;
    let conn = Connection::connect(
        ConnectionOptions::new(Url::from_str(&addr)?)
            .maybe_tls_options(tls)
            .build(),
    )
    .await?;
    let client = Client::new(conn, ClientOptions::new("default").build())?;

    let builder = WorkerOptions::new(TASK_QUEUE)
        .register_workflow::<Card>()?
        .register_activities(CardActivities);
    let opts = match (
        std::env::var("SPIKE_DEPLOYMENT"),
        std::env::var("SPIKE_BUILD_ID"),
    ) {
        (Ok(dep), Ok(build)) => {
            println!("worker versioning on: deployment={dep} build_id={build}");
            let version = WorkerDeploymentVersion::builder()
                .deployment_name(dep)
                .build_id(build)
                .build();
            builder
                .deployment_options(
                    WorkerDeploymentOptions::new(version)
                        .use_worker_versioning(true)
                        .default_versioning_behavior(VersioningBehavior::Pinned)
                        .build(),
                )
                .build()
        }
        _ => builder.build(),
    };
    let mut worker = Worker::new(&runtime, client, opts)?;
    println!("worker up on task queue {TASK_QUEUE}");
    worker.run().await?;
    Ok(())
}
