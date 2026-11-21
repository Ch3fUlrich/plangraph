//! A secret that straddles the capture cut must never survive as a fragment.
use super::*;

/// A token-shaped fake secret assembled from fragments (no token-shaped literal in source).
fn secret() -> String {
    ["gh", "p_", &"Qr7x".repeat(10)].concat()
}

/// Hands out its data in fixed-size chunks, like a pipe read.
struct Chunks {
    data: Vec<u8>,
    at: usize,
    size: usize,
}
impl Read for Chunks {
    fn read(&mut self, out: &mut [u8]) -> io::Result<usize> {
        let n = self.size.min(out.len()).min(self.data.len() - self.at);
        out[..n].copy_from_slice(&self.data[self.at..self.at + n]);
        self.at += n;
        Ok(n)
    }
}

fn tail(data: Vec<u8>, size: usize, max: usize, redact: bool) -> String {
    let last = AtomicU64::new(0);
    let reader = Chunks { data, at: 0, size };
    String::from_utf8_lossy(&read_tail(reader, Instant::now(), &last, max, redact)).into_owned()
}

fn fragment(secret: &str) -> &str {
    &secret[12..24]
}

#[test]
fn final_cut_through_a_secret_leaves_no_fragment() {
    let s = secret();
    let data = [" ".repeat(20), s.clone(), " ".repeat(5)]
        .concat()
        .into_bytes();
    let cut_inside = data.len() - 35;
    assert!(cut_inside > 20 && cut_inside < 20 + s.len());
    let raw = tail(data.clone(), 4096, 35, false);
    assert!(
        raw.contains(fragment(&s)),
        "the unredacted capture keeps the fragment: {raw:?}"
    );
    let out = tail(data, 4096, 35, true);
    assert!(
        !out.contains(fragment(&s)),
        "fragment survived the cut: {out:?}"
    );
}

#[test]
fn midstream_drain_through_a_secret_leaves_no_fragment() {
    let s = secret();
    // 16-byte chunks: the front is drained again and again while the secret streams in.
    let mut data = " ".repeat(30).into_bytes();
    data.extend_from_slice(s.as_bytes());
    data.extend_from_slice(" ".repeat(70).as_bytes());
    let out = tail(data.clone(), 16, 40, true);
    assert!(
        !out.contains(fragment(&s)),
        "fragment survived a mid-stream drain: {out:?}"
    );
    assert!(!tail(data, 16, 40, false).is_empty());
}

#[test]
fn redacting_capture_keeps_plain_text_and_a_split_utf8_tail() {
    let data = ["plain output ", "é"].concat().into_bytes();
    // Chunk size 1 splits the two-byte character; the tail must stay intact text.
    assert_eq!(tail(data, 1, 1 << 20, true), "plain output é");
}

#[test]
fn supervise_redacts_before_the_capture_limit_cuts() {
    let s = secret();
    let script = format!("printf '%s' '{}{}{}'", " ".repeat(20), s, " ".repeat(5));
    let mut cmd = Command::new("sh");
    cmd.arg("-c").arg(script);
    let opts = Opts {
        max_output: 35,
        redact_discarded: true,
        grace: Duration::from_millis(300),
        ..Opts::default()
    };
    let out = String::from_utf8_lossy(&supervise(cmd, &opts).unwrap().output).into_owned();
    assert!(
        !out.contains(fragment(&s)),
        "fragment survived supervision: {out:?}"
    );
}
