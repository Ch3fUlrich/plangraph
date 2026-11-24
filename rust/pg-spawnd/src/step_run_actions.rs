//! The real actions of `pg-spawnd step-run`: the services of the keeper, GitHub and the
//! train desk, the native writer Host for the checkout, the gates and the agent turns, and
//! the shell publish of the accepted head. One receipt serves checks, review and push: the
//! stamp the Host's gates wrote is the one the push verifies.

use crate::native_writer_host::{Host, StepHost};
use crate::native_writer_steps::AgentSteps;
use crate::step_run::Args;
use pg_spawnd::step_runner::StepActions;
use pg_spawnd::step_runner_shell::{ShellConfig, ShellSteps, SystemRunner};
use pg_spawnd::step_services::{CiWait, ServiceSteps};
use pg_spawnd::step_services_cli::{GhCi, KeeperCli, TrainCommand};
use pg_worker_core::batch_steps::{
    CiClass, Claim, FastChecks, Fix, Push, Review, TrainJoin, Worktree, Write,
};

type Services = ServiceSteps<
    KeeperCli<SystemRunner>,
    GhCi<SystemRunner>,
    TrainCommand<SystemRunner>,
    AgentSteps<Host>,
>;

pub(crate) struct Production {
    shell: ShellSteps<SystemRunner>,
    services: Services,
    /// Keeps the supervisor lock of the run for as long as the list runs.
    _owner: pg_worker_core::lockfile::LockGuard,
}

pub(crate) fn production(args: &Args, host: StepHost) -> Production {
    let inputs = &args.inputs;
    let shell = ShellSteps {
        config: ShellConfig {
            repo_dir: host.repo.clone(),
            worktree_dir: host.worktree.clone(),
            branch: args
                .branch
                .clone()
                .unwrap_or_else(|| format!("lane/{}", inputs.batch_key)),
            // The Host's gates run the checks; the shell only publishes.
            checks: Vec::new(),
            pr_title: inputs.batch_key.clone(),
            pr_body: format!("Items: {}", inputs.items.join(", ")),
        },
        runner: SystemRunner,
    };
    let services = ServiceSteps {
        keeper: KeeperCli {
            runner: SystemRunner,
            dir: host.repo.clone(),
            prefix: args
                .keeper_cmd
                .clone()
                .unwrap_or_else(|| vec!["pg".into(), "--json".into()]),
        },
        ci: GhCi {
            runner: SystemRunner,
            dir: host.repo.clone(),
            repo: inputs.repo.clone(),
        },
        train: TrainCommand {
            runner: SystemRunner,
            dir: host.repo.clone(),
            argv: args.train_cmd.clone(),
        },
        wait: CiWait::default(),
        required: args.ci_workflows.clone(),
        sleep: Box::new(std::thread::sleep),
        rest: host.agents,
    };
    Production {
        shell,
        services,
        _owner: host._owner,
    }
}

impl StepActions for Production {
    fn claim(&mut self, items: &[String]) -> Result<Claim, String> {
        self.services.claim(items)
    }
    fn worktree(&mut self, claim: &str, base: &str) -> Result<Worktree, String> {
        self.services.rest.worktree(claim, base)
    }
    fn write(&mut self, worktree: &str, brief: &str) -> Result<Write, String> {
        self.services.rest.write(worktree, brief)
    }
    fn fast_checks(&mut self, head: &str) -> Result<FastChecks, String> {
        self.services.rest.fast_checks(head)
    }
    fn review(&mut self, head: &str, stamp: &str) -> Result<Review, String> {
        self.services.rest.review(head, stamp)
    }
    fn fix(&mut self, head: &str, session: &str, review: &str) -> Result<Fix, String> {
        self.services.rest.fix(head, session, review)
    }
    fn push(&mut self, head: &str, stamp: &str, _review: &str) -> Result<Push, String> {
        self.services.rest.verify_stamp(head, stamp)?;
        self.shell.publish(head)
    }
    fn ci_class(&mut self, head: &str, pr: u64) -> Result<CiClass, String> {
        self.services.ci_class(head, pr)
    }
    fn train_join(
        &mut self,
        head: &str,
        pr: u64,
        run: u64,
        attempt: u32,
    ) -> Result<TrainJoin, String> {
        self.services.train_join(head, pr, run, attempt)
    }
}
