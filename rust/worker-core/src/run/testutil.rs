//! Shared unchanged execution-group test doubles.
use super::{group::*, supervise::*};
use std::sync::{
    atomic::{AtomicBool, AtomicUsize, Ordering},
    Arc,
};
struct Guard(Arc<AtomicUsize>);
impl Drop for Guard {
    fn drop(&mut self) {
        self.0.fetch_sub(1, Ordering::SeqCst);
    }
}
pub(crate) struct Count(pub(crate) Arc<AtomicUsize>);
impl Admission for Count {
    fn acquire(&self, _: &str, _: &AtomicBool) -> Admit {
        self.0.fetch_add(1, Ordering::SeqCst);
        Admit::Granted(Permit::new(Guard(self.0.clone())))
    }
}
struct FakeGroup {
    identity: Identity,
    empty: Arc<AtomicBool>,
}
impl RunGroup for FakeGroup {
    fn identity(&self) -> &Identity {
        &self.identity
    }
    fn members(&self) -> std::io::Result<Vec<u32>> {
        Ok(if self.empty.load(Ordering::SeqCst) {
            vec![]
        } else {
            vec![7, 3]
        })
    }
    fn terminate(&self) {}
    fn kill(&self) {}
}
pub(crate) struct Survivors(pub(crate) Arc<AtomicBool>);
impl Launcher for Survivors {
    fn launch(
        &self,
        unit: &str,
        spec: &LaunchSpec<'_>,
        c: &Confinement,
        l: &RunLimits,
    ) -> Result<Grouped, LaunchError> {
        let mut g = SystemLauncher.launch(unit, spec, c, l)?;
        g.group = Box::new(FakeGroup {
            identity: Identity {
                pid: g.child.id(),
                start_time: None,
                group: None,
            },
            empty: self.0.clone(),
        });
        Ok(g)
    }
}
