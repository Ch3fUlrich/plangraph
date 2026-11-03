use crate::state::resolve_state;
use std::path::PathBuf;

#[test]
fn precedence_and_empty_values() {
    let home = if cfg!(windows) { "USERPROFILE" } else { "HOME" };
    let other = if cfg!(windows) { "HOME" } else { "USERPROFILE" };
    for (flag, env, expected) in [
        (
            Some("flag"),
            vec![
                ("PLANGRAPH_SPAWN_STATE", "spawn"),
                ("XDG_STATE_HOME", "xdg"),
                (home, "home"),
            ],
            Some("flag"),
        ),
        (
            None,
            vec![
                ("PLANGRAPH_SPAWN_STATE", "relative"),
                ("XDG_STATE_HOME", "xdg"),
            ],
            Some("relative"),
        ),
        (
            Some(""),
            vec![
                ("PLANGRAPH_SPAWN_STATE", ""),
                ("XDG_STATE_HOME", "xdg"),
                (home, "home"),
            ],
            Some("xdg/plangraph"),
        ),
        (
            None,
            vec![("XDG_STATE_HOME", ""), (home, "home"), (other, "wrong")],
            Some("home/.local/state/plangraph"),
        ),
        (None, vec![(home, ""), (other, "wrong")], None),
        (None, vec![], None),
    ] {
        let result = resolve_state(flag, |key| {
            env.iter()
                .find(|(k, _)| *k == key)
                .map(|(_, v)| (*v).into())
        });
        assert_eq!(result.ok(), expected.map(PathBuf::from));
    }
}
