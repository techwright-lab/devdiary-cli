use crate::{MAX_WIRE, Object, Result, endpoint, freeze, receipt, scope};
use rusqlite::{Connection, OptionalExtension, TransactionBehavior, params};
use std::{
    fs::{self, File, OpenOptions},
    io::Read,
    os::unix::fs::{MetadataExt, OpenOptionsExt},
    path::{Component, Path},
    time::Duration,
};
const DB: &str = "collector-rust-v1.sqlite3";
const APP_ID: i64 = 1145328177;

// The same UID is trusted, as in the Python reference. Never follow symlink
// ancestors; reject shared permissions/hardlinks rather than repairing them.
pub fn private(path: &Path, directory: bool) -> Result<()> {
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
    let m = fs::metadata(path)?;
    if m.uid() != rustix::process::geteuid().as_raw()
        || m.mode() & 0o077 != 0
        || if directory {
            !m.is_dir()
        } else {
            !m.is_file() || m.nlink() != 1
        }
    {
        return Err("private_state_required".into());
    }
    Ok(())
}
fn create_private(path: &Path) -> Result<File> {
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
fn state_check(state: &Path) -> Result<()> {
    private(state, true)?;
    for entry in fs::read_dir(state)? {
        let e = entry?;
        let name = e.file_name();
        if ![
            DB,
            "collector-rust-v1.sqlite3-journal",
            "collector-rust-v1.sqlite3-wal",
            "collector-rust-v1.sqlite3-shm",
            "sync.lock",
        ]
        .iter()
        .any(|n| name == *n)
        {
            return Err("separate_empty_rust_state_required".into());
        }
        // SQLite can unlink a rollback journal between readdir and stat.
        // Only a missing transient sidecar is harmless; never ignore ownership,
        // symlink, permissions or missing persistent database/lock failures.
        if let Err(error) = private(&e.path(), false) {
            let transient = name != DB && name != "sync.lock";
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
        let mut db = Connection::open(&path)?;
        db.busy_timeout(Duration::from_millis(250))?;
        db.execute_batch(
            "PRAGMA synchronous=FULL; PRAGMA journal_mode=DELETE; PRAGMA max_page_count=4096;",
        )?;
        let page_size: i64 = db.query_row("PRAGMA page_size", [], |r| r.get(0))?;
        if page_size != 4096 {
            return Err("incompatible_spool".into());
        }
        let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
        let id: i64 = tx.query_row("PRAGMA application_id", [], |r| r.get(0))?;
        let version: i64 = tx.query_row("PRAGMA user_version", [], |r| r.get(0))?;
        if id == 0 && version == 0 && initial.is_some() {
            let tables: i64 =
                tx.query_row("SELECT count(*) FROM sqlite_master", [], |r| r.get(0))?;
            if tables != 0 {
                return Err("foreign_spool".into());
            }
            tx.execute_batch("PRAGMA application_id=1145328177; PRAGMA user_version=1;
                CREATE TABLE scope (singleton INTEGER PRIMARY KEY CHECK(singleton=1), json BLOB NOT NULL, cursor INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE outbox (seq INTEGER PRIMARY KEY, observation_id TEXT UNIQUE NOT NULL, payload BLOB NOT NULL, delivered INTEGER NOT NULL DEFAULT 0, receipt BLOB, failure TEXT);
                CREATE INDEX pending ON outbox(delivered, seq);")?;
            let config = scope(initial.ok_or("scope_required")?)?;
            tx.execute(
                "INSERT INTO scope(singleton,json) VALUES(1,?)",
                [serde_json::to_vec(&config)?],
            )?;
        } else if id != APP_ID || version != 1 {
            return Err("incompatible_spool".into());
        }
        let saved: Vec<u8> =
            tx.query_row("SELECT json FROM scope WHERE singleton=1", [], |r| r.get(0))?;
        let config = scope(&saved)?;
        if let Some(raw) = initial
            && config != scope(raw)?
        {
            return Err("frozen_scope_conflict".into());
        }
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
        let old: Option<Vec<u8>> = tx
            .query_row(
                "SELECT payload FROM outbox WHERE observation_id=?",
                [id],
                |r| r.get(0),
            )
            .optional()?;
        if let Some(old) = old {
            if old != payload {
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
            // Reserve the entire retained spool's worst-case geometry, not just
            // today's compact rows. Receipt/failure updates can split leaves or
            // create overflow pages even when the added bytes are small.
            // Each of the table and its two indexes needs at most one leaf and
            // one interior page per row. Overflow pages carry 4092 bytes; 1024
            // extra bytes bound the ID, record header, exact receipt (collector
            // <=200 ASCII bytes, two UUIDs, u64 ID), and bounded failure code.
            // The 32-page fixed allowance covers scope/schema and transient
            // balancing/cursor growth. This deliberately conservative reservation
            // counts delivered rows too: acknowledgement is not pruning.
            let reserved: i64 = tx.query_row(
                "SELECT 32 + coalesce(sum(6 + (length(payload)+1024+4091)/4092),0) FROM outbox",
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
            "SELECT count(*)-coalesce(sum(delivered),0),coalesce(sum(delivered),0) FROM outbox",
            [],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )?)
    }
    fn batch(&self, limit: usize) -> Result<Vec<(i64, Vec<u8>)>> {
        let cursor: i64 = self
            .db
            .query_row("SELECT cursor FROM scope", [], |r| r.get(0))?;
        let mut result = Vec::new();
        for comparison in [">", "<="] {
            let mut statement = self.db.prepare(&format!("SELECT seq,payload FROM outbox WHERE delivered=0 AND seq {comparison} ? ORDER BY seq LIMIT ?"))?;
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
    if !(1..=100).contains(&limit) {
        return Err("invalid_limit".into());
    }
    state_check(state)?;
    // Do not accept a credential inside the spool, or persist its locator.
    if key_path.starts_with(state) {
        return Err("separate_credential_required".into());
    }
    let lock = create_private(&state.join("sync.lock"))?;
    lock.try_lock()?;
    let key = credential(key_path)?;
    let store = Store::open(state, None)?;
    let url = store.config["endpoint"]
        .as_str()
        .ok_or("invalid_endpoint")?;
    endpoint(url)?;
    let collector = store.config["collector_ref"]
        .as_str()
        .ok_or("invalid_collector")?;
    let client = reqwest::blocking::Client::builder()
        .no_proxy()
        .redirect(reqwest::redirect::Policy::none())
        .connect_timeout(Duration::from_secs(4))
        .timeout(Duration::from_secs(5))
        .build()?;
    for (seq, payload) in store.batch(limit)? {
        // Autocommit FULL before any HTTP. Lost response or process death cannot
        // erase frozen bytes or pin the next invocation to a poison row.
        store
            .db
            .execute("UPDATE scope SET cursor=? WHERE singleton=1", [seq])?;
        let outcome = post(&client, url, &key, &payload, collector);
        match outcome {
            Ok(r) => {
                store.db.execute(
                    "UPDATE outbox SET delivered=1,receipt=?,failure=NULL WHERE seq=?",
                    params![serde_json::to_vec(&r)?, seq],
                )?;
            }
            Err((failure, stop)) => {
                store.db.execute(
                    "UPDATE outbox SET failure=? WHERE seq=?",
                    params![failure, seq],
                )?;
                if stop {
                    break;
                }
            }
        }
    }
    store.counts()
}
fn post(
    client: &reqwest::blocking::Client,
    url: &str,
    key: &str,
    payload: &[u8],
    collector: &str,
) -> std::result::Result<Object, (String, bool)> {
    let response = client
        .post(url)
        .bearer_auth(key)
        .header("Content-Type", "application/json")
        .header("Accept", "application/json")
        .body(payload.to_vec())
        .send()
        .map_err(|_| ("network_failure".into(), true))?;
    let status = response.status().as_u16();
    if !matches!(status, 200 | 201) {
        return Err((
            format!("http_{status}"),
            matches!(status, 401 | 403 | 429 | 500..=599),
        ));
    }
    let mut raw = Vec::new();
    response
        .take((MAX_WIRE + 1) as u64)
        .read_to_end(&mut raw)
        .map_err(|_| ("network_failure".into(), true))?;
    receipt(&raw, payload, collector).map_err(|_| ("invalid_receipt".into(), false))
}
