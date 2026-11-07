use super::*;
#[test]
fn finalize_scan_chunk_edge_and_truncation() {
    let secret = ["Bear", "er ", &"z9".repeat(16)].concat();
    let mut content = vec![b' '; CHUNK - 3];
    content.extend(secret.bytes());
    let mut stream = format!("oid blob {}\n", content.len()).into_bytes();
    stream.extend(content);
    stream.push(b'\n');
    assert_eq!(
        batch(&mut &stream[..], b"oid\n"),
        Err(Refusal::SecretInHistory)
    );
    for stream in [
        b"oid blob 9\nshort".as_slice(),
        b"oid blob 1\nx",
        b"oid missing\n",
    ] {
        assert_eq!(batch(&mut &stream[..], b"oid\n"), Err(Refusal::BadBundle));
    }
}
#[test]
fn finalize_scan_many_blobs_are_consumed_incrementally() {
    struct Counting {
        left: usize,
        offset: usize,
        largest: usize,
    }
    impl Read for Counting {
        fn read(&mut self, out: &mut [u8]) -> std::io::Result<usize> {
            self.largest = self.largest.max(out.len());
            let object = b"oid blob 4\nsafe\n";
            let mut n = 0;
            while n < out.len() && self.left > 0 {
                out[n] = object[self.offset];
                n += 1;
                self.offset += 1;
                if self.offset == object.len() {
                    self.offset = 0;
                    self.left -= 1;
                }
            }
            Ok(n)
        }
    }
    let mut source = Counting {
        left: 2000,
        offset: 0,
        largest: 0,
    };
    batch(&mut BufReader::new(&mut source), &b"oid\n".repeat(2000)).unwrap();
    assert_eq!(source.left, 0);
    assert!(source.largest <= CHUNK + OVERLAP);
}

#[test]
fn finalize_scan_multi_mib_buffer_is_bounded() {
    struct Counting {
        remaining: usize,
        largest: usize,
        total: usize,
    }
    impl Read for Counting {
        fn read(&mut self, out: &mut [u8]) -> std::io::Result<usize> {
            self.largest = self.largest.max(out.len());
            let count = out.len().min(self.remaining);
            out[..count].fill(b' ');
            self.remaining -= count;
            self.total += count;
            Ok(count)
        }
    }
    let size = 5 * 1024 * 1024 + 17;
    let mut reader = Counting {
        remaining: size,
        largest: 0,
        total: 0,
    };
    scan_chunks(&mut reader, size, true).unwrap();
    assert_eq!(reader.total, size);
    assert_eq!(reader.remaining, 0);
    assert_eq!(reader.largest, CHUNK);
    assert_eq!(std::mem::size_of::<[u8; CHUNK + OVERLAP]>(), 81920);
    eprintln!("multi-MiB scan: max buffer bytes = {}", CHUNK + OVERLAP);
}
