//! One-command install: pair, pin a versioned binary, register the hooks only
//! after consent, then deliver the spool from a systemd user timer.
use crate::{
    Object, Result, claude, drops, object, pairing,
    store::{Store, private},
    systemd::{self, Runner},
    text,
};
use serde_json::{Value, json};
use std::{
    fs::{self, DirBuilder, File, OpenOptions},
    io::{BufRead, IsTerminal, Read, Write},
    os::unix::fs::{DirBuilderExt, OpenOptionsExt},
    path::{Path, PathBuf},
    process::{Command, Stdio},
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};
use uuid::Uuid;

const VERSION: &str = env!("CARGO_PKG_VERSION");
const BINARY: &str = "devdiary-collector";
const RECORD: &str = "install.json";
const SYNC_STATUS: &str = "sync-status.json";
const BATCH: usize = 100;
const BATCHES: usize = 10;

pub struct Options<'a> {
    pub origin: Option<&'a str>,
    pub settings: Option<&'a Path>,
    pub record_orchestrated_outside_repos: bool,
    pub yes: bool,
    pub browser: bool,
}

fn home() -> Result<PathBuf> {
    std::env::var_os("HOME")
        .map(PathBuf::from)
        .filter(|h| h.is_absolute())
        .ok_or_else(|| "home_required".into())
}
fn data_dir() -> Result<PathBuf> {
    let base = match std::env::var_os("XDG_DATA_HOME").map(PathBuf::from) {
        Some(p) if p.is_absolute() => p,
        _ => home()?.join(".local/share"),
    };
    Ok(base.join("devdiary/collector"))
}
fn connection(data: &Path) -> PathBuf {
    data.join("connection")
}
fn private_dir(path: &Path) -> Result<()> {
    if !path.exists() {
        fs::create_dir_all(path.parent().ok_or("invalid_path")?)?;
        DirBuilder::new().mode(0o700).create(path)?;
    }
    private(path, true)
}
fn save(path: &Path, value: &Value) -> Result<()> {
    let parent = path.parent().ok_or("invalid_path")?;
    let temporary = parent.join(format!(".{}.tmp", Uuid::new_v4()));
    let result = (|| -> Result<()> {
        let mut file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(&temporary)?;
        file.write_all(&serde_json::to_vec(value)?)?;
        file.sync_all()?;
        fs::rename(&temporary, path)?;
        File::open(parent)?.sync_all()?;
        Ok(())
    })();
    let _ = fs::remove_file(&temporary);
    result
}
fn load(path: &Path) -> Result<Option<Object>> {
    if !path.exists() {
        return Ok(None);
    }
    private(path, false)?;
    let mut raw = Vec::new();
    File::open(path)?.take(16385).read_to_end(&mut raw)?;
    Ok(Some(object(&raw)?))
}

struct Record {
    settings: PathBuf,
    plan: PathBuf,
    version: String,
}
fn record(data: &Path) -> Result<Option<Record>> {
    let Some(doc) = load(&data.join(RECORD))? else {
        return Ok(None);
    };
    Ok(Some(Record {
        settings: text(&doc, "settings")?.into(),
        plan: text(&doc, "plan")?.into(),
        version: text(&doc, "version")?.into(),
    }))
}

// The registered hook must survive `cargo clean` and rebuilds, so it runs a
// private copy whose path names the version.
fn pin_binary(data: &Path) -> Result<PathBuf> {
    let dir = data.join(VERSION);
    private_dir(&dir)?;
    let target = dir.join(BINARY);
    let source = std::env::current_exe()?;
    if fs::canonicalize(&source)? == target {
        return Ok(target);
    }
    let temporary = dir.join(format!(".{BINARY}-{}.tmp", Uuid::new_v4()));
    let result = (|| -> Result<()> {
        let mut out = OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o700)
            .open(&temporary)?;
        std::io::copy(&mut File::open(&source)?, &mut out)?;
        out.sync_all()?;
        fs::rename(&temporary, &target)?;
        File::open(&dir)?.sync_all()?;
        Ok(())
    })();
    let _ = fs::remove_file(&temporary);
    result.map(|_| target)
}

fn require_consent_channel(yes: bool) -> Result<()> {
    if !yes && !std::io::stdin().is_terminal() {
        return Err("consent_required".into());
    }
    Ok(())
}
fn consent(yes: bool, question: &str) -> Result<()> {
    if yes {
        return Ok(());
    }
    require_consent_channel(yes)?;
    print!("{question} [y/N] ");
    std::io::stdout().flush()?;
    let mut line = String::new();
    std::io::stdin().lock().take(64).read_line(&mut line)?;
    if matches!(line.trim(), "y" | "Y" | "yes") {
        Ok(())
    } else {
        Err("consent_declined".into())
    }
}

pub fn install(o: Options<'_>, runner: &dyn Runner) -> Result<()> {
    require_consent_channel(o.yes)?;
    let data = data_dir()?;
    let settings = match o.settings {
        Some(path) => path.to_path_buf(),
        None => home()?.join(".claude/settings.json"),
    };
    if record(&data)?.is_some() {
        return Err("already_installed".into());
    }
    claude::preflight(&settings)?;
    private_dir(&data)?;
    pairing::setup(pairing::Options {
        root: &connection(&data),
        repository: None,
        remote: None,
        origin: o.origin.unwrap_or(pairing::DEFAULT_ORIGIN),
        trust_origin: o.origin.is_some(),
        browser: o.browser,
        new_pair: false,
        record_orchestrated_outside_repos: o.record_orchestrated_outside_repos,
        next_steps: false,
    })?;
    let exe = pin_binary(&data)?;
    register(&data, &settings, &exe, o.yes, runner)
}

fn register(
    data: &Path,
    settings: &Path,
    exe: &Path,
    yes: bool,
    runner: &dyn Runner,
) -> Result<()> {
    let plans = data.join("plans");
    private_dir(&plans)?;
    let nanos = SystemTime::now().duration_since(UNIX_EPOCH)?.as_nanos();
    let plan = plans.join(format!("claude-{nanos}.json"));
    pairing::plan_with(&connection(data), settings, &plan, exe)?;
    println!("Hook entries to append to {}:", settings.display());
    for line in claude::entries(&plan)? {
        println!("  {line}");
    }
    if let Err(error) = consent(
        yes,
        "Append these 9 entries and enable the devdiary-collector-sync user timer?",
    ) {
        let _ = fs::remove_file(&plan);
        return Err(error);
    }
    claude::apply(&plan)?;
    save(
        &data.join(RECORD),
        &json!({"version": VERSION, "executable": exe, "plan": plan, "settings": settings}),
    )?;
    systemd::enable(runner, &systemd::unit_dir()?, exe, &connection(data))?;
    self_test(exe, &plan, &connection(data).join("outbox"))?;
    println!(
        "Self-test passed: the installed hook recorded a local test event that is never uploaded.\nThe timer syncs every 2 minutes. Run `devdiary-collector doctor` to check it."
    );
    Ok(())
}

fn self_test(exe: &Path, plan: &Path, outbox: &Path) -> Result<()> {
    let session = format!("{}{}", claude::SELF_TEST, Uuid::new_v4());
    let event = json!({"hook_event_name": "SessionStart", "session_id": session, "cwd": "/"});
    let mut child = Command::new(exe)
        .arg("claude-hook")
        .arg(plan)
        .env_clear()
        .stdin(Stdio::piped())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()?;
    if let Some(mut stdin) = child.stdin.take() {
        let _ = stdin.write_all(&serde_json::to_vec(&event)?);
    }
    let deadline = Instant::now() + Duration::from_secs(5);
    while child.try_wait()?.is_none() && Instant::now() < deadline {
        std::thread::sleep(Duration::from_millis(20));
    }
    if child.try_wait()?.is_none() {
        let _ = child.kill();
        let _ = child.wait();
    }
    if !Store::open(outbox, None)?.self_tested(&session)? {
        return Err("self_test_failed".into());
    }
    Ok(())
}

pub fn upgrade(yes: bool, runner: &dyn Runner) -> Result<()> {
    require_consent_channel(yes)?;
    let data = data_dir()?;
    let old = record(&data)?.ok_or("not_installed")?;
    consent(
        yes,
        &format!(
            "Replace the hook entries of version {} with version {VERSION}?",
            old.version
        ),
    )?;
    let exe = pin_binary(&data)?;
    claude::remove(&old.plan)?;
    fs::remove_file(data.join(RECORD))?;
    register(&data, &old.settings, &exe, true, runner)
}

pub fn uninstall(runner: &dyn Runner) -> Result<()> {
    let data = data_dir()?;
    let Some(old) = record(&data)? else {
        println!("Not installed; nothing changed.");
        return Ok(());
    };
    systemd::disable(runner, &systemd::unit_dir()?)?;
    claude::remove(&old.plan)?;
    fs::remove_file(data.join(RECORD))?;
    File::open(&data)?.sync_all()?;
    println!(
        "Hooks removed and the sync timer disabled. The connection and any pending rows stay in {}; revoke the connection in the browser to retire its credential.",
        connection(&data).display()
    );
    Ok(())
}

pub fn doctor(runner: &dyn Runner) -> Result<()> {
    let data = data_dir()?;
    let Some(current) = record(&data)? else {
        println!(
            "{}",
            json!({"installed": false, "timer": systemd::state(runner)})
        );
        return Ok(());
    };
    let outbox = connection(&data).join("outbox");
    let store = Store::open(&outbox, None)?;
    let (pending, delivered) = store.counts()?;
    let receipt = store
        .last_receipt()?
        .map(|raw| serde_json::from_slice::<Value>(&raw))
        .transpose()?;
    let status = load(&connection(&data).join(SYNC_STATUS))?;
    println!(
        "{}",
        json!({
            "installed": true,
            "version": current.version,
            "registration": claude::registration_status(&current.plan, &outbox)?,
            "timer": systemd::state(runner),
            "pending": pending,
            "delivered": delivered,
            "drops": drops::counts(&store, &outbox)?,
            "revoked": connection(&data).join("revoked").exists(),
            "last_receipt": receipt,
            "last_sync_error": status.and_then(|s| s.get("last_sync_error").cloned()),
        })
    );
    Ok(())
}

// Codes only: errors from lower layers can carry paths or server text.
fn code(error: &dyn std::error::Error) -> String {
    let s = error.to_string();
    let known = ["revoked", "sync_rate_limited", "repair_required"];
    if known.contains(&s.as_str()) {
        s
    } else {
        "error".into()
    }
}
fn note_sync(root: &Path, error: Option<String>) -> Result<()> {
    let at = SystemTime::now().duration_since(UNIX_EPOCH)?.as_secs();
    save(
        &root.join(SYNC_STATUS),
        &json!({"last_sync_error": error, "at": at}),
    )
}
/// Server refusals end the run quietly and are left for `doctor`; only local
/// failures make the service fail.
pub fn timer_sync(root: &Path) -> Result<()> {
    for _ in 0..BATCHES {
        let report = match pairing::sync_report(root, BATCH) {
            Ok(report) => report,
            Err(error) => {
                let code = code(error.as_ref());
                note_sync(root, Some(code.clone()))?;
                return match code.as_str() {
                    "revoked" | "sync_rate_limited" => Ok(()),
                    _ => Err(error),
                };
            }
        };
        if report.stopped.is_some() {
            return note_sync(root, report.stopped);
        }
        if report.pending == 0 || report.attempted < BATCH {
            break;
        }
    }
    note_sync(root, None)
}
