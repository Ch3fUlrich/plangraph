//! Private immutable SHA-256 blobs and execution bindings. No expiry or completion.
use crate::writer_result::{oid, safe_id, WriterError};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{
    fs,
    io::{Read, Write},
    path::{Path, PathBuf},
};
pub const MAX_BYTES: u64 = 64 * 1024 * 1024;
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SealedBlob {
    pub id: String,
    pub sha256: String,
    pub bytes: u64,
}
pub trait BlobStore {
    fn seal(&mut self, bytes: &[u8]) -> Result<SealedBlob, WriterError>;
    fn read(&self, blob: &SealedBlob) -> Result<Vec<u8>, WriterError>;
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub enum BlobKind {
    Result,
    Salvage,
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ExecutionMetadata {
    pub run_id: String,
    pub repo_id: String,
    pub checkout: PathBuf,
    pub base_sha: String,
    pub head_sha: String,
    pub branch: String,
    pub kind: BlobKind,
    pub blob: SealedBlob,
}
pub struct LocalBlobStore {
    root: PathBuf,
}
fn digest(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}
fn regular(path: &Path) -> Result<fs::Metadata, WriterError> {
    let meta = fs::symlink_metadata(path)?;
    if !meta.file_type().is_file() {
        return Err(WriterError::Verify);
    }
    Ok(meta)
}
impl LocalBlobStore {
    pub fn open(root: &Path) -> Result<Self, WriterError> {
        if !root.exists() {
            let mut builder = fs::DirBuilder::new();
            builder.recursive(true);
            #[cfg(unix)]
            {
                use std::os::unix::fs::DirBuilderExt;
                builder.mode(0o700);
            }
            builder.create(root)?;
        }
        let meta = fs::symlink_metadata(root)?;
        if !meta.is_dir() || meta.file_type().is_symlink() {
            return Err(WriterError::Verify);
        }
        #[cfg(unix)]
        {
            use std::os::unix::fs::MetadataExt;
            if meta.uid() != unsafe { libc::geteuid() } || meta.mode() & 0o077 != 0 {
                return Err(WriterError::Verify);
            }
        }
        Ok(Self {
            root: root.canonicalize()?,
        })
    }
    fn install(&self, name: &str, bytes: &[u8]) -> Result<(), WriterError> {
        let mut temp = tempfile::NamedTempFile::new_in(&self.root)?;
        temp.write_all(bytes)?;
        temp.as_file().sync_all()?;
        let dest = self.root.join(name);
        match temp.persist_noclobber(&dest) {
            Ok(_) => (),
            Err(err) if err.error.kind() == std::io::ErrorKind::AlreadyExists => {
                regular(&dest)?;
                if fs::read(dest)? != bytes {
                    return Err(WriterError::Conflict);
                }
            }
            Err(_) => return Err(WriterError::Io),
        }
        #[cfg(unix)]
        fs::File::open(&self.root)?.sync_all()?;
        Ok(())
    }
    pub fn bind(&self, metadata: &ExecutionMetadata) -> Result<(), WriterError> {
        if !safe_id(&metadata.run_id) || !oid(&metadata.base_sha) || !oid(&metadata.head_sha) {
            return Err(WriterError::Invalid);
        }
        self.read(&metadata.blob)?;
        let bytes = serde_json::to_vec(metadata).map_err(|_| WriterError::Invalid)?;
        self.install(&format!("{}.meta", metadata.run_id), &bytes)
    }
    pub fn metadata(&self, run_id: &str) -> Result<ExecutionMetadata, WriterError> {
        if !safe_id(run_id) {
            return Err(WriterError::Invalid);
        }
        let path = self.root.join(format!("{run_id}.meta"));
        if regular(&path)?.len() > 16384 {
            return Err(WriterError::Invalid);
        }
        let metadata: ExecutionMetadata =
            serde_json::from_slice(&fs::read(path)?).map_err(|_| WriterError::Verify)?;
        if metadata.run_id != run_id {
            return Err(WriterError::Verify);
        }
        self.read(&metadata.blob)?;
        Ok(metadata)
    }
}
impl BlobStore for LocalBlobStore {
    fn seal(&mut self, bytes: &[u8]) -> Result<SealedBlob, WriterError> {
        if bytes.len() as u64 > MAX_BYTES {
            return Err(WriterError::BundleTooLarge);
        }
        let sha256 = digest(bytes);
        let blob = SealedBlob {
            id: sha256.clone(),
            sha256,
            bytes: bytes.len() as u64,
        };
        self.install(&blob.id, bytes)?;
        self.read(&blob)?;
        Ok(blob)
    }
    fn read(&self, blob: &SealedBlob) -> Result<Vec<u8>, WriterError> {
        if blob.id.len() != 64
            || !blob
                .id
                .bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
            || blob.id != blob.sha256
        {
            return Err(WriterError::Invalid);
        }
        if blob.bytes > MAX_BYTES {
            return Err(WriterError::BundleTooLarge);
        }
        let path = self.root.join(&blob.id);
        if regular(&path)?.len() != blob.bytes {
            return Err(WriterError::Verify);
        }
        let mut bytes = Vec::new();
        fs::File::open(path)?
            .take(MAX_BYTES + 1)
            .read_to_end(&mut bytes)?;
        if bytes.len() as u64 != blob.bytes || digest(&bytes) != blob.sha256 {
            return Err(WriterError::Verify);
        }
        Ok(bytes)
    }
}
#[cfg(test)]
#[path = "writer_blob_tests.rs"]
mod tests;
