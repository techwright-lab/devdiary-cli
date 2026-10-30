use crate::{MAX_WIRE, Object, Result, drops, endpoint, freeze, receipt, reflog::Mark, scope};
use rusqlite::{Connection, OptionalExtension, Transaction, TransactionBehavior, params};
use sha2::{Digest, Sha256};
use std::{
    fs::{self, File, OpenOptions},
    io::Read,
    os::unix::fs::{MetadataExt, OpenOptionsExt},
    path::{Component, Path},
    time::{Duration, SystemTime, UNIX_EPOCH},
};
const DB: &str = "collector-rust-v1.sqlite3";
const APP_ID: i64 = 1145328177;
const VERSION: i64 = 2;
pub(crate) const SETTINGS_LOCK: &str = "claude-settings.lock";
pub(crate) const FINGERPRINT: &str = "executable-fingerprint";
const REFS: &str = "repository-refs.json";
const REFS_NEXT: &str = "repository-refs.json.next";
const MAX_REFS: u64 = 262144;
const SCOPE_PATH: &str = "/ingest/v1/collector/scope";
// Delivered IDs are kept so a late duplicate hook firing is still deduplicated
// instead of being resent as a conflicting observation.
const TOMBSTONES: i64 = 4000;
const SCHEMA: &str = "
    CREATE TABLE scope (singleton INTEGER PRIMARY KEY CHECK(singleton=1), json BLOB NOT NULL, cursor INTEGER NOT NULL DEFAULT 0, delivered INTEGER NOT NULL DEFAULT 0, last_receipt BLOB, retry_after INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE outbox (seq INTEGER PRIMARY KEY, observation_id TEXT UNIQUE NOT NULL, payload BLOB NOT NULL, failure TEXT);";
const ADDED_TABLES: &str = "
    CREATE TABLE delivered (observation_id TEXT PRIMARY KEY, digest BLOB NOT NULL, at INTEGER NOT NULL) WITHOUT ROWID;
    CREATE TABLE drops (reason TEXT PRIMARY KEY, count INTEGER NOT NULL, last_at INTEGER NOT NULL) WITHOUT ROWID;";
const FOLD_TABLE: &str = "CREATE TABLE IF NOT EXISTS drops_folded (singleton INTEGER PRIMARY KEY CHECK(singleton=1), inode INTEGER NOT NULL);";
const MARKS_TABLE: &str = "CREATE TABLE IF NOT EXISTS bracket_marks (session_id TEXT NOT NULL, tool_use_id TEXT NOT NULL, checkout_key TEXT NOT NULL, offset INTEGER NOT NULL, ino INTEGER NOT NULL, opened_at INTEGER NOT NULL, closed_at INTEGER, PRIMARY KEY(session_id, tool_use_id)) WITHOUT ROWID;";
const MARK_TTL_NANOS: i64 = 6 * 3_600_000_000_000;
const MAX_MARKS: i64 = 256;
// Claude Code's Bash tool cannot run longer than this, so an unclosed mark
// older than it is a denied or interrupted call, not one still running.
const MAX_BASH_NANOS: i64 = 600_000_000_000;

fn trusted_ancestors(path: &Path) -> Result<()> {
    if !path.is_absolute() || path.components().any(|c| matches!(c, Component::ParentDir)) {
        return Err("absolute_private_path_required".into());
    }
    for p in path.ancestors() {
        let metadata = fs::symlink_metadata(p)?;
        if metadata.file_type().is_symlink() {
            return Err("symlink_path".into());
        }
        if p != path
            && ((metadata.uid() != 0 && metadata.uid() != rustix::process::geteuid().as_raw())
                || (metadata.mode() & 0o022 != 0 && metadata.mode() & 0o1000 == 0))
        {
            return Err("unsafe_ancestor".into());
        }
    }
    Ok(())
}
fn owned_by_us(path: &Path, directory: bool, forbidden_mode: u32) -> std::io::Result<bool> {
    let m = fs::metadata(path)?;
    if m.nlink() == 0 {
        return Err(std::io::ErrorKind::NotFound.into());
    }
    Ok(m.uid() == rustix::process::geteuid().as_raw()
        && m.mode() & forbidden_mode == 0
        && if directory {
            m.is_dir()
        } else {
            m.is_file() && m.nlink() == 1
        })
}
// The same UID is trusted, as in the Python reference. Never follow symlink
// ancestors; reject shared permissions/hardlinks rather than repairing them.
pub fn private(path: &Path, directory: bool) -> Result<()> {
    trusted_ancestors(path)?;
    if !owned_by_us(path, directory, 0o077)? {
        return Err("private_state_required".into());
    }
    Ok(())
}
// Customer settings stay readable by others (0644 under 0755); only writes by
// anyone but the owner, hardlinks and symlinks are refused.
pub fn owner_writable(path: &Path, directory: bool) -> Result<()> {
    trusted_ancestors(path)?;
    if !owned_by_us(path, directory, 0o022)? {
        return Err("unsafe_settings".into());
    }
    Ok(())
}
pub(crate) fn create_private(path: &Path) -> Result<File> {
    match OpenOptions::new()
        .read(true)
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(path)
    {
        Ok(f) => {
            f.sync_all()?;
            File::open(path.parent().ok_or("invalid_path")?)?.sync_all()?;
            Ok(f)
        }
        Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => {
            private(path, false)?;
            Ok(OpenOptions::new().read(true).write(true).open(path)?)
        }
        Err(e) => Err(e.into()),
    }
}
pub(crate) fn now() -> Result<i64> {
    Ok(SystemTime::now().duration_since(UNIX_EPOCH)?.as_secs() as i64)
}
fn state_check(state: &Path) -> Result<()> {
    private(state, true)?;
    let persistent = [DB, "sync.lock", SETTINGS_LOCK];
    for entry in fs::read_dir(state)? {
        let e = entry?;
        let name = e.file_name();
        if ![
            DB,
            "collector-rust-v1.sqlite3-journal",
            "collector-rust-v1.sqlite3-wal",
            "collector-rust-v1.sqlite3-shm",
            "sync.lock",
            SETTINGS_LOCK,
            FINGERPRINT,
            REFS,
            REFS_NEXT,
            drops::LOG,
            drops::FOLDING,
        ]
        .iter()
        .any(|n| name == *n)
        {
            return Err("separate_empty_rust_state_required".into());
        }
        if let Err(error) = private(&e.path(), false)
            && !vanished(&e.path(), &name, &persistent)
        {
            return Err(error);
        }
    }
    Ok(())
}
// Journals, the fingerprint and drop logs can be unlinked between readdir and
// stat. Only a missing transient file is harmless; never ignore ownership,
// symlink, permissions or a missing database/lock.
fn vanished(path: &Path, name: &std::ffi::OsStr, persistent: &[&str]) -> bool {
    !persistent.iter().any(|n| name == *n)
        && fs::symlink_metadata(path).is_err_and(|e| e.kind() == std::io::ErrorKind::NotFound)
}
fn connect(path: &Path, page_limit: bool) -> Result<Connection> {
    let db = Connection::open(path)?;
    db.busy_timeout(Duration::from_millis(250))?;
    db.execute_batch("PRAGMA synchronous=FULL; PRAGMA journal_mode=DELETE;")?;
    if page_limit {
        db.execute_batch("PRAGMA max_page_count=4096;")?;
    }
    let page_size: i64 = db.query_row("PRAGMA page_size", [], |r| r.get(0))?;
    if page_size != 4096 {
        return Err("incompatible_spool".into());
    }
    Ok(db)
}
fn pragma(db: &Connection, name: &str) -> Result<i64> {
    Ok(db.query_row(&format!("PRAGMA {name}"), [], |r| r.get(0))?)
}
fn digest(payload: &[u8]) -> Vec<u8> {
    Sha256::digest(payload).to_vec()
}

pub struct Store {
    pub db: Connection,
    pub config: Object,
}
/// One Bash tool call in one checkout. Local only: never spooled or sent.
pub struct Bracket<'a> {
    pub session_id: &'a str,
    pub tool_use_id: &'a str,
    pub checkout_key: &'a str,
}
impl Store {
    pub fn open(state: &Path, initial: Option<&[u8]>) -> Result<Self> {
        state_check(state)?;
        let path = state.join(DB);
        if let Some(raw) = initial {
            scope(raw)?;
            create_private(&path)?;
        } else {
            private(&path, false)?;
        }
        if fs::metadata(&path)?.len() > 16 * 1024 * 1024 {
            return Err("spool_full".into());
        }
        let mut db = connect(&path, true)?;
        // Only an empty file accepts this; it must precede the first table.
        if initial.is_some() && fs::metadata(&path)?.len() == 0 {
            db.execute_batch("PRAGMA auto_vacuum=INCREMENTAL;")?;
        }
        let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
        let id = pragma(&tx, "application_id")?;
        let version = pragma(&tx, "user_version")?;
        if id == 0 && version == 0 && initial.is_some() {
            create(&tx, initial.ok_or("scope_required")?)?;
        } else if id != APP_ID || !(1..=VERSION).contains(&version) {
            return Err("incompatible_spool".into());
        } else if version != VERSION || pragma(&tx, "auto_vacuum")? != 2 {
            return Err("repair_required".into());
        }
        let saved: Vec<u8> =
            tx.query_row("SELECT json FROM scope WHERE singleton=1", [], |r| r.get(0))?;
        let config = scope(&saved)?;
        if let Some(raw) = initial
            && config != scope(raw)?
        {
            return Err("frozen_scope_conflict".into());
        }
        tx.execute_batch(FOLD_TABLE)?;
        tx.execute_batch(MARKS_TABLE)?;
        tx.commit()?;
        File::open(state)?.sync_all()?;
        Ok(Self { db, config })
    }
    pub fn collect(&mut self, raw: &[u8]) -> Result<()> {
        let payload = freeze(raw, &self.config)?;
        let parsed = crate::object(&payload)?;
        let id = parsed["observation_id"].as_str().ok_or("invalid_id")?;
        let tx = self
            .db
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        let pending: Option<Vec<u8>> = tx
            .query_row(
                "SELECT payload FROM outbox WHERE observation_id=?",
                [id],
                |r| r.get(0),
            )
            .optional()?;
        let delivered: Option<Vec<u8>> = tx
            .query_row(
                "SELECT digest FROM delivered WHERE observation_id=?",
                [id],
                |r| r.get(0),
            )
            .optional()?;
        if let Some(old) = pending.map(|p| digest(&p)).or(delivered) {
            if old != digest(&payload) {
                return Err("observation_conflict".into());
            }
        } else {
            let count: i64 = tx.query_row("SELECT count(*) FROM outbox", [], |r| r.get(0))?;
            if count >= 10000 {
                return Err("spool_full".into());
            }
            tx.execute(
                "INSERT INTO outbox(observation_id,payload) VALUES(?,?)",
                params![id, payload],
            )?;
            // Worst case per pending row: a leaf and an interior page for the table
            // and its unique index, plus overflow with 1024 bytes of ID/header/failure
            // slack. 256 fixed pages cover schema, scope, drops, capped tombstones and
            // capped bracket marks.
            let reserved: i64 = tx.query_row(
                "SELECT 256 + coalesce(sum(4 + (length(payload)+1024+4091)/4092),0) FROM outbox",
                [],
                |r| r.get(0),
            )?;
            if reserved > 4096 {
                return Err("spool_full".into());
            }
        }
        tx.commit()?;
        Ok(())
    }
    // A repeated PreToolUse keeps the first offset, so the bracket never shrinks.
    pub fn open_bracket(&self, bracket: &Bracket, mark: Mark, at: i64) -> Result<()> {
        let tx = self.db.unchecked_transaction()?;
        prune_marks(&tx, at)?;
        tx.execute(
            "INSERT OR IGNORE INTO bracket_marks(session_id,tool_use_id,checkout_key,offset,ino,opened_at) VALUES(?,?,?,?,?,?)",
            params![
                bracket.session_id,
                bracket.tool_use_id,
                bracket.checkout_key,
                mark.offset as i64,
                mark.ino as i64,
                at
            ],
        )?;
        tx.execute(
            "DELETE FROM bracket_marks WHERE (session_id,tool_use_id) IN (SELECT session_id,tool_use_id FROM bracket_marks m ORDER BY (closed_at IS NULL OR EXISTS (SELECT 1 FROM bracket_marks o WHERE o.checkout_key=m.checkout_key AND o.closed_at IS NULL AND o.opened_at<=m.closed_at)) DESC, opened_at DESC LIMIT -1 OFFSET ?)",
            [MAX_MARKS],
        )?;
        tx.commit()?;
        Ok(())
    }
    /// Closed marks stay until pruned so a bracket that closes later still
    /// counts this one as overlapping. Only other sessions are overlap: one
    /// session's parallel calls are a single claim. Capped at the wire maximum.
    pub fn close_bracket(&self, bracket: &Bracket, at: i64) -> Result<Option<(Mark, u64)>> {
        let tx = self.db.unchecked_transaction()?;
        prune_marks(&tx, at)?;
        let open: Option<(String, i64, i64, i64)> = tx
            .query_row(
                "SELECT checkout_key,offset,ino,opened_at FROM bracket_marks WHERE session_id=? AND tool_use_id=? AND closed_at IS NULL",
                params![bracket.session_id, bracket.tool_use_id],
                |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)),
            )
            .optional()?;
        tx.execute(
            "UPDATE bracket_marks SET closed_at=? WHERE session_id=? AND tool_use_id=?",
            params![at, bracket.session_id, bracket.tool_use_id],
        )?;
        let result = match open {
            Some((key, offset, ino, opened_at)) if key == bracket.checkout_key => {
                let overlap: i64 = tx.query_row(
                    "SELECT count(*) FROM bracket_marks WHERE checkout_key=? AND session_id<>? AND opened_at<=? AND (closed_at>=? OR (closed_at IS NULL AND opened_at>=?))",
                    params![key, bracket.session_id, at, opened_at, opened_at - MAX_BASH_NANOS],
                    |r| r.get(0),
                )?;
                let mark = Mark {
                    offset: offset as u64,
                    ino: ino as u64,
                };
                Some((mark, overlap.min(64) as u64))
            }
            _ => None,
        };
        tx.commit()?;
        Ok(result)
    }
    pub fn counts(&self) -> Result<(i64, i64)> {
        Ok(self.db.query_row(
            "SELECT (SELECT count(*) FROM outbox), delivered FROM scope WHERE singleton=1",
            [],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )?)
    }
    pub fn last_receipt(&self) -> Result<Option<Vec<u8>>> {
        Ok(self
            .db
            .query_row("SELECT last_receipt FROM scope", [], |r| r.get(0))?)
    }
    fn batch(&self, limit: usize) -> Result<Vec<(i64, Vec<u8>)>> {
        let cursor: i64 = self
            .db
            .query_row("SELECT cursor FROM scope", [], |r| r.get(0))?;
        let mut result = Vec::new();
        for comparison in [">", "<="] {
            let mut statement = self.db.prepare(&format!(
                "SELECT seq,payload FROM outbox WHERE seq {comparison} ? ORDER BY seq LIMIT ?"
            ))?;
            let rows = statement
                .query_map(params![cursor, (limit - result.len()) as i64], |r| {
                    Ok((r.get(0)?, r.get(1)?))
                })?;
            for row in rows {
                result.push(row?);
            }
        }
        Ok(result)
    }
    // The row, its tombstone and the counter change together, so a crash
    // either keeps the row pending or records it delivered, never neither.
    fn deliver(&mut self, seq: i64, payload: &[u8], receipt: &[u8]) -> Result<()> {
        let tx = self
            .db
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        tx.execute(
            "INSERT OR REPLACE INTO delivered(observation_id,digest,at) SELECT observation_id,?,? FROM outbox WHERE seq=?",
            params![digest(payload), now()?, seq],
        )?;
        tx.execute("DELETE FROM outbox WHERE seq=?", [seq])?;
        tx.execute(
            "UPDATE scope SET delivered=delivered+1,last_receipt=? WHERE singleton=1",
            [receipt],
        )?;
        tx.commit()?;
        Ok(())
    }
    fn compact(&self) -> Result<()> {
        self.db.execute(
            "DELETE FROM delivered WHERE observation_id IN (SELECT observation_id FROM delivered ORDER BY at DESC, observation_id LIMIT -1 OFFSET ?)",
            [TOMBSTONES],
        )?;
        self.db.execute_batch("PRAGMA incremental_vacuum;")?;
        Ok(())
    }
}
fn prune_marks(tx: &Transaction, at: i64) -> Result<()> {
    tx.execute(
        "DELETE FROM bracket_marks WHERE opened_at<?",
        [at - MARK_TTL_NANOS],
    )?;
    Ok(())
}
fn create(tx: &Transaction, initial: &[u8]) -> Result<()> {
    let tables: i64 = tx.query_row("SELECT count(*) FROM sqlite_master", [], |r| r.get(0))?;
    if tables != 0 {
        return Err("foreign_spool".into());
    }
    tx.execute_batch(&format!(
        "PRAGMA application_id={APP_ID}; PRAGMA user_version={VERSION}; {SCHEMA} {ADDED_TABLES}"
    ))?;
    tx.execute(
        "INSERT INTO scope(singleton,json) VALUES(1,?)",
        [serde_json::to_vec(&scope(initial)?)?],
    )?;
    Ok(())
}

/// Migrates a version 1 spool in place: delivered rows become tombstones and a
/// counter, pending rows keep their exact bytes. Safe to re-run after a crash.
pub fn repair(state: &Path) -> Result<()> {
    state_check(state)?;
    let lock = create_private(&state.join("sync.lock"))?;
    lock.try_lock()?;
    let path = state.join(DB);
    private(&path, false)?;
    let mut db = connect(&path, false)?;
    if pragma(&db, "application_id")? != APP_ID {
        return Err("incompatible_spool".into());
    }
    match pragma(&db, "user_version")? {
        1 => migrate(&mut db)?,
        VERSION => {}
        _ => return Err("incompatible_spool".into()),
    }
    if pragma(&db, "auto_vacuum")? != 2 {
        db.execute_batch("PRAGMA auto_vacuum=INCREMENTAL; VACUUM;")?;
    }
    Ok(())
}
fn migrate(db: &mut Connection) -> Result<()> {
    let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
    tx.execute_batch(&format!(
        "ALTER TABLE scope ADD COLUMN delivered INTEGER NOT NULL DEFAULT 0;
         ALTER TABLE scope ADD COLUMN last_receipt BLOB;
         ALTER TABLE scope ADD COLUMN retry_after INTEGER NOT NULL DEFAULT 0;
         {ADDED_TABLES}"
    ))?;
    let delivered: Vec<(String, Vec<u8>)> = tx
        .prepare(
            "SELECT observation_id,payload FROM outbox WHERE delivered=1 ORDER BY seq DESC LIMIT ?",
        )?
        .query_map([TOMBSTONES], |r| Ok((r.get(0)?, r.get(1)?)))?
        .collect::<rusqlite::Result<_>>()?;
    let at = now()?;
    for (id, payload) in delivered {
        tx.execute(
            "INSERT INTO delivered(observation_id,digest,at) VALUES(?,?,?)",
            params![id, digest(&payload), at],
        )?;
    }
    tx.execute_batch(&format!(
        "UPDATE scope SET delivered=(SELECT count(*) FROM outbox WHERE delivered=1),
            last_receipt=(SELECT receipt FROM outbox WHERE delivered=1 ORDER BY seq DESC LIMIT 1);
         DELETE FROM outbox WHERE delivered=1;
         DROP INDEX pending;
         ALTER TABLE outbox DROP COLUMN delivered;
         ALTER TABLE outbox DROP COLUMN receipt;
         PRAGMA user_version={VERSION};"
    ))?;
    tx.commit()?;
    Ok(())
}

pub fn repository_refs(value: &serde_json::Value) -> Result<Vec<String>> {
    let refs: Vec<String> = value
        .as_array()
        .ok_or("invalid_repository_refs")?
        .iter()
        .map(|r| {
            r.as_str()
                .filter(|r| crate::repository_ref(r))
                .map(str::to_owned)
                .ok_or("invalid_repository_refs")
        })
        .collect::<std::result::Result<_, _>>()?;
    let mut seen = std::collections::HashSet::new();
    Ok(refs
        .into_iter()
        .filter(|r| seen.insert(r.to_ascii_lowercase()))
        .collect())
}
pub(crate) fn read_refs(state: &Path) -> Result<Vec<String>> {
    let path = state.join(REFS);
    private(&path, false)?;
    let mut raw = Vec::new();
    File::open(path)?.take(MAX_REFS + 1).read_to_end(&mut raw)?;
    if raw.len() as u64 > MAX_REFS {
        return Err("invalid_repository_refs".into());
    }
    repository_refs(&serde_json::from_slice(&raw)?)
}
pub(crate) fn has_refs(state: &Path) -> bool {
    state.join(REFS).exists()
}
// Callers hold sync.lock, so the fixed staging name has one writer.
fn replace_refs(state: &Path, refs: &[String]) -> Result<()> {
    let raw = serde_json::to_vec(refs)?;
    if raw.len() as u64 > MAX_REFS {
        return Err("invalid_repository_refs".into());
    }
    let next = state.join(REFS_NEXT);
    match fs::remove_file(&next) {
        Err(e) if e.kind() != std::io::ErrorKind::NotFound => return Err(e.into()),
        _ => {}
    }
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .custom_flags(rustix::fs::OFlags::NOFOLLOW.bits() as i32)
        .open(&next)?;
    std::io::Write::write_all(&mut file, &raw)?;
    file.sync_all()?;
    fs::rename(&next, state.join(REFS))?;
    File::open(state)?.sync_all()?;
    Ok(())
}
pub(crate) fn seed_refs(state: &Path, refs: &[String]) -> Result<()> {
    let lock = create_private(&state.join("sync.lock"))?;
    lock.lock()?;
    if has_refs(state) {
        return Ok(());
    }
    replace_refs(state, refs)
}

fn credential(path: &Path) -> Result<String> {
    private(path, false)?;
    let mut raw = String::new();
    File::open(path)?.take(4097).read_to_string(&mut raw)?;
    if raw.len() > 4096 {
        return Err("invalid_credential".into());
    }
    let key = raw.trim();
    if !key.strip_prefix("dc_live_").is_some_and(|s| {
        !s.is_empty()
            && s.bytes()
                .all(|b| b.is_ascii_alphanumeric() || b"_-".contains(&b))
    }) {
        return Err("invalid_credential".into());
    }
    Ok(key.to_string())
}

pub fn sync(state: &Path, key_path: &Path, limit: usize) -> Result<(i64, i64)> {
    // Compatibility entrypoint for existing manually provisioned credentials.
    if key_path.starts_with(state) {
        return Err("separate_credential_required".into());
    }
    sync_key(state, &credential(key_path)?, limit, None)
}
pub(crate) fn sync_key(
    state: &Path,
    key: &str,
    limit: usize,
    revoked: Option<&Path>,
) -> Result<(i64, i64)> {
    if !(1..=100).contains(&limit) {
        return Err("invalid_limit".into());
    }
    state_check(state)?;
    let lock = create_private(&state.join("sync.lock"))?;
    lock.try_lock()?;
    let mut store = Store::open(state, None)?;
    drops::fold(&store, state)?;
    if revoked.is_some_and(Path::exists) {
        return Err("revoked".into());
    }
    let retry_after: i64 = store
        .db
        .query_row("SELECT retry_after FROM scope", [], |r| r.get(0))?;
    if retry_after > now()? {
        return Err("sync_rate_limited".into());
    }
    crate::claude::rehash(state)?;
    let url = store.config["endpoint"]
        .as_str()
        .ok_or("invalid_endpoint")?
        .to_owned();
    endpoint(&url)?;
    let collector = store.config["collector_ref"]
        .as_str()
        .ok_or("invalid_collector")?
        .to_owned();
    let client = reqwest::blocking::Client::builder()
        .no_proxy()
        .redirect(reqwest::redirect::Policy::none())
        .connect_timeout(Duration::from_secs(4))
        .timeout(Duration::from_secs(5))
        .build()?;
    if crate::installation(&store.config) {
        refresh_refs(&client, &store, state, key, revoked)?;
    }
    for (seq, payload) in store.batch(limit)? {
        // Autocommit FULL before any HTTP. Lost response or process death cannot
        // erase frozen bytes or pin the next invocation to a poison row.
        store
            .db
            .execute("UPDATE scope SET cursor=? WHERE singleton=1", [seq])?;
        match post(&client, &url, key, &payload, &collector) {
            Ok(r) => store.deliver(seq, &payload, &serde_json::to_vec(&r)?)?,
            Err(failure) => {
                store.db.execute(
                    "UPDATE outbox SET failure=? WHERE seq=?",
                    params![failure.code, seq],
                )?;
                if let Some(seconds) = failure.retry_after {
                    store.db.execute(
                        "UPDATE scope SET retry_after=? WHERE singleton=1",
                        [now()? + seconds],
                    )?;
                }
                if failure.revoked
                    && let Some(marker) = revoked
                {
                    create_private(marker)?;
                }
                if failure.stop {
                    break;
                }
            }
        }
    }
    store.compact()?;
    store.counts()
}
// A refresh that fails for any reason but revocation or a server-requested
// wait keeps the last good list; delivery of spooled rows still proceeds.
fn refresh_refs(
    client: &reqwest::blocking::Client,
    store: &Store,
    state: &Path,
    key: &str,
    revoked: Option<&Path>,
) -> Result<()> {
    let mut url = reqwest::Url::parse(
        store.config["endpoint"]
            .as_str()
            .ok_or("invalid_endpoint")?,
    )?;
    url.set_path(SCOPE_PATH);
    let Ok(response) = client
        .get(url)
        .bearer_auth(key)
        .header("Accept", "application/json")
        .send()
    else {
        return Ok(());
    };
    let status = response.status().as_u16();
    if matches!(status, 401 | 403) {
        if let Some(marker) = revoked {
            create_private(marker)?;
        }
        return Err("revoked".into());
    }
    if status == 429 {
        let wait = response
            .headers()
            .get("Retry-After")
            .and_then(|v| v.to_str().ok())
            .and_then(|v| retry_after(v, now().ok()?))
            .unwrap_or(60);
        store.db.execute(
            "UPDATE scope SET retry_after=? WHERE singleton=1",
            [now()? + wait],
        )?;
        return Err("sync_rate_limited".into());
    }
    let mut raw = Vec::new();
    if status != 200
        || response.take(MAX_REFS + 1).read_to_end(&mut raw).is_err()
        || raw.len() as u64 > MAX_REFS
    {
        return Ok(());
    }
    if let Ok(refs) = scope_refs(&raw, &store.config) {
        replace_refs(state, &refs)?;
    }
    Ok(())
}
fn scope_refs(raw: &[u8], config: &Object) -> Result<Vec<String>> {
    serde_json::from_slice::<crate::claude::UniqueKeys>(raw)?;
    let doc = crate::object(raw)?;
    let keys = [
        "schema_version",
        "collector_ref",
        "scope_kind",
        "repository_refs",
        "observation_schema_versions",
    ];
    if doc.len() != keys.len()
        || keys.iter().any(|k| !doc.contains_key(*k))
        || doc["schema_version"] != serde_json::json!(1)
        || doc.get("collector_ref") != config.get("collector_ref")
        || doc["scope_kind"] != serde_json::json!("installation")
    {
        return Err("invalid_scope_response".into());
    }
    repository_refs(&doc["repository_refs"])
}
const MAX_RETRY_AFTER: i64 = 86400;
fn retry_after(value: &str, now: i64) -> Option<i64> {
    let value = value.trim();
    let seconds = if !value.is_empty() && value.bytes().all(|b| b.is_ascii_digit()) {
        value.parse::<i64>().unwrap_or(MAX_RETRY_AFTER)
    } else {
        let until = chrono::DateTime::parse_from_rfc2822(value)
            .ok()?
            .timestamp();
        until.saturating_sub(now).max(0)
    };
    Some(seconds.min(MAX_RETRY_AFTER))
}
struct Failure {
    code: String,
    stop: bool,
    revoked: bool,
    retry_after: Option<i64>,
}
impl Failure {
    fn network() -> Self {
        Self {
            code: "network_failure".into(),
            stop: true,
            revoked: false,
            retry_after: None,
        }
    }
}
fn post(
    client: &reqwest::blocking::Client,
    url: &str,
    key: &str,
    payload: &[u8],
    collector: &str,
) -> std::result::Result<Object, Failure> {
    let response = client
        .post(url)
        .bearer_auth(key)
        .header("Content-Type", "application/json")
        .header("Accept", "application/json")
        .body(payload.to_vec())
        .send()
        .map_err(|_| Failure::network())?;
    let status = response.status().as_u16();
    if !matches!(status, 200 | 201) {
        let retry_after = response
            .headers()
            .get("Retry-After")
            .and_then(|v| v.to_str().ok())
            .filter(|_| status == 429)
            .and_then(|v| retry_after(v, now().ok()?));
        return Err(Failure {
            code: format!("http_{status}"),
            stop: matches!(status, 401 | 403 | 429 | 500..=599),
            revoked: matches!(status, 401 | 403),
            retry_after,
        });
    }
    let mut raw = Vec::new();
    response
        .take((MAX_WIRE + 1) as u64)
        .read_to_end(&mut raw)
        .map_err(|_| Failure::network())?;
    receipt(&raw, payload, collector).map_err(|_| Failure {
        code: "invalid_receipt".into(),
        stop: false,
        revoked: false,
        retry_after: None,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::reflog::Mark;
    use std::ffi::OsStr;

    const HOUR: i64 = 3_600_000_000_000;
    fn store() -> (tempfile::TempDir, Store) {
        let dir = tempfile::tempdir().unwrap();
        let state = dir.path().join("state");
        fs::create_dir(&state).unwrap();
        fs::set_permissions(&state, std::os::unix::fs::PermissionsExt::from_mode(0o700)).unwrap();
        let scope = include_bytes!("../tests/fixtures/installation-scope.json");
        let store = Store::open(&state, Some(scope)).unwrap();
        (dir, store)
    }
    fn bracket<'a>(session: &'a str, tool: &'a str, key: &'a str) -> Bracket<'a> {
        Bracket {
            session_id: session,
            tool_use_id: tool,
            checkout_key: key,
        }
    }
    const MARK: Mark = Mark { offset: 7, ino: 9 };

    #[test]
    fn only_a_missing_transient_file_counts_as_vanished() {
        let dir = std::env::temp_dir().join(format!("vanished-{}", std::process::id()));
        fs::create_dir_all(&dir).unwrap();
        let held = dir.join("held-journal");
        fs::write(&held, b"x").unwrap();
        let persistent = ["db"];
        assert!(vanished(
            &dir.join("gone-journal"),
            OsStr::new("gone-journal"),
            &persistent
        ));
        assert!(!vanished(&held, OsStr::new("held-journal"), &persistent));
        assert!(!vanished(&dir.join("db"), OsStr::new("db"), &persistent));
        fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn a_closed_bracket_returns_its_mark_once() {
        let (_dir, store) = store();
        let own = bracket("s", "t", "k");
        store.open_bracket(&own, MARK, HOUR).unwrap();
        store
            .open_bracket(&own, Mark { offset: 99, ino: 9 }, HOUR + 1)
            .unwrap();
        assert_eq!(
            store.close_bracket(&own, HOUR + 2).unwrap(),
            Some((MARK, 0))
        );
        assert_eq!(store.close_bracket(&own, HOUR + 3).unwrap(), None);
        assert_eq!(
            store
                .close_bracket(&bracket("s", "never", "k"), HOUR)
                .unwrap(),
            None
        );
    }

    #[test]
    fn a_post_in_another_checkout_closes_without_a_mark() {
        let (_dir, store) = store();
        store
            .open_bracket(&bracket("s", "t", "k"), MARK, HOUR)
            .unwrap();
        assert_eq!(
            store
                .close_bracket(&bracket("s", "t", "other"), HOUR + 1)
                .unwrap(),
            None
        );
        assert_eq!(
            store
                .close_bracket(&bracket("s", "t", "k"), HOUR + 2)
                .unwrap(),
            None
        );
    }

    #[test]
    fn overlap_counts_other_brackets_on_the_same_checkout_that_shared_time() {
        let (_dir, store) = store();
        let a = bracket("a", "t", "k");
        let b = bracket("b", "t", "k");
        store.open_bracket(&a, MARK, 10).unwrap();
        store.open_bracket(&b, MARK, 20).unwrap();
        store
            .open_bracket(&bracket("c", "t", "sibling"), MARK, 15)
            .unwrap();
        store
            .open_bracket(&bracket("d", "t", "k"), MARK, 1)
            .unwrap();
        store.close_bracket(&bracket("d", "t", "k"), 5).unwrap();
        assert_eq!(store.close_bracket(&a, 30).unwrap(), Some((MARK, 1)));
        assert_eq!(store.close_bracket(&b, 40).unwrap(), Some((MARK, 1)));
        store
            .open_bracket(&bracket("e", "t", "k"), MARK, 50)
            .unwrap();
        assert_eq!(
            store.close_bracket(&bracket("e", "t", "k"), 60).unwrap(),
            Some((MARK, 0))
        );
    }

    #[test]
    fn marks_older_than_six_hours_are_pruned_without_a_result() {
        let (_dir, store) = store();
        store
            .open_bracket(&bracket("s", "lost", "k"), MARK, HOUR)
            .unwrap();
        store
            .open_bracket(&bracket("s", "next", "k"), MARK, 7 * HOUR + 1)
            .unwrap();
        let marks: i64 = store
            .db
            .query_row("SELECT count(*) FROM bracket_marks", [], |r| r.get(0))
            .unwrap();
        assert_eq!(marks, 1);
        assert_eq!(
            store
                .close_bracket(&bracket("s", "lost", "k"), 7 * HOUR + 2)
                .unwrap(),
            None
        );
    }

    #[test]
    fn open_marks_are_capped() {
        let (_dir, store) = store();
        for i in 0..300 {
            store
                .open_bracket(&bracket("s", &format!("t{i}"), "k"), MARK, HOUR + i)
                .unwrap();
        }
        let marks: i64 = store
            .db
            .query_row("SELECT count(*) FROM bracket_marks", [], |r| r.get(0))
            .unwrap();
        assert_eq!(marks, 256);
        assert_eq!(
            store
                .close_bracket(&bracket("s", "t0", "k"), 2 * HOUR)
                .unwrap(),
            None
        );
    }

    #[test]
    fn an_open_bracket_outlives_the_mark_cap() {
        let (_dir, store) = store();
        let long = bracket("long", "t", "k");
        store.open_bracket(&long, MARK, HOUR).unwrap();
        let inside = bracket("inside", "t", "k");
        store.open_bracket(&inside, MARK, HOUR).unwrap();
        store.close_bracket(&inside, HOUR).unwrap();
        for i in 0..MAX_MARKS {
            let tool = format!("t{i}");
            let other = bracket("busy", &tool, "other");
            store.open_bracket(&other, MARK, HOUR + 1 + i).unwrap();
            store.close_bracket(&other, HOUR + 1 + i).unwrap();
        }
        let short = bracket("short", "t", "k");
        store.open_bracket(&short, MARK, HOUR + 1000).unwrap();
        assert_eq!(
            store.close_bracket(&short, HOUR + 1001).unwrap(),
            Some((MARK, 1))
        );
        assert_eq!(
            store.close_bracket(&long, HOUR + 1002).unwrap(),
            Some((MARK, 2))
        );
    }

    #[test]
    fn brackets_of_the_same_session_are_not_overlap() {
        let (_dir, store) = store();
        let status = bracket("s", "status", "k");
        let commit = bracket("s", "commit", "k");
        store.open_bracket(&status, MARK, 10).unwrap();
        store.open_bracket(&commit, MARK, 20).unwrap();
        assert_eq!(store.close_bracket(&commit, 30).unwrap(), Some((MARK, 0)));
        assert_eq!(store.close_bracket(&status, 40).unwrap(), Some((MARK, 0)));
    }

    #[test]
    fn an_unclosed_mark_counts_only_while_its_call_could_still_run() {
        let (_dir, store) = store();
        store
            .open_bracket(&bracket("denied", "t", "k"), MARK, HOUR)
            .unwrap();
        let soon = bracket("soon", "t", "k");
        store
            .open_bracket(&soon, MARK, HOUR + MAX_BASH_NANOS)
            .unwrap();
        assert_eq!(
            store
                .close_bracket(&soon, HOUR + MAX_BASH_NANOS + 1)
                .unwrap(),
            Some((MARK, 1))
        );
        let later = bracket("later", "t", "k");
        store
            .open_bracket(&later, MARK, HOUR + MAX_BASH_NANOS + 2)
            .unwrap();
        assert_eq!(
            store
                .close_bracket(&later, HOUR + MAX_BASH_NANOS + 3)
                .unwrap(),
            Some((MARK, 0))
        );
    }

    #[test]
    fn a_journal_that_vanishes_during_the_state_check_is_harmless() {
        let (dir, _store) = store();
        let state = dir.path().join("state");
        let journal = state.join("collector-rust-v1.sqlite3-journal");
        let stop = std::sync::Arc::new(std::sync::atomic::AtomicBool::new(false));
        let churn = {
            let (journal, stop) = (journal.clone(), stop.clone());
            std::thread::spawn(move || {
                while !stop.load(std::sync::atomic::Ordering::Relaxed) {
                    drop(create_private(&journal));
                    drop(fs::remove_file(&journal));
                }
            })
        };
        let failures = (0..5_000)
            .filter_map(|_| state_check(&state).err().map(|e| e.to_string()))
            .collect::<Vec<_>>();
        stop.store(true, std::sync::atomic::Ordering::Relaxed);
        churn.join().unwrap();
        assert_eq!(failures, Vec::<String>::new());
    }

    #[test]
    fn refs_differing_only_by_case_keep_the_first_spelling() {
        let refs = repository_refs(&serde_json::json!([
            "https://github.com/Acme/Demo",
            "https://github.com/acme/other",
            "https://github.com/acme/demo",
        ]))
        .unwrap();
        assert_eq!(
            refs,
            [
                "https://github.com/Acme/Demo",
                "https://github.com/acme/other"
            ]
        );
    }

    #[test]
    fn retry_after_seconds_beyond_a_day_are_clamped() {
        assert_eq!(retry_after("172800", 0), Some(86400));
        assert_eq!(retry_after("30", 0), Some(30));
        assert_eq!(retry_after("-1", 0), None);
        assert_eq!(retry_after("soon", 0), None);
    }

    #[test]
    fn retry_after_http_dates_wait_until_that_time() {
        let at = 1792567680;
        let date = "Wed, 21 Oct 2026 07:28:00 GMT";
        assert_eq!(retry_after(date, at - 90), Some(90));
        assert_eq!(retry_after(date, at + 90), Some(0));
        assert_eq!(retry_after(date, at - 172800), Some(86400));
    }
}
