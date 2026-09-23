//! Consented Linux command hooks. Raw input is never persisted or logged.
//! Registration is one atomic settings replacement; the immutable private plan
//! is the ownership receipt. A crash leaves either all hooks or none installed.
use crate::{
    MAX_INPUT, Object, Result, object,
    store::{Store, private},
    text, token,
};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    fs::{self, File, OpenOptions},
    io::{Read, Write},
    os::unix::fs::{MetadataExt, OpenOptionsExt},
    path::Path,
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
fn executable(path: &Path) -> Result<String> {
    private(path, false)?;
    let m = fs::metadata(path)?;
    if m.mode() & 0o100 == 0 || m.len() > 128 * 1024 * 1024 {
        return Err("untrusted_executable".into());
    }
    // Native Linux executable, not an interpreter or package-manager shim.
    let mut f = File::open(path)?;
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
    Ok(format!("{:x}", digest.finalize()))
}
fn quote(s: &str) -> String {
    format!("'{}'", s.replace('\'', "'\"'\"'"))
}
fn entry(plan: &Path, exe: &str) -> Result<Value> {
    Ok(
        json!({"hooks": [{"type": "command", "command": format!("{} claude-hook {} >/dev/null 2>&1 || :", quote(exe), quote(plan.to_str().ok_or("invalid_path")?)), "timeout": 2}]}),
    )
}
fn lock(settings: &Path, shared: bool) -> Result<File> {
    let parent = settings.parent().ok_or("invalid_path")?;
    private(parent, true)?;
    let path = parent.join(".devdiary-rust-claude.lock");
    let file = match OpenOptions::new()
        .read(true)
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(&path)
    {
        Ok(f) => f,
        Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => {
            private(&path, false)?;
            OpenOptions::new().read(true).write(true).open(&path)?
        }
        Err(e) => return Err(e.into()),
    };
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
struct UniqueKeys;
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
    let raw = read(path)?;
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
        file.write_all(&bytes)?;
        file.sync_all()?;
        if read(path)? != expected {
            return Err("settings_changed_replan".into());
        }
        fs::rename(&temporary, path)?;
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
    if plan_path.starts_with(state) || settings_path.starts_with(state) {
        return Err("separate_registration_paths_required".into());
    }
    let _lock = lock(settings_path, false)?;
    let (raw, doc) = settings(settings_path)?;
    let store = Store::open(state, None)?;
    let repo = Path::new(text(&store.config, "repository")?);
    if !repo.is_dir() || fs::canonicalize(repo)? != repo {
        return Err("canonical_repository_required".into());
    }
    let exe = std::env::current_exe()?;
    let digest = executable(&exe)?;
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
    Ok(())
}
fn load(plan_path: &Path) -> Result<Object> {
    let p = object(&read(plan_path)?)?;
    if p.get("version") != Some(&json!(1)) || Path::new(text(&p, "plan")?) != plan_path {
        return Err("invalid_plan".into());
    }
    if p.get("entry") != Some(&entry(plan_path, text(&p, "executable")?)?) {
        return Err("invalid_entry".into());
    }
    Ok(p)
}
fn validate(p: &Object) -> Result<Store> {
    if !cfg!(target_os = "linux") {
        return Err("linux_only".into());
    }
    if executable(Path::new(text(p, "executable")?))? != text(p, "executable_sha256")? {
        return Err("executable_changed".into());
    }
    let store = Store::open(Path::new(text(p, "state")?), None)?;
    if p.get("scope") != Some(&Value::Object(store.config.clone())) {
        return Err("scope_changed".into());
    }
    Ok(store)
}
pub fn apply(plan_path: &Path) -> Result<()> {
    let p = load(plan_path)?;
    let path = Path::new(text(&p, "settings")?);
    let _lock = lock(path, false)?;
    validate(&p)?;
    let (raw, mut doc) = settings(path)?;
    if owned(&doc, &p["entry"]).is_ok() {
        return Ok(());
    }
    if hash(&raw) != text(&p, "settings_sha256")? {
        return Err("stale_plan".into());
    }
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
    replace(path, &doc, &raw)
}
pub fn remove(plan_path: &Path) -> Result<()> {
    let p = load(plan_path)?;
    let path = Path::new(text(&p, "settings")?);
    let _lock = lock(path, false)?;
    let (raw, mut doc) = settings(path)?;
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
    replace(path, &doc, &raw)
}

pub fn normalize(raw: &[u8], config: &Object) -> Result<Vec<u8>> {
    if raw.len() > MAX_INPUT {
        return Err("input_too_large".into());
    }
    let data = object(raw)?;
    let event = text(&data, "hook_event_name")?;
    let session = text(&data, "session_id")?;
    if !EVENTS.contains(&event) || !token(session) {
        return Err("invalid_event".into());
    }
    let cwd = text(&data, "cwd")?;
    if cwd.len() > 4096 || !Path::new(cwd).is_absolute() {
        return Err("invalid_cwd".into());
    }
    let repository = Path::new(text(config, "repository")?);
    if !repository.is_dir()
        || !Path::new(cwd).is_dir()
        || fs::canonicalize(repository)? != repository
        || !fs::canonicalize(cwd)?.starts_with(repository)
    {
        return Err("outside_scope".into());
    }
    let mut out = object(&serde_json::to_vec(
        &json!({"schema_version": 1, "installation_id": config["installation_id"], "runtime": "claude-code", "session_id": session, "event": event, "repository": config["repository"], "actor_ref": null, "attribution_basis": "unknown", "observed_at": SystemTime::now().duration_since(UNIX_EPOCH)?.as_secs_f64()}),
    )?)?;
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
    serde_json::to_vec(&out).map_err(Into::into)
}
pub fn hook(plan_path: &Path, raw: &[u8]) -> Result<()> {
    let p = load(plan_path)?;
    let path = Path::new(text(&p, "settings")?);
    let _lock = lock(path, true)?;
    let (_, doc) = settings(path)?;
    owned(&doc, &p["entry"])?;
    let mut store = validate(&p)?;
    let normalized = normalize(raw, &store.config)?;
    // A racing duplicate has the same UUID but a new observation timestamp.
    // Store's exact-byte conflict is deliberately ignored by the host-neutral
    // entrypoint, preserving the first observation without rewriting evidence.
    store.collect(&normalized)
}
