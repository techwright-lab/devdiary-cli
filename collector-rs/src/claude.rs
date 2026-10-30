//! Consented Linux command hooks. Raw input is never persisted or logged.
//! Registration uses an atomic settings replacement and a private hash-only
//! receipt binding the validated plan. Crashes leave all hooks or none installed.
use crate::{
    MAX_HOOK_INPUT, Object, Result,
    checkout::{self, Checkout},
    drops, object,
    reflog::{self, Appended},
    store::{Bracket, FINGERPRINT, SETTINGS_LOCK, Store, create_private, owner_writable, private},
    text, token,
};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    fs::{self, File, OpenOptions},
    io::{Read, Write},
    os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt},
    path::{Path, PathBuf},
    time::{SystemTime, UNIX_EPOCH},
};
use uuid::Uuid;

pub const EVENTS: [&str; 9] = [
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "PostToolUseFailure",
    "Stop",
    "SubagentStart",
    "SubagentStop",
    "SessionEnd",
];
const MAX_PLAN: usize = 65536;

fn read(path: &Path) -> Result<Vec<u8>> {
    private(path, false)?;
    bounded(path)
}
fn read_settings(path: &Path) -> Result<Vec<u8>> {
    owner_writable(path.parent().ok_or("invalid_path")?, true)?;
    owner_writable(path, false)?;
    bounded(path)
}
fn bounded(path: &Path) -> Result<Vec<u8>> {
    let mut raw = Vec::new();
    File::open(path)?
        .take((MAX_PLAN + 1) as u64)
        .read_to_end(&mut raw)?;
    if raw.len() > MAX_PLAN {
        return Err("file_too_large".into());
    }
    Ok(raw)
}
fn hash(raw: &[u8]) -> String {
    format!("{:x}", Sha256::digest(raw))
}
fn fingerprint(m: &fs::Metadata, sha: &str) -> String {
    format!(
        "{}:{}:{}:{}:{}:{}:{}:{sha}",
        m.dev(),
        m.ino(),
        m.size(),
        m.mtime(),
        m.mtime_nsec(),
        m.ctime(),
        m.ctime_nsec()
    )
}
fn executable(path: &Path) -> Result<(String, String)> {
    private(path, false)?;
    let mut f = File::open(path)?;
    let m = f.metadata()?;
    if m.mode() & 0o100 == 0 || m.len() > 128 * 1024 * 1024 {
        return Err("untrusted_executable".into());
    }
    // Native Linux executable, not an interpreter or package-manager shim.
    let mut magic = [0; 4];
    f.read_exact(&mut magic)?;
    if magic != *b"\x7fELF" {
        return Err("native_executable_required".into());
    }
    let mut digest = Sha256::new();
    digest.update(magic);
    let mut buf = [0; 65536];
    loop {
        let n = f.read(&mut buf)?;
        if n == 0 {
            break;
        }
        digest.update(&buf[..n]);
    }
    let sha = format!("{:x}", digest.finalize());
    Ok((fingerprint(&m, &sha), sha))
}
fn fingerprint_file(state: &Path, write: bool) -> Result<File> {
    private(state, true)?;
    let file = OpenOptions::new()
        .read(true)
        .write(write)
        .create(write)
        .mode(0o600)
        .custom_flags(rustix::fs::OFlags::NOFOLLOW.bits() as i32)
        .open(state.join(FINGERPRINT))?;
    let m = file.metadata()?;
    if m.uid() != rustix::process::geteuid().as_raw()
        || m.mode() & 0o077 != 0
        || m.nlink() != 1
        || !m.is_file()
    {
        return Err("private_state_required".into());
    }
    Ok(file)
}
fn verify_executable(p: &Object, state: &Path) -> Result<()> {
    let (current, sha) = executable(Path::new(text(p, "executable")?))?;
    if sha != text(p, "executable_sha256")? {
        return Err("executable_changed".into());
    }
    remember(state, &current)
}
fn remember(state: &Path, fingerprint: &str) -> Result<()> {
    let mut file = fingerprint_file(state, true)?;
    file.write_all(fingerprint.as_bytes())?;
    file.set_len(fingerprint.len() as u64)?;
    Ok(())
}
// The syncing binary is normally the registered one, so hashing it here keeps
// the full hash out of the next hook's deadline. Any other binary's stat
// fingerprint never matches the registered path and the hook re-hashes.
pub(crate) fn rehash(state: &Path) -> Result<()> {
    match executable(&std::env::current_exe()?) {
        Ok((current, _)) => remember(state, &current),
        Err(_) => match fs::remove_file(state.join(FINGERPRINT)) {
            Err(e) if e.kind() != std::io::ErrorKind::NotFound => Err(e.into()),
            _ => Ok(()),
        },
    }
}
// A matching stat fingerprint stands in for the full hash; any replacement,
// write or chmod changes ino, size, mtime or ctime and forces a re-hash.
fn trusted_executable(p: &Object, state: &Path) -> Result<()> {
    let path = Path::new(text(p, "executable")?);
    private(path, false)?;
    let current = fingerprint(&fs::metadata(path)?, text(p, "executable_sha256")?);
    let mut recorded = Vec::new();
    let read =
        fingerprint_file(state, false).and_then(|f| Ok(f.take(512).read_to_end(&mut recorded)?));
    if read.is_ok() && recorded == current.as_bytes() {
        return Ok(());
    }
    verify_executable(p, state)
}
fn quote(s: &str) -> String {
    format!("'{}'", s.replace('\'', "'\"'\"'"))
}
fn entry(plan: &Path, exe: &str) -> Result<Value> {
    Ok(
        json!({"hooks": [{"type": "command", "command": format!("{} claude-hook {} >/dev/null 2>&1 || :", quote(exe), quote(plan.to_str().ok_or("invalid_path")?)), "timeout": 2}]}),
    )
}
fn lock(state: &Path, shared: bool) -> Result<File> {
    private(state, true)?;
    let file = create_private(&state.join(SETTINGS_LOCK))?;
    rustix::fs::flock(
        &file,
        if shared {
            rustix::fs::FlockOperation::NonBlockingLockShared
        } else {
            rustix::fs::FlockOperation::NonBlockingLockExclusive
        },
    )?;
    Ok(file)
}
// Settings are rewritten, unlike discarded hook payloads. Refuse duplicate
// keys at every depth rather than silently deleting unrelated customer data.
pub(crate) struct UniqueKeys;
impl<'de> serde::Deserialize<'de> for UniqueKeys {
    fn deserialize<D: serde::Deserializer<'de>>(de: D) -> std::result::Result<Self, D::Error> {
        struct Check;
        impl<'de> serde::de::Visitor<'de> for Check {
            type Value = UniqueKeys;
            fn expecting(&self, f: &mut std::fmt::Formatter) -> std::fmt::Result {
                f.write_str("unique JSON keys")
            }
            fn visit_map<A: serde::de::MapAccess<'de>>(
                self,
                mut map: A,
            ) -> std::result::Result<UniqueKeys, A::Error> {
                let mut keys = std::collections::HashSet::new();
                while let Some(key) = map.next_key::<String>()? {
                    if !keys.insert(key) {
                        return Err(serde::de::Error::custom("duplicate_key"));
                    }
                    map.next_value::<UniqueKeys>()?;
                }
                Ok(UniqueKeys)
            }
            fn visit_seq<A: serde::de::SeqAccess<'de>>(
                self,
                mut seq: A,
            ) -> std::result::Result<UniqueKeys, A::Error> {
                while seq.next_element::<UniqueKeys>()?.is_some() {}
                Ok(UniqueKeys)
            }
            fn visit_bool<E: serde::de::Error>(
                self,
                _: bool,
            ) -> std::result::Result<UniqueKeys, E> {
                Ok(UniqueKeys)
            }
            fn visit_i64<E: serde::de::Error>(self, _: i64) -> std::result::Result<UniqueKeys, E> {
                Ok(UniqueKeys)
            }
            fn visit_u64<E: serde::de::Error>(self, _: u64) -> std::result::Result<UniqueKeys, E> {
                Ok(UniqueKeys)
            }
            fn visit_f64<E: serde::de::Error>(self, _: f64) -> std::result::Result<UniqueKeys, E> {
                Ok(UniqueKeys)
            }
            fn visit_str<E: serde::de::Error>(self, _: &str) -> std::result::Result<UniqueKeys, E> {
                Ok(UniqueKeys)
            }
            fn visit_unit<E: serde::de::Error>(self) -> std::result::Result<UniqueKeys, E> {
                Ok(UniqueKeys)
            }
        }
        de.deserialize_any(Check)
    }
}
fn settings(path: &Path) -> Result<(Vec<u8>, Object)> {
    let raw = read_settings(path)?;
    serde_json::from_slice::<UniqueKeys>(&raw)?;
    let doc = object(&raw)?;
    if doc.get("hooks").is_some_and(|v| !v.is_object()) {
        return Err("invalid_hooks".into());
    }
    if let Some(hooks) = doc.get("hooks").and_then(Value::as_object) {
        for event in EVENTS {
            if hooks.get(event).is_some_and(|v| !v.is_array()) {
                return Err("invalid_hooks".into());
            }
        }
    }
    Ok((raw, doc))
}
fn owned(doc: &Object, expected: &Value) -> Result<()> {
    for event in EVENTS {
        let items = doc
            .get("hooks")
            .and_then(|v| v.get(event))
            .and_then(Value::as_array)
            .ok_or("owned_entry_missing")?;
        if items.iter().filter(|e| *e == expected).count() != 1 {
            return Err("owned_entry_changed".into());
        }
    }
    Ok(())
}
fn replace(path: &Path, value: &Object, expected: &[u8]) -> Result<()> {
    let mode = fs::metadata(path)?.mode() & 0o755;
    atomic_replace(path, value, Some(expected), mode, read_settings)
}
fn atomic_replace(
    path: &Path,
    value: &Object,
    expected: Option<&[u8]>,
    mode: u32,
    current: fn(&Path) -> Result<Vec<u8>>,
) -> Result<()> {
    let bytes = serde_json::to_vec_pretty(value)?;
    if bytes.len() > MAX_PLAN {
        return Err("settings_too_large".into());
    }
    let parent = path.parent().ok_or("invalid_path")?;
    let temporary = parent.join(format!(".devdiary-{}.tmp", Uuid::new_v4()));
    let result = (|| -> Result<()> {
        let mut file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(&temporary)?;
        file.set_permissions(fs::Permissions::from_mode(mode))?;
        file.write_all(&bytes)?;
        file.sync_all()?;
        if let Some(expected) = expected {
            if current(path)? != expected {
                return Err("settings_changed_replan".into());
            }
            fs::rename(&temporary, path)?;
        } else {
            // Atomic create without clobbering a concurrently-created receipt.
            rustix::fs::renameat_with(
                rustix::fs::CWD,
                &temporary,
                rustix::fs::CWD,
                path,
                rustix::fs::RenameFlags::NOREPLACE,
            )?;
        }
        File::open(parent)?.sync_all()?;
        Ok(())
    })();
    let _ = fs::remove_file(&temporary);
    result
}

/// Plan contains hashes/scope and ownership only, never a settings backup (which
/// could contain provider secrets). Removal restores JSON semantics, not spacing.
pub fn plan(state: &Path, settings_path: &Path, plan_path: &Path) -> Result<()> {
    if !cfg!(target_os = "linux") {
        return Err("linux_only".into());
    }
    private(plan_path.parent().ok_or("invalid_path")?, true)?;
    if !plan_path.is_absolute() || plan_path.exists() {
        return Err("new_absolute_plan_required".into());
    }
    if plan_path.starts_with(state)
        || settings_path.starts_with(state)
        || manifest_path(plan_path) == settings_path
    {
        return Err("separate_registration_paths_required".into());
    }
    let _lock = lock(state, false)?;
    let (raw, doc) = settings(settings_path)?;
    if String::from_utf8_lossy(&raw).contains("observer_hook") {
        return Err("python_observer_registered".into());
    }
    let store = Store::open(state, None)?;
    if !crate::installation(&store.config) {
        let repo = Path::new(text(&store.config, "repository")?);
        if !repo.is_dir() || fs::canonicalize(repo)? != repo {
            return Err("canonical_repository_required".into());
        }
    }
    let exe = std::env::current_exe()?;
    let (current, digest) = executable(&exe)?;
    let expected = entry(plan_path, exe.to_str().ok_or("invalid_path")?)?;
    // Do not stack registrations or silently take over another Rust adapter.
    if String::from_utf8_lossy(&raw).contains(" claude-hook ") {
        return Err("already_registered".into());
    }
    let absent: Vec<_> = EVENTS
        .iter()
        .filter(|e| doc.get("hooks").and_then(|h| h.get(**e)).is_none())
        .collect();
    let plan = json!({"version": 1, "state": state, "settings": settings_path, "plan": plan_path,
        "scope": store.config, "executable": exe, "executable_sha256": digest,
        "settings_sha256": hash(&raw), "entry": expected, "absent_events": absent,
        "absent_hooks": !doc.contains_key("hooks")});
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(plan_path)?;
    file.write_all(&serde_json::to_vec_pretty(&plan)?)?;
    file.sync_all()?;
    File::open(plan_path.parent().ok_or("invalid_path")?)?.sync_all()?;
    remember(state, &current)
}
fn load(plan_path: &Path) -> Result<Object> {
    let raw = read(plan_path)?;
    serde_json::from_slice::<UniqueKeys>(&raw)?;
    let p = object(&raw)?;
    const FIELDS: [&str; 11] = [
        "version",
        "state",
        "settings",
        "plan",
        "scope",
        "executable",
        "executable_sha256",
        "settings_sha256",
        "entry",
        "absent_events",
        "absent_hooks",
    ];
    if p.len() != FIELDS.len()
        || FIELDS.iter().any(|k| !p.contains_key(*k))
        || p.get("version") != Some(&json!(1))
        || Path::new(text(&p, "plan")?) != plan_path
    {
        return Err("invalid_plan".into());
    }
    for key in ["state", "settings", "plan", "executable"] {
        let path = Path::new(text(&p, key)?);
        if !path.is_absolute()
            || path
                .components()
                .any(|c| matches!(c, std::path::Component::ParentDir))
        {
            return Err("invalid_plan_path".into());
        }
    }
    let state = Path::new(text(&p, "state")?);
    if plan_path.starts_with(state)
        || Path::new(text(&p, "settings")?).starts_with(state)
        || plan_path == Path::new(text(&p, "settings")?)
        || manifest_path(plan_path) == Path::new(text(&p, "settings")?)
        || manifest_path(plan_path) == Path::new(text(&p, "executable")?)
    {
        return Err("separate_registration_paths_required".into());
    }
    for key in ["executable_sha256", "settings_sha256"] {
        let digest = text(&p, key)?;
        if digest.len() != 64
            || !digest
                .bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
        {
            return Err("invalid_digest".into());
        }
    }
    let scope = p
        .get("scope")
        .and_then(Value::as_object)
        .ok_or("invalid_scope")?;
    if crate::scope(&serde_json::to_vec(scope)?)? != *scope {
        return Err("invalid_scope".into());
    }
    if p.get("entry") != Some(&entry(plan_path, text(&p, "executable")?)?) {
        return Err("invalid_entry".into());
    }
    let absent = p["absent_events"].as_array().ok_or("invalid_plan")?;
    let mut seen = std::collections::HashSet::new();
    for event in absent {
        let event = event.as_str().ok_or("invalid_plan")?;
        if !EVENTS.contains(&event) || !seen.insert(event) {
            return Err("invalid_plan".into());
        }
    }
    let absent_hooks = p["absent_hooks"].as_bool().ok_or("invalid_plan")?;
    if absent_hooks && absent.len() != EVENTS.len() {
        return Err("invalid_plan".into());
    }
    Ok(p)
}

fn original_presence(p: &Object, doc: &Object) -> Result<()> {
    let absent: Vec<_> = EVENTS
        .iter()
        .filter(|e| doc.get("hooks").and_then(|h| h.get(**e)).is_none())
        .collect();
    if p["absent_events"] != json!(absent) || p["absent_hooks"] != json!(!doc.contains_key("hooks"))
    {
        return Err("invalid_original_presence".into());
    }
    Ok(())
}

// Outside the bounded outbox: hashes only, never a backup of customer settings.
// Bind every proposal field, including scope and removal metadata, before the
// first settings write. The same UID is trusted, not hostile receipt forgery.
fn manifest_path(plan: &Path) -> std::path::PathBuf {
    let mut name = plan.as_os_str().to_os_string();
    name.push(".registration.json");
    name.into()
}
fn manifest(plan: &Path, p: &Object) -> Result<Object> {
    let raw = read(&manifest_path(plan))?;
    serde_json::from_slice::<UniqueKeys>(&raw)?;
    let m = object(&raw)?;
    let status = text(&m, "status")?;
    if !["prepared", "installed", "removing", "removed"].contains(&status)
        || m.keys()
            .any(|k| !["status", "plan_sha256", "removed_sha256"].contains(&k.as_str()))
        || (status == "removing" && !m.contains_key("removed_sha256"))
    {
        return Err("invalid_registration".into());
    }
    if let Some(digest) = m.get("removed_sha256") {
        let digest = digest.as_str().ok_or("invalid_registration")?;
        if digest.len() != 64
            || !digest
                .bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
        {
            return Err("invalid_registration".into());
        }
    }
    if m.get("plan_sha256") != Some(&json!(hash(&serde_json::to_vec(p)?))) {
        return Err("plan_changed".into());
    }
    Ok(m)
}
fn save_manifest(plan: &Path, m: &Object) -> Result<()> {
    let path = manifest_path(plan);
    let old = if path.exists() {
        Some(read(&path)?)
    } else {
        None
    };
    atomic_replace(&path, m, old.as_deref(), 0o600, read)
}

#[cfg(test)]
thread_local! { static INTERRUPT: std::cell::Cell<Option<&'static str>> = const { std::cell::Cell::new(None) }; }
fn checkpoint(_stage: &'static str) -> Result<()> {
    #[cfg(test)]
    if INTERRUPT.with(|point| point.get() == Some(_stage)) {
        return Err("test_interruption".into());
    }
    Ok(())
}
fn validate(p: &Object) -> Result<Store> {
    if !cfg!(target_os = "linux") {
        return Err("linux_only".into());
    }
    let state = Path::new(text(p, "state")?);
    verify_executable(p, state)?;
    let store = Store::open(state, None)?;
    if p.get("scope") != Some(&Value::Object(store.config.clone())) {
        return Err("scope_changed".into());
    }
    Ok(store)
}
pub fn apply(plan_path: &Path) -> Result<()> {
    let p = load(plan_path)?;
    let path = Path::new(text(&p, "settings")?);
    let _lock = lock(Path::new(text(&p, "state")?), false)?;
    let (raw, mut doc) = settings(path)?;
    if owned(&doc, &p["entry"]).is_ok() {
        let mut m = manifest(plan_path, &p)?;
        if !["prepared", "installed"].contains(&text(&m, "status")?) {
            return Err("registration_not_active".into());
        }
        validate(&p)?;
        m.insert("status".into(), json!("installed"));
        return save_manifest(plan_path, &m);
    }
    if hash(&raw) != text(&p, "settings_sha256")? {
        return Err("stale_plan".into());
    }
    original_presence(&p, &doc)?;
    validate(&p)?;
    // A removed receipt can be replaced only after validating the new original.
    if manifest_path(plan_path).exists() {
        let old = object(&read(&manifest_path(plan_path))?)?;
        if old.get("status") != Some(&json!("removed")) {
            let m = manifest(plan_path, &p)?;
            if m.get("status") != Some(&json!("prepared")) {
                return Err("registration_not_active".into());
            }
        }
    }
    let mut m = object(&serde_json::to_vec(&json!({
        "plan_sha256": hash(&serde_json::to_vec(&p)?), "status": "prepared"
    }))?)?;
    let hooks = doc
        .entry("hooks")
        .or_insert_with(|| json!({}))
        .as_object_mut()
        .ok_or("invalid_hooks")?;
    for event in EVENTS {
        hooks
            .entry(event)
            .or_insert_with(|| json!([]))
            .as_array_mut()
            .ok_or("invalid_hooks")?
            .push(p["entry"].clone());
    }
    if serde_json::to_vec_pretty(&doc)?.len() > MAX_PLAN {
        return Err("settings_too_large".into());
    }
    save_manifest(plan_path, &m)?;
    checkpoint("prepared")?;
    replace(path, &doc, &raw)?;
    checkpoint("applied_settings")?;
    m.insert("status".into(), json!("installed"));
    save_manifest(plan_path, &m)
}
/// Read-only local evidence, never a statement of effective host trust/policy.
pub fn registration_status(plan_path: &Path, state: &Path) -> Result<Object> {
    let p = load(plan_path)?;
    if Path::new(text(&p, "state")?) != state {
        return Err("scope_changed".into());
    }
    let (_, doc) = settings(Path::new(text(&p, "settings")?))?;
    let status = if manifest_path(plan_path).exists() {
        let m = manifest(plan_path, &p)?;
        let status = text(&m, "status")?;
        if status == "installed" && owned(&doc, &p["entry"]).is_err() {
            "changed".to_owned()
        } else {
            status.to_owned()
        }
    } else {
        "planned_not_applied".to_owned()
    };
    object(&serde_json::to_vec(&json!({
        "registration": status,
        "target_settings_block": doc.get("disableAllHooks") == Some(&json!(true))
            || doc.get("allowManagedHooksOnly") == Some(&json!(true))
    }))?)
}
pub fn remove(plan_path: &Path) -> Result<()> {
    let p = load(plan_path)?;
    let path = Path::new(text(&p, "settings")?);
    // Without its state directory no hook can capture, so removal proceeds
    // unlocked rather than being stranded by a deleted spool.
    let state = Path::new(text(&p, "state")?);
    let _lock = if state.is_dir() {
        Some(lock(state, false)?)
    } else {
        None
    };
    let mut m = manifest(plan_path, &p)?;
    let (raw, mut doc) = settings(path)?;
    let status = text(&m, "status")?;
    if status == "removed" {
        // Do not re-remove or rewrite later customer edits.
        return Ok(());
    }
    if status == "removing" && m.get("removed_sha256") == Some(&json!(hash(&raw))) {
        m.insert("status".into(), json!("removed"));
        return save_manifest(plan_path, &m);
    }
    if status == "prepared" && hash(&raw) == text(&p, "settings_sha256")? {
        original_presence(&p, &doc)?;
        m.insert("status".into(), json!("removed"));
        return save_manifest(plan_path, &m);
    }
    if !["prepared", "installed", "removing"].contains(&status) {
        return Err("invalid_registration".into());
    }
    // Removal must remain possible after executable upgrades or spool failure.
    owned(&doc, &p["entry"])?;
    let hooks = doc
        .get_mut("hooks")
        .and_then(Value::as_object_mut)
        .ok_or("invalid_hooks")?;
    let absent = p
        .get("absent_events")
        .and_then(Value::as_array)
        .ok_or("invalid_plan")?;
    for event in EVENTS {
        let items = hooks
            .get_mut(event)
            .and_then(Value::as_array_mut)
            .ok_or("invalid_hooks")?;
        items.retain(|e| e != &p["entry"]);
        if items.is_empty() && absent.contains(&json!(event)) {
            hooks.remove(event);
        }
    }
    if hooks.is_empty() && p.get("absent_hooks") == Some(&Value::Bool(true)) {
        doc.remove("hooks");
    }
    m.insert("status".into(), json!("removing"));
    m.insert(
        "removed_sha256".into(),
        json!(hash(&serde_json::to_vec_pretty(&doc)?)),
    );
    save_manifest(plan_path, &m)?;
    checkpoint("removing")?;
    replace(path, &doc, &raw)?;
    checkpoint("removed_settings")?;
    m.insert("status".into(), json!("removed"));
    save_manifest(plan_path, &m)?;
    checkpoint("removed")
}

const HOOK_FIELDS: [&str; 11] = [
    "hook_event_name",
    "session_id",
    "cwd",
    "prompt_id",
    "tool_use_id",
    "agent_id",
    "agent_type",
    "source",
    "model",
    "reason",
    "tool_name",
];
fn fields(raw: &[u8]) -> Result<Object> {
    struct Known;
    impl<'de> serde::de::Visitor<'de> for Known {
        type Value = Object;
        fn expecting(&self, f: &mut std::fmt::Formatter) -> std::fmt::Result {
            f.write_str("hook object")
        }
        fn visit_map<A: serde::de::MapAccess<'de>>(
            self,
            mut map: A,
        ) -> std::result::Result<Object, A::Error> {
            let mut seen = std::collections::HashSet::new();
            let mut out = Object::new();
            while let Some(key) = map.next_key::<String>()? {
                if !seen.insert(key.clone()) {
                    return Err(serde::de::Error::custom("duplicate"));
                }
                if HOOK_FIELDS.contains(&key.as_str()) {
                    out.insert(key, map.next_value::<Value>()?);
                } else {
                    map.next_value::<serde::de::IgnoredAny>()?;
                }
            }
            Ok(out)
        }
    }
    if raw.len() > MAX_HOOK_INPUT {
        return Err("input_too_large".into());
    }
    let mut de = serde_json::Deserializer::from_slice(raw);
    let data = serde::Deserializer::deserialize_map(&mut de, Known)?;
    de.end()?;
    let event = text(&data, "hook_event_name")?;
    if !EVENTS.contains(&event) || !token(text(&data, "session_id")?) {
        return Err("invalid_event".into());
    }
    Ok(data)
}
fn within_repository(cwd: &Path, config: &Object) -> Result<()> {
    let repository = Path::new(text(config, "repository")?);
    if !repository.is_dir()
        || !cwd.is_dir()
        || fs::canonicalize(repository)? != repository
        || !fs::canonicalize(cwd)?.starts_with(repository)
    {
        return Err("outside_scope".into());
    }
    Ok(())
}
const PAPERCLIP: [(&str, &str); 3] = [
    ("orchestrator_agent_ref", "PAPERCLIP_AGENT_ID"),
    ("orchestrator_run_ref", "PAPERCLIP_RUN_ID"),
    ("orchestrator_task_ref", "PAPERCLIP_TASK_ID"),
];
const WORKSPACE_HINTS: [&str; 2] = [
    "PAPERCLIP_WORKSPACE_WORKTREE_PATH",
    "PAPERCLIP_WORKSPACE_CWD",
];
// Any Paperclip id, even malformed, marks the run orchestrated: reporting
// 'none' would let the server bind someone else's run to the machine owner.
fn orchestrator() -> Object {
    let mut out = Object::new();
    let mut paperclip = false;
    for (field, name) in PAPERCLIP {
        match std::env::var(name) {
            Ok(value) => {
                paperclip = true;
                let value = value.to_ascii_lowercase();
                if crate::is_uuid(&value) {
                    out.insert(field.into(), value.into());
                }
            }
            Err(std::env::VarError::NotUnicode(_)) => paperclip = true,
            Err(std::env::VarError::NotPresent) => {}
        }
    }
    out.insert(
        "orchestrator".into(),
        if paperclip { "paperclip" } else { "none" }.into(),
    );
    out
}
fn workspace_checkout(cwd: &Path, paperclip: bool) -> (Checkout, Option<PathBuf>) {
    let found = checkout::locate(cwd);
    if found.0 != Checkout::Absent || !paperclip {
        return found;
    }
    WORKSPACE_HINTS
        .iter()
        .filter_map(|name| std::env::var(name).ok())
        .filter(|hint| hint.len() <= 4096 && Path::new(hint).is_absolute())
        .map(|hint| checkout::locate(Path::new(&hint)))
        .find(|c| c.0 != Checkout::Absent)
        .unwrap_or((Checkout::Absent, None))
}
type Located = Option<(String, Option<PathBuf>)>;
// The stamped ref is the server's spelling; GitHub names are case-insensitive.
fn locate(cwd: &Path, config: &Object, state: &Path, paperclip: bool) -> Result<Located> {
    let (found, gitdir) = workspace_checkout(cwd, paperclip);
    match found {
        Checkout::Repository(found) => crate::store::read_refs(state)?
            .into_iter()
            .find(|r| r.eq_ignore_ascii_case(&found))
            .map(|reference| Some((reference, gitdir)))
            .ok_or_else(|| "unknown_repository".into()),
        Checkout::Unresolved => Err("unknown_repository".into()),
        Checkout::Absent
            if paperclip
                && crate::wire_version(config) == 2
                && config["record_orchestrated_outside_repos"] == json!(true) =>
        {
            Ok(None)
        }
        Checkout::Absent => Err("outside_scope".into()),
    }
}
fn scoped_row(cwd: &Path, config: &Object, state: &Path) -> Result<(Object, Option<PathBuf>)> {
    if !crate::installation(config) {
        within_repository(cwd, config)?;
        let row = json!({"schema_version": 1, "repository": config["repository"]});
        return Ok((object(&serde_json::to_vec(&row)?)?, None));
    }
    let mut row = orchestrator();
    let paperclip = row["orchestrator"] == json!("paperclip");
    let mut gitdir = None;
    if let Some((reference, found)) = locate(cwd, config, state, paperclip)? {
        row.insert("repository_ref".into(), reference.into());
        gitdir = found;
    }
    row.insert("schema_version".into(), crate::wire_version(config).into());
    Ok((row, gitdir))
}
fn normalize(data: &Object, config: &Object, state: &Path) -> Result<(Object, Option<PathBuf>)> {
    let event = text(data, "hook_event_name")?;
    let session = text(data, "session_id")?;
    let cwd = text(data, "cwd")?;
    if cwd.len() > 4096 || !Path::new(cwd).is_absolute() {
        return Err("invalid_cwd".into());
    }
    let (mut out, gitdir) = scoped_row(Path::new(cwd), config, state)?;
    for (key, value) in json!({"installation_id": config["installation_id"], "runtime": "claude-code", "session_id": session, "event": event, "actor_ref": null, "attribution_basis": "unknown", "observed_at": SystemTime::now().duration_since(UNIX_EPOCH)?.as_secs_f64()}).as_object().ok_or("invalid_metadata")? {
        out.insert(key.clone(), value.clone());
    }
    for key in ["prompt_id", "tool_use_id", "agent_id", "agent_type"] {
        if let Some(value) = data.get(key) {
            if !value.as_str().is_some_and(token) {
                return Err("invalid_metadata".into());
            }
            out.insert(key.into(), value.clone());
        }
    }
    if event == "SessionStart" {
        if data
            .get("source")
            .and_then(Value::as_str)
            .is_some_and(|s| ["startup", "resume", "clear", "compact", "fork"].contains(&s))
        {
            out.insert("source".into(), data["source"].clone());
        }
        if data.get("model").and_then(Value::as_str).is_some_and(token) {
            out.insert("model".into(), data["model"].clone());
        }
    }
    if event == "SessionEnd"
        && data.get("reason").and_then(Value::as_str).is_some_and(|s| {
            ["clear", "resume", "logout", "prompt_input_exit", "other"].contains(&s)
        })
    {
        out.insert("reason".into(), data["reason"].clone());
    }
    if ["PreToolUse", "PostToolUse", "PostToolUseFailure"].contains(&event)
        && data
            .get("tool_name")
            .and_then(Value::as_str)
            .is_some_and(token)
    {
        out.insert("tool_name".into(), data["tool_name"].clone());
    }
    let identity = match event {
        "PreToolUse" | "PostToolUse" | "PostToolUseFailure" => out.get("tool_use_id"),
        "UserPromptSubmit" | "Stop" => out.get("prompt_id"),
        "SubagentStart" | "SubagentStop" => out.get("agent_id"),
        _ => None,
    };
    let id = if let Some(identity) = identity {
        // Same documented correlation tuple as Python. Never hash private input.
        let tuple = json!([
            config["installation_id"],
            session,
            out.get("agent_id"),
            event,
            identity
        ]);
        Uuid::new_v5(&Uuid::NAMESPACE_OID, &serde_json::to_vec(&tuple)?)
    } else {
        Uuid::new_v4()
    };
    out.insert("observation_id".into(), id.to_string().into());
    Ok((out, gitdir))
}
// Only the bracketing Bash call's own checkout is read: a commit made between
// tool calls or in another checkout is honestly absent, never inferred.
fn bracket(store: &Store, state: &Path, row: &mut Object, gitdir: &Path) -> Result<()> {
    let tool = row.get("tool_use_id").and_then(Value::as_str);
    let key = reflog::checkout_key(gitdir);
    let (Some(tool), Some(key)) = (tool, key) else {
        return Ok(());
    };
    if crate::wire_version(&store.config) != 2 || row.get("tool_name") != Some(&json!("Bash")) {
        return Ok(());
    }
    let (session, tool) = (text(row, "session_id")?.to_owned(), tool.to_owned());
    let call = Bracket {
        session_id: &session,
        tool_use_id: &tool,
        checkout_key: &key,
    };
    let at = nanos_now()?;
    match text(row, "event")? {
        "PreToolUse" => {
            if let Some(mark) = reflog::mark(gitdir) {
                store.open_bracket(&call, mark, at)?;
            }
        }
        "PostToolUse" | "PostToolUseFailure" => {
            if let Some((mark, overlap)) = store.close_bracket(&call, at)? {
                linked(state, row, reflog::appended(gitdir, mark), overlap);
            }
        }
        _ => {}
    }
    Ok(())
}
fn nanos_now() -> Result<i64> {
    Ok(i64::try_from(
        SystemTime::now().duration_since(UNIX_EPOCH)?.as_nanos(),
    )?)
}
fn linked(state: &Path, row: &mut Object, appended: Appended, overlap: u64) {
    match appended {
        Appended::Commits(shas) if shas.is_empty() => {}
        Appended::Commits(shas) => {
            row.insert("commits".into(), json!(shas));
            row.insert("overlap_count".into(), overlap.into());
        }
        Appended::Gap(gap) => {
            if gap == "reflog_rewritten" {
                drops::note(state, "reflog_rotated");
            }
            row.insert("commit_gap".into(), gap.into());
        }
    }
}
fn input_reason(error: &(dyn std::error::Error + 'static)) -> &'static str {
    if error.is::<serde_json::Error>() {
        return "invalid";
    }
    drop_reason(error)
}
// Past input parsing, unparseable JSON is registration state: settings, the
// manifest or the saved scope, never the hook event.
fn drop_reason(error: &(dyn std::error::Error + 'static)) -> &'static str {
    if error.is::<serde_json::Error>() {
        return "stale_plan";
    }
    if let Some(e) = error.downcast_ref::<rustix::io::Errno>() {
        return if *e == rustix::io::Errno::WOULDBLOCK {
            "busy"
        } else {
            "error"
        };
    }
    if let Some(e) = error.downcast_ref::<std::io::Error>() {
        return match e.kind() {
            std::io::ErrorKind::PermissionDenied => "permission",
            _ => "error",
        };
    }
    if let Some(rusqlite::Error::SqliteFailure(e, _)) = error.downcast_ref::<rusqlite::Error>() {
        return match e.code {
            rusqlite::ErrorCode::DatabaseBusy | rusqlite::ErrorCode::DatabaseLocked => "busy",
            _ => "error",
        };
    }
    match error.to_string().as_str() {
        "outside_scope" => "outside_scope",
        "unknown_repository" => "unknown_repository",
        "spool_full" => "spool_full",
        "observation_conflict" => "observation_conflict",
        "revoked" => "revoked",
        "invalid_event" | "invalid_cwd" | "invalid_metadata" | "input_too_large" => "invalid",
        "owned_entry_missing"
        | "owned_entry_changed"
        | "executable_changed"
        | "scope_changed"
        | "plan_changed"
        | "registration_not_active"
        | "invalid_registration"
        | "repair_required"
        | "incompatible_spool" => "stale_plan",
        "private_state_required"
        | "symlink_path"
        | "unsafe_ancestor"
        | "unsafe_settings"
        | "untrusted_executable"
        | "native_executable_required" => "permission",
        _ => "error",
    }
}
/// Cheap checks run first so out-of-scope events never hash the executable,
/// parse settings or open SQLite. Every refusal after the plan loads is counted.
pub fn hook(plan_path: &Path, raw: Result<Vec<u8>>) -> Result<()> {
    let data = raw.and_then(|raw| fields(&raw));
    let p = load(plan_path)?;
    let state = Path::new(text(&p, "state")?);
    let data = data.inspect_err(|error| drops::note(state, input_reason(error.as_ref())))?;
    let outcome = capture(plan_path, &p, state, &data);
    if let Err(error) = &outcome {
        drops::note(state, drop_reason(error.as_ref()));
    }
    outcome
}
fn active(plan_path: &Path, p: &Object) -> Result<()> {
    let m = manifest(plan_path, p)?;
    if !["prepared", "installed"].contains(&text(&m, "status")?) {
        return Err("registration_not_active".into());
    }
    Ok(())
}
fn capture(plan_path: &Path, p: &Object, state: &Path, data: &Object) -> Result<()> {
    active(plan_path, p)?;
    if crate::pairing::revoked(state) {
        return Err("revoked".into());
    }
    let scope = p
        .get("scope")
        .and_then(Value::as_object)
        .ok_or("invalid_scope")?;
    let (mut row, gitdir) = normalize(data, scope, state)?;
    trusted_executable(p, state)?;
    let _lock = lock(state, true)?;
    active(plan_path, p)?;
    let (_, doc) = settings(Path::new(text(p, "settings")?))?;
    owned(&doc, &p["entry"])?;
    let mut store = Store::open(state, None)?;
    if *scope != store.config {
        return Err("scope_changed".into());
    }
    if let Some(gitdir) = gitdir {
        bracket(&store, state, &mut row, &gitdir)?;
    }
    if ["UserPromptSubmit", "Stop", "SessionEnd"].contains(&text(&row, "event")?) {
        store.release_brackets(text(&row, "session_id")?, nanos_now()?)?;
    }
    // A racing duplicate has the same UUID but a new observation timestamp.
    // The store's conflict refusal keeps the first observation unrewritten.
    store.collect(&serde_json::to_vec(&row)?)
}

#[cfg(test)]
mod recovery_tests {
    use super::*;
    use std::os::unix::fs::PermissionsExt;

    fn fixture(original: Value) -> (tempfile::TempDir, std::path::PathBuf) {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        fs::set_permissions(root, fs::Permissions::from_mode(0o700)).unwrap();
        let state = root.join("state");
        let repo = root.join("repo");
        for path in [&state, &repo] {
            fs::create_dir(path).unwrap();
            fs::set_permissions(path, fs::Permissions::from_mode(0o700)).unwrap();
        }
        let exe = root.join("collector");
        fs::copy(std::env::current_exe().unwrap(), &exe).unwrap();
        fs::set_permissions(&exe, fs::Permissions::from_mode(0o700)).unwrap();
        let mut config = object(include_bytes!("../tests/fixtures/scope.json")).unwrap();
        config.insert("repository".into(), json!(repo));
        Store::open(&state, Some(&serde_json::to_vec(&config).unwrap())).unwrap();
        let path = root.join("settings.json");
        let raw = serde_json::to_vec(&original).unwrap();
        fs::write(&path, &raw).unwrap();
        fs::set_permissions(&path, fs::Permissions::from_mode(0o600)).unwrap();
        let plan_path = root.join("plan.json");
        let absent: Vec<_> = EVENTS
            .iter()
            .filter(|e| original.get("hooks").and_then(|h| h.get(**e)).is_none())
            .collect();
        let p = json!({"version":1,"state":state,"settings":path,"plan":plan_path,
            "scope":config,"executable":exe,"executable_sha256":executable(&exe).unwrap().1,
            "settings_sha256":hash(&raw),"entry":entry(&plan_path, exe.to_str().unwrap()).unwrap(),
            "absent_events":absent,"absent_hooks":original.get("hooks").is_none()});
        fs::write(&plan_path, serde_json::to_vec(&p).unwrap()).unwrap();
        fs::set_permissions(&plan_path, fs::Permissions::from_mode(0o600)).unwrap();
        (dir, plan_path)
    }

    #[test]
    fn removal_recovers_every_durable_boundary() {
        for original in [
            json!({}),
            json!({"hooks":{}}),
            json!({"hooks":{"Stop":[],"Other":[{"keep":true}]},"secret":"NEVER_BACK_UP"}),
        ] {
            // Stop after each persisted boundary, drop all stack state/locks,
            // then re-enter the public API from disk. No production fault knobs.
            for stage in [
                "prepared",
                "applied_settings",
                "removing",
                "removed_settings",
                "removed",
            ] {
                let (dir, plan) = fixture(original.clone());
                let settings_path = dir.path().join("settings.json");
                let p = load(&plan).unwrap();
                if ["prepared", "applied_settings"].contains(&stage) {
                    INTERRUPT.with(|point| point.set(Some(stage)));
                    assert!(apply(&plan).is_err());
                } else {
                    apply(&plan).unwrap();
                    // Preserve edits made by the customer after installation.
                    let (raw, mut doc) = settings(&settings_path).unwrap();
                    doc.insert("later".into(), json!(true));
                    replace(&settings_path, &doc, &raw).unwrap();
                    INTERRUPT.with(|point| point.set(Some(stage)));
                    assert!(remove(&plan).is_err());
                }
                INTERRUPT.with(|point| point.set(None));
                let m = manifest(&plan, &p).unwrap();
                let expected_status = match stage {
                    "prepared" | "applied_settings" => "prepared",
                    "removing" | "removed_settings" => "removing",
                    _ => "removed",
                };
                assert_eq!(m["status"], expected_status);
                let (_, doc) = settings(&settings_path).unwrap();
                if ["applied_settings", "removing"].contains(&stage) {
                    owned(&doc, &p["entry"]).unwrap();
                } else {
                    assert!(owned(&doc, &p["entry"]).is_err());
                }
                if ["removing", "removed_settings", "removed"].contains(&stage) {
                    // An inactive registration blocks capture before any store work.
                    let event = json!({"hook_event_name": "Stop", "session_id": "s",
                        "cwd": dir.path().join("repo")});
                    assert_eq!(
                        hook(&plan, Ok(serde_json::to_vec(&event).unwrap()))
                            .unwrap_err()
                            .to_string(),
                        "registration_not_active"
                    );
                    // Removal retry works without a usable spool or executable.
                    fs::remove_dir_all(dir.path().join("state")).unwrap();
                    fs::remove_file(dir.path().join("collector")).unwrap();
                }
                remove(&plan).unwrap();
                let mut expected = original.clone();
                if !["prepared", "applied_settings"].contains(&stage) {
                    expected["later"] = json!(true);
                }
                assert_eq!(Value::Object(settings(&settings_path).unwrap().1), expected);
                let before = read(&settings_path).unwrap();
                let receipt = read(&manifest_path(&plan)).unwrap();
                remove(&plan).unwrap();
                assert_eq!(read(&settings_path).unwrap(), before);
                assert_eq!(read(&manifest_path(&plan)).unwrap(), receipt);
                assert_eq!(manifest(&plan, &p).unwrap()["status"], "removed");
                assert!(
                    !String::from_utf8(receipt)
                        .unwrap()
                        .contains("NEVER_BACK_UP")
                );
            }
        }
    }

    #[test]
    fn removal_retry_refuses_ambiguous_post_replace_edits() {
        let (dir, plan) = fixture(json!({"hooks":{"Stop":[]}}));
        apply(&plan).unwrap();
        INTERRUPT.with(|point| point.set(Some("removed_settings")));
        assert!(remove(&plan).is_err());
        INTERRUPT.with(|point| point.set(None));
        let path = dir.path().join("settings.json");
        let (raw, mut doc) = settings(&path).unwrap();
        doc.insert("later".into(), json!(true));
        replace(&path, &doc, &raw).unwrap();
        let before = read(&path).unwrap();
        let receipt = read(&manifest_path(&plan)).unwrap();
        assert!(remove(&plan).is_err());
        assert_eq!(read(&path).unwrap(), before);
        assert_eq!(read(&manifest_path(&plan)).unwrap(), receipt);
    }
}
