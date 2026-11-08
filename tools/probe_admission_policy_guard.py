"""Two negative probes for the production admission guard; missing tools fail."""
import pathlib
import subprocess


ROOT = pathlib.Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "rust/worker/Cargo.toml"
SOURCE = ROOT / "rust/worker/src/lib.rs"


def main():
    manifest = MANIFEST.read_text()
    source = SOURCE.read_text()
    probes = [
        (
            "default-disabled-caller",
            'default = ["guard-probe-default"]\nguard-probe-default = []\n',
            '\n#[cfg(not(feature = "guard-probe-default"))]\n'
            'fn guard_probe() { let _ = pg_worker_core::config::Config::allow_all_for_tests(); }\n',
            "allow_all_for_tests",
        ),
        (
            "forbidden-default-alias",
            'default = ["guard-probe-alias"]\n'
            'guard-probe-alias = ["guard-probe-indirect"]\n'
            'guard-probe-indirect = ["pg-worker-core/test-support"]\n',
            "",
            "default feature closure enables a forbidden test feature or alias",
        ),
    ]
    for name, features, caller, diagnostic in probes:
        print(f"Running {name}", flush=True)
        try:
            if "default =" in manifest:
                raise SystemExit(f"FAIL: {name}: probe expects no existing defaults")
            MANIFEST.write_text(manifest + "\n[features]\n" + features)
            SOURCE.write_text(source + caller)
            result = subprocess.run(
                ["cargo", "test", "--test", "admission_policy_guard", "--", "--nocapture"],
                cwd=ROOT / "rust/worker",
                capture_output=True,
                text=True,
                timeout=590,
            )
            print(result.stdout + result.stderr, flush=True)
            if not (result.returncode != 0 and diagnostic in result.stdout + result.stderr):
                raise SystemExit(f"FAIL: {name}")
            print(f"PASS: {name} rejected by guard", flush=True)
        finally:
            MANIFEST.write_text(manifest)
            SOURCE.write_text(source)


if __name__ == "__main__":
    main()
