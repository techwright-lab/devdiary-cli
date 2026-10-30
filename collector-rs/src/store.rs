use crate::{MAX_WIRE, Object, Result, drops, endpoint, freeze, receipt, scope};
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
fn owned_by_us(path: &Path, directory: bool, forbidden_mode: u32) -> bool {
    fs::metadata(path).is_ok_and(|m| {
        m.uid() == rustix::process::geteuid().as_raw()
            && m.mode() & forbidden_mode == 0
            && if directory {
                m.is_dir()
            } else {
                m.is_file() && m.nlink() == 1
            }
    })
}
// The same UID is trusted, as in the Python reference. Never follow symlink
// ancestors; reject shared permissions/hardlinks rather than repairing them.
pub fn private(path: &Path, directory: bool) -> Result<()> {
    trusted_ancestors(path)?;
    if !owned_by_us(path, directory, 0o077) {
        return Err("private_state_required".into());
    }
    Ok(())
}
// Customer settings stay readable by others (0644 under 0755); only writes by
// anyone but the owner, hardlinks and symlinks are refused.
pub fn owner_writable(path: &Path, directory: bool) -> Result<()> {
    trusted_ancestors(path)?;
    if !owned_by_us(path, directory, 0o022) {
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
        // Journals, the fingerprint and drop logs can be unlinked between readdir
        // and stat. Only a missing transient file is harmless; never ignore
        // ownership, symlink, permissions or a missing database/lock.
        if let Err(error) = private(&e.path(), false) {
            let transient = !persistent.iter().any(|n| name == *n);
            let missing = error
                .downcast_ref::<std::io::Error>()
                .is_some_and(|e| e.kind() == std::io::ErrorKind::NotFound);
            if !transient || !missing {
                return Err(error);
            }
        }
    }
    Ok(())
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
            // slack. 192 fixed pages cover schema, scope, drops and capped tombstones.
            let reserved: i64 = tx.query_row(
                "SELECT 192 + coalesce(sum(4 + (length(payload)+1024+4091)/4092),0) FROM outbox",
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
