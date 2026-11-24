//! The admission of one writer launch: the check of its keys and their reservation as one step
//! under the state's lock, and the guard that releases the reservation when the launch ends.

use crate::lane_guard::{admit, launch_keys, Observed};
use std::path::{Path, PathBuf};

/// What a launch was admitted with: its keys and the reason of a deliberate second lane. While
/// it lives the keys stay reserved for this launch; dropping it releases them, so every refused
/// or failed launch path frees the keys and the run record the launch registered holds them.
#[derive(Debug)]
pub struct Launched {
    pub keys: Vec<String>,
    pub lane_override: Option<String>,
    reservation: Option<Reservation>,
}

impl Launched {
    /// The launch starts its supervisor now: the claim then outlives this dispatcher for as
    /// long as the supervisor can still register the run. A launch without a reservation
    /// (check only) has nothing to mark. A claim that expired while the dispatcher was paused is
    /// refused, never revived: another admission may have taken the key.
    pub fn mark_started(&self) -> Result<(), String> {
        match &self.reservation {
            Some(held) => crate::lane_reservation::start(&held.state, &held.keys, &held.owner),
            None => Ok(()),
        }
    }
}

/// The reservation of an admitted launch; dropping it releases the keys of this owner.
#[derive(Debug)]
struct Reservation {
    state: PathBuf,
    keys: Vec<String>,
    owner: String,
}

impl Drop for Reservation {
    fn drop(&mut self) {
        // Under the lock, like every admission, so none runs between the release and what it
        // follows. An admission that holds the lock for longer than `RELEASE_WAIT` must not keep a
        // finished launch's claim alive for its whole lease: the release then goes ahead without
        // the lock, which is safe because admissions read the claims before the runs.
        match crate::lane_reservation::lock_for(&self.state, RELEASE_WAIT) {
            Ok(_lock) => {
                crate::lane_reservation::release(&self.state, &self.keys, &self.owner);
                crate::lane_reservation::prune(&self.state, &self.keys);
            }
            Err(_) => crate::lane_reservation::release(&self.state, &self.keys, &self.owner),
        }
    }
}

/// How long a finished launch waits for the admission lock before it releases without it.
const RELEASE_WAIT: std::time::Duration = std::time::Duration::from_secs(2);

/// The most keys one launch holds: the run record keeps a batch of at most this many.
pub const MAX_KEYS: usize = 50;

/// One writer launch to admit.
pub struct LaunchRequest<'a> {
    pub prompt: &'a str,
    /// Keys the launch card names besides the brief's own.
    pub extra: &'a [String],
    /// The one state path of the entry point: lock, reservation and run lookup all use it.
    pub state: &'a Path,
    pub repo: &'a Path,
    /// The override reason of the launch card (`lane_override_reason`), the only override a
    /// dispatch admits: a `lane_override` in the typed prompt is metadata for the renderer.
    pub explicit: Option<&'a str>,
    /// Reserve the admitted keys for this launch; a check-only caller (a dry run) leaves them free.
    pub reserve: bool,
}

/// The one admission of a writer launch: render the prompt, take the override from `explicit`,
/// and check the keys of the brief and of the card against the live runs,
/// the remote lane branches of `repo` and the reservations of other launches. With `reserve`
/// the check and the reservation are one step under the state's lock, so two concurrent launches
/// of a key cannot both be admitted.
pub fn admit_launch(request: &LaunchRequest) -> Result<Launched, String> {
    let rendered = crate::batch_writer_brief::render_input(request.prompt)?;
    let owner = crate::lane_reservation::new_owner();
    let _lock = request
        .reserve
        .then(|| crate::lane_reservation::lock(request.state))
        .transpose()?;
    // One reading of the claim clock, taken under the lock: the scan of the claims, the sweep of
    // the lapsed ones and the time stamped on the new claim all decide with it.
    let at = crate::lane_reservation::now_secs();
    let observed = Observed {
        state: request.state.to_owned(),
        repo: request.repo.to_owned(),
        owner: owner.clone(),
        claims: crate::lane_reservation::Snapshot::take(
            request.state,
            &launch_keys(&rendered.brief, request.extra),
            at,
        ),
    };
    let (lane_override, keys) = admit(&rendered.brief, request.extra, &observed, request.explicit)?;
    if keys.len() > MAX_KEYS {
        return Err(format!("a launch holds at most {MAX_KEYS} item keys"));
    }
    let reservation = if request.reserve {
        // The admission passed over any claim that lapsed: take those away for good. The claims the scan judged are the ones swept; any claim that cannot be read or removed
        // refuses the admission before the replacement is published.
        observed
            .claims
            .sweep()
            .map_err(|error| format!("lane reservation: {error}"))?;
        if let Err(error) = crate::lane_reservation::reserve(request.state, &keys, &owner, at) {
            // Still under the lock: drop what was written before the failure.
            crate::lane_reservation::release(request.state, &keys, &owner);
            crate::lane_reservation::prune(request.state, &keys);
            return Err(format!(
                "lane reservation: claim cannot be published: {error}"
            ));
        }
        Some(Reservation {
            state: request.state.to_owned(),
            keys: keys.clone(),
            owner,
        })
    } else {
        None
    };
    Ok(Launched {
        keys,
        lane_override,
        reservation,
    })
}
