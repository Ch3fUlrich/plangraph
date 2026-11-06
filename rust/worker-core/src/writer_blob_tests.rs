use super::*;
#[test]
fn failed_seal_not_recorded() {
    let dir = tempfile::tempdir().unwrap();
    let mut store = LocalBlobStore::open(&dir.path().join("blobs")).unwrap();
    let blob = store.seal(b"result").unwrap();
    let mut metadata = ExecutionMetadata {
        run_id: "run".into(),
        repo_id: "fixture".into(),
        checkout: PathBuf::from("checkout"),
        base_sha: "a".repeat(40),
        head_sha: "b".repeat(40),
        branch: "lane/result".into(),
        kind: BlobKind::Result,
        blob,
    };
    store.bind(&metadata).unwrap();
    store.bind(&metadata).unwrap();
    metadata.kind = BlobKind::Salvage;
    assert!(matches!(store.bind(&metadata), Err(WriterError::Conflict)));
    assert_eq!(store.metadata("run").unwrap().kind, BlobKind::Result);
    metadata.run_id = "failed".into();
    metadata.blob.sha256 = "a".repeat(64);
    assert!(store.bind(&metadata).is_err());
    assert!(store.metadata("failed").is_err());
}
#[test]
fn atomic_seal_restart() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path().join("blobs");
    let blob = LocalBlobStore::open(&root)
        .unwrap()
        .seal(b"result")
        .unwrap();
    let store = LocalBlobStore::open(&root).unwrap();
    assert_eq!(store.read(&blob).unwrap(), b"result");
    assert!(store.metadata("missing").is_err());
}
#[test]
fn hash_length_cap_boundaries() {
    let dir = tempfile::tempdir().unwrap();
    let mut store = LocalBlobStore::open(&dir.path().join("blobs")).unwrap();
    let blob = store.seal(b"").unwrap();
    assert_eq!(blob.sha256, format!("{:x}", Sha256::digest(b"")));
    let mut wrong = blob.clone();
    wrong.bytes = 1;
    assert!(store.read(&wrong).is_err());
    wrong = blob;
    wrong.id = "../outside".into();
    assert!(store.read(&wrong).is_err());
    let bytes = vec![0; MAX_BYTES as usize];
    assert!(store.seal(&bytes).is_ok());
    assert!(matches!(
        store.seal(&vec![0; MAX_BYTES as usize + 1]),
        Err(WriterError::BundleTooLarge)
    ));
}
#[test]
fn corrupt_or_symlink_blob_refused() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path().join("blobs");
    let mut store = LocalBlobStore::open(&root).unwrap();
    let blob = store.seal(b"result").unwrap();
    fs::write(root.join(&blob.id), b"broken").unwrap();
    assert!(store.read(&blob).is_err());
    #[cfg(unix)]
    {
        fs::remove_file(root.join(&blob.id)).unwrap();
        std::os::unix::fs::symlink("missing", root.join(&blob.id)).unwrap();
        assert!(store.read(&blob).is_err());
        assert!(store.seal(b"result").is_err());
    }
}
