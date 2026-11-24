//! Bind native fast checks to the prepared comparison before publishing a receipt.
use super::Host;
use plangraph_cli::fast_checks::{replay_committed, ReplayReceipt};
use std::{
    fs,
    path::{Path, PathBuf},
};

impl Host {
    pub(super) fn stamp(&self, head: &str) -> Result<PathBuf, String> {
        if head.len() != 40 || !head.bytes().all(|byte| byte.is_ascii_hexdigit()) {
            return Err("invalid head".into());
        }
        let gitdir = self.git(&["rev-parse", "--absolute-git-dir"])?;
        Ok(Path::new(&gitdir).join("fast-checks").join(head))
    }

    fn replay_base(&self, head: &str) -> Result<String, String> {
        let pinned = self.git(&["rev-parse", "--verify", &self.base_pin()])?;
        if let Ok(record) = crate::records::read_record_file(&self.record_path) {
            let prepared = &record["review_base"];
            if (record["run_kind"] == "native-codex" || !prepared.is_null())
                && (prepared["base"] != pinned || prepared["head"] != head)
            {
                return Err("prepared review range differs from the writer checkout pins".into());
            }
        }
        Ok(pinned)
    }

    pub(super) fn replay_receipt(&self, head: &str) -> Result<ReplayReceipt, String> {
        let base = self.replay_base(head)?;
        let receipt = ReplayReceipt::read(&self.stamp(head)?)?;
        if !receipt.matches(&base, head) {
            return Err(format!(
                "fast-checks replay receipt does not match {base}..{head}"
            ));
        }
        Ok(receipt)
    }

    pub(super) fn admitted_replay(&self, head: &str) -> Result<ReplayReceipt, String> {
        let receipt = self.replay_receipt(head)?;
        if let Ok(record) = crate::records::read_record_file(&self.record_path) {
            if record["run_kind"] == "native-codex" {
                let checks = &record["fast_checks"];
                if checks["status"] != "passed"
                    || checks["head"] != head
                    || checks["base"] != receipt.base
                    || checks["replay"] != serde_json::json!(receipt)
                {
                    return Err(
                        "owning writer has not published the matching replay receipt".into(),
                    );
                }
            }
        }
        Ok(receipt)
    }

    pub(super) fn run_fast_checks(&self, head: &str) -> Result<(), String> {
        let _guard = crate::writer_control::mutation_guard(&self.record_path)?;
        let stamp = self.stamp(head)?;
        fs::create_dir_all(stamp.parent().ok_or("stamp directory missing")?)
            .map_err(|error| error.to_string())?;
        if let Err(error) = fs::remove_file(&stamp) {
            if error.kind() != std::io::ErrorKind::NotFound {
                return Err(error.to_string());
            }
        }
        if self.git(&["rev-parse", "HEAD"])? != head {
            return Err(format!("worktree is not at {head}"));
        }
        let base = self.replay_base(head)?;
        // Failed heads keep their own turns, so the next head's evidence starts after them.
        self.session_excerpt(&stamp)?;
        let mut log = self.checks()?;
        let receipt = replay_committed(&self.cwd, &base, head, Some(&self.tool_path))?;
        log.push_str(&receipt.evidence());
        fs::write(stamp.with_extension("log"), log).map_err(|e| e.to_string())?;
        receipt.write(&stamp)
    }
}
