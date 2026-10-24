//! Stable, dependency-free content hash (FNV-1a 64) for diff / output identity.

const OFFSET: u64 = 0xcbf2_9ce4_8422_2325;
const PRIME: u64 = 0x0000_0100_0000_01b3;

/// Incremental FNV-1a hasher; stable across Rust versions and platforms.
pub struct Fnv(u64);

impl Default for Fnv {
    fn default() -> Self {
        Fnv(OFFSET)
    }
}

impl Fnv {
    pub fn update(&mut self, bytes: &[u8]) {
        for b in bytes {
            self.0 ^= u64::from(*b);
            self.0 = self.0.wrapping_mul(PRIME);
        }
    }

    pub fn hex(&self) -> String {
        format!("{:016x}", self.0)
    }
}

pub fn hash_bytes(bytes: &[u8]) -> String {
    let mut h = Fnv::default();
    h.update(bytes);
    h.hex()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn known_vectors() {
        assert_eq!(hash_bytes(b""), "cbf29ce484222325");
        assert_eq!(hash_bytes(b"a"), "af63dc4c8601ec8c");
        assert_ne!(hash_bytes(b"a"), hash_bytes(b"b"));
    }
}
