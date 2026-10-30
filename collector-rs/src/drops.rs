//! Counts of hook events that were not stored, by reason. The hook appends one
//! byte per drop to an O_APPEND log, so counting never needs the SQLite store;
//! sync folds the log into the store's drops table.
use crate::{
    Result,
    store::{Store, private},
};
use rusqlite::{OptionalExtension, Transaction, TransactionBehavior, params};
use std::{
    collections::BTreeMap,
    fs::{self, File, OpenOptions},
    io::{Read, Write},
    os::unix::fs::{MetadataExt, OpenOptionsExt},
    path::Path,
};

pub(crate) const LOG: &str = "drops.log";
pub(crate) const FOLDING: &str = "drops.log.folding";
const CAP: u64 = 1 << 20;
// Append only: a reason's index is its byte in the log.
pub const REASONS: [&str; 11] = [
    "outside_scope",
    "unknown_repository",
    "spool_full",
    "permission",
    "invalid",
    "observation_conflict",
    "reflog_rotated",
    "revoked",
    "stale_plan",
    "busy",
    "error",
];

/// Best effort and silent: a drop that cannot be counted is not an error.
pub fn note(state: &Path, reason: &str) {
    let Some(index) = REASONS.iter().position(|r| *r == reason) else {
        return;
    };
    for _ in 0..2 {
        match open(state).and_then(|log| append(&log, state, b'a' + index as u8)) {
            Ok(false) => continue,
            _ => return,
        }
    }
}
fn open(state: &Path) -> Result<File> {
    private(state, true)?;
    Ok(OpenOptions::new()
        .append(true)
        .create(true)
        .mode(0o600)
        .custom_flags(rustix::fs::OFlags::NOFOLLOW.bits() as i32)
        .open(state.join(LOG))?)
}
// Sync may rename the log between open and write. A handle that no longer
// names the live log must not write: its inode may already be tallied.
fn append(log: &File, state: &Path, byte: u8) -> Result<bool> {
    log.lock_shared()?;
    let m = log.metadata()?;
    let live = fs::symlink_metadata(state.join(LOG))
        .is_ok_and(|path| path.dev() == m.dev() && path.ino() == m.ino());
    if live
        && m.uid() == rustix::process::geteuid().as_raw()
        && m.mode() & 0o077 == 0
        && m.nlink() == 1
        && m.len() < CAP
    {
        (&*log).write_all(&[byte])?;
    }
    log.unlock()?;
    Ok(live)
}
fn tally(path: &Path, counts: &mut BTreeMap<String, i64>) -> Result<()> {
    if !path.exists() {
        return Ok(());
    }
    private(path, false)?;
    let mut raw = Vec::new();
    File::open(path)?.take(CAP + 1).read_to_end(&mut raw)?;
    for byte in raw {
        if let Some(reason) = byte.checked_sub(b'a').and_then(|i| REASONS.get(i as usize)) {
            *counts.entry((*reason).to_owned()).or_default() += 1;
        }
    }
    Ok(())
}
fn inode(path: &Path) -> Result<Option<i64>> {
    match fs::symlink_metadata(path) {
        Ok(m) => Ok(Some(m.ino() as i64)),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(e) => Err(e.into()),
    }
}
fn folded(store: &Store, folding: &Path) -> Result<bool> {
    let Some(inode) = inode(folding)? else {
        return Ok(false);
    };
    let row = store
        .db
        .query_row("SELECT 1 FROM drops_folded WHERE inode=?", [inode], |_| {
            Ok(())
        })
        .optional()?;
    Ok(row.is_some())
}
pub fn counts(store: &Store, state: &Path) -> Result<BTreeMap<String, i64>> {
    let mut counts = BTreeMap::new();
    let mut statement = store.db.prepare("SELECT reason,count FROM drops")?;
    for row in statement.query_map([], |r| Ok((r.get(0)?, r.get(1)?)))? {
        let (reason, count): (String, i64) = row?;
        counts.insert(reason, count);
    }
    if !folded(store, &state.join(FOLDING))? {
        tally(&state.join(FOLDING), &mut counts)?;
    }
    tally(&state.join(LOG), &mut counts)?;
    Ok(counts)
}
// The folding file's inode is committed with its counts, so a fold retried
// after a failure or crash before the unlink never adds them twice.
fn commit(store: &Store, folding: &Path) -> Result<()> {
    let inode = inode(folding)?.ok_or("drops_log_missing")?;
    let mut counts = BTreeMap::new();
    tally(folding, &mut counts)?;
    let at = fs::symlink_metadata(folding)?.mtime();
    let tx = Transaction::new_unchecked(&store.db, TransactionBehavior::Immediate)?;
    for (reason, count) in counts {
        tx.execute(
            "INSERT INTO drops(reason,count,last_at) VALUES(?1,?2,?3) ON CONFLICT(reason) DO UPDATE SET count=count+?2,last_at=MAX(last_at,?3)",
            params![reason, count, at],
        )?;
    }
    tx.execute(
        "INSERT OR REPLACE INTO drops_folded(singleton,inode) VALUES(1,?)",
        [inode],
    )?;
    tx.commit()?;
    Ok(())
}
pub(crate) fn fold(store: &Store, state: &Path) -> Result<()> {
    let folding = state.join(FOLDING);
    if !folding.exists() {
        if !state.join(LOG).exists() {
            return Ok(());
        }
        // Cleared before the rename: a later folding file may reuse the inode.
        store.db.execute("DELETE FROM drops_folded", [])?;
        match fs::rename(state.join(LOG), &folding) {
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(()),
            other => other?,
        }
    }
    private(&folding, false)?;
    File::open(&folding)?.lock()?;
    if !folded(store, &folding)? {
        commit(store, &folding)?;
    }
    fs::remove_file(&folding)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::PermissionsExt;

    fn spool() -> (tempfile::TempDir, std::path::PathBuf, Store) {
        let dir = tempfile::tempdir().unwrap();
        fs::set_permissions(dir.path(), fs::Permissions::from_mode(0o700)).unwrap();
        let state = dir.path().join("state");
        fs::create_dir(&state).unwrap();
        fs::set_permissions(&state, fs::Permissions::from_mode(0o700)).unwrap();
        let store =
            Store::open(&state, Some(include_bytes!("../tests/fixtures/scope.json"))).unwrap();
        (dir, state, store)
    }
    fn write(path: &Path, bytes: &[u8]) {
        fs::write(path, bytes).unwrap();
        fs::set_permissions(path, fs::Permissions::from_mode(0o600)).unwrap();
    }

    #[test]
    fn a_fold_interrupted_before_unlink_is_not_counted_twice() {
        let (_dir, state, store) = spool();
        write(&state.join(FOLDING), b"aab");
        commit(&store, &state.join(FOLDING)).unwrap();
        fold(&store, &state).unwrap();
        assert!(!state.join(FOLDING).exists());
        let expected = BTreeMap::from([
            ("outside_scope".into(), 2),
            ("unknown_repository".into(), 1),
        ]);
        assert_eq!(counts(&store, &state).unwrap(), expected);
    }

    #[test]
    fn a_new_folding_log_is_counted_after_an_earlier_completed_fold() {
        let (_dir, state, store) = spool();
        write(&state.join(LOG), b"a");
        fold(&store, &state).unwrap();
        write(&state.join(LOG), b"a");
        fold(&store, &state).unwrap();
        assert_eq!(
            counts(&store, &state).unwrap(),
            BTreeMap::from([("outside_scope".into(), 2)])
        );
    }

    #[test]
    fn last_at_is_the_time_of_the_last_drop_not_of_the_fold() {
        let (_dir, state, store) = spool();
        write(&state.join(LOG), b"a");
        let dropped = std::time::UNIX_EPOCH + std::time::Duration::from_secs(1_700_000_000);
        File::options()
            .write(true)
            .open(state.join(LOG))
            .unwrap()
            .set_modified(dropped)
            .unwrap();
        fold(&store, &state).unwrap();
        let last_at: i64 = store
            .db
            .query_row("SELECT last_at FROM drops", [], |r| r.get(0))
            .unwrap();
        assert_eq!(last_at, 1_700_000_000);
    }

    #[test]
    fn a_handle_opened_before_the_fold_rename_does_not_write_into_the_folding_log() {
        let (_dir, state, store) = spool();
        let stale = open(&state).unwrap();
        fs::rename(state.join(LOG), state.join(FOLDING)).unwrap();
        assert!(!append(&stale, &state, b'a').unwrap());
        fold(&store, &state).unwrap();
        note(&state, "outside_scope");
        assert_eq!(
            counts(&store, &state).unwrap(),
            BTreeMap::from([("outside_scope".into(), 1)])
        );
    }
}
