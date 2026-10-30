//! Counts of hook events that were not stored, by reason. The hook appends one
//! byte per drop to an O_APPEND log, so counting never needs the SQLite store;
//! sync folds the log into the store's drops table.
use crate::{
    Result,
    store::{Store, now, private},
};
use rusqlite::params;
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
    let _ = (|| -> Result<()> {
        private(state, true)?;
        let mut log = OpenOptions::new()
            .append(true)
            .create(true)
            .mode(0o600)
            .custom_flags(rustix::fs::OFlags::NOFOLLOW.bits() as i32)
            .open(state.join(LOG))?;
        let m = log.metadata()?;
        if m.uid() == rustix::process::geteuid().as_raw()
            && m.mode() & 0o077 == 0
            && m.nlink() == 1
            && m.len() < CAP
        {
            log.write_all(&[b'a' + index as u8])?;
        }
        Ok(())
    })();
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
pub fn counts(store: &Store, state: &Path) -> Result<BTreeMap<String, i64>> {
    let mut counts = BTreeMap::new();
    let mut statement = store.db.prepare("SELECT reason,count FROM drops")?;
    for row in statement.query_map([], |r| Ok((r.get(0)?, r.get(1)?)))? {
        let (reason, count): (String, i64) = row?;
        counts.insert(reason, count);
    }
    tally(&state.join(FOLDING), &mut counts)?;
    tally(&state.join(LOG), &mut counts)?;
    Ok(counts)
}
pub(crate) fn fold(store: &Store, state: &Path) -> Result<()> {
    let folding = state.join(FOLDING);
    if !folding.exists() {
        match fs::rename(state.join(LOG), &folding) {
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(()),
            other => other?,
        }
    }
    let mut counts = BTreeMap::new();
    tally(&folding, &mut counts)?;
    let at = now()?;
    for (reason, count) in counts {
        store.db.execute(
            "INSERT INTO drops(reason,count,last_at) VALUES(?1,?2,?3) ON CONFLICT(reason) DO UPDATE SET count=count+?2,last_at=?3",
            params![reason, count, at],
        )?;
    }
    fs::remove_file(&folding)?;
    Ok(())
}
