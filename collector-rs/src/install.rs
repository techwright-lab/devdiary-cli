//! One-command install: pair, pin a versioned binary, register the hooks only
//! after consent, then deliver the spool from a systemd user timer.
use crate::{
    Object, Result, claude, drops, object, orchestrators, pairing,
    store::{Report, Store, private},
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
    pub trust_origin: bool,
    pub settings: Option<&'a Path>,
    pub record_orchestrated_outside_repos: bool,
    pub yes: bool,
    pub browser: bool,
    pub new_pair: bool,
}

fn home() -> Result<PathBuf> {
    std::env::var_os("HOME")
        .map(PathBuf::from)
        .filter(|h| h.is_absolute())
        .ok_or_else(|| "home_required".into())
}
// Fixed under HOME, not XDG_DATA_HOME: a shell without that variable must still
// find the same install record, connection and pinned binary.
fn data_dir() -> Result<PathBuf> {
    Ok(home()?.join(".local/share/devdiary/collector"))
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
fn pinned(data: &Path) -> PathBuf {
    data.join(VERSION).join(BINARY)
}
fn pin_binary(data: &Path) -> Result<PathBuf> {
    let dir = data.join(VERSION);
    private_dir(&dir)?;
    let target = pinned(data);
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
        Some(path) if path.is_absolute() => path.to_path_buf(),
        Some(_) => return Err("unsupported_path".into()),
        None => home()?.join(".claude/settings.json"),
    };
    if record(&data)?.is_some() {
        return Err("already_installed".into());
    }
    let origin = o.origin.unwrap_or(pairing::DEFAULT_ORIGIN);
    pairing::origin(origin, o.trust_origin)?;
    claude::preflight(&settings)?;
    private_dir(&data)?;
    reuse_or_retire(&data, o.new_pair)?;
    pairing::setup(pairing::Options {
        root: &connection(&data),
        repository: None,
        remote: None,
        origin,
        trust_origin: o.trust_origin,
        browser: o.browser,
        new_pair: o.new_pair,
        record_orchestrated_outside_repos: o.record_orchestrated_outside_repos,
        next_steps: false,
    })
    .map_err(setup_failure)?;
    let exe = pin_binary(&data)?;
    let registration = Registration::new(&data, &settings, &exe)?;
    registration.plan()?;
    registration.show()?;
    if let Err(error) = consent(
        o.yes,
        "Append these 9 entries and enable the devdiary-collector-sync user timer?",
    ) {
        let _ = fs::remove_file(&registration.plan);
        return Err(error);
    }
    registration.finish(runner)?;
    offer_orchestrators(&data)
}
fn revoked(data: &Path) -> bool {
    pairing::revoked(&connection(data).join("outbox"))
}
// A revoked connection is set aside rather than deleted so its pending rows stay
// on disk; a working one is never replaced, since install can reuse it.
fn reuse_or_retire(data: &Path, new_pair: bool) -> Result<()> {
    let saved = connection(data).join("connection.json").is_file();
    match (revoked(data), new_pair) {
        (true, false) => Err("connection_revoked".into()),
        (false, true) if saved => Err("connection_active".into()),
        (true, true) => {
            let nanos = SystemTime::now().duration_since(UNIX_EPOCH)?.as_nanos();
            fs::rename(
                connection(data),
                data.join(format!("connection-revoked-{nanos}")),
            )?;
            Ok(File::open(data)?.sync_all()?)
        }
        (false, _) => Ok(()),
    }
}
fn setup_failure(error: Box<dyn std::error::Error>) -> Box<dyn std::error::Error> {
    if pairing::known_diagnostic(error.as_ref()).is_some() {
        error
    } else {
        "setup_incomplete".into()
    }
}

struct Registration<'a> {
    data: &'a Path,
    settings: &'a Path,
    exe: &'a Path,
    plan: PathBuf,
}
impl<'a> Registration<'a> {
    fn new(data: &'a Path, settings: &'a Path, exe: &'a Path) -> Result<Self> {
        let plans = data.join("plans");
        private_dir(&plans)?;
        let nanos = SystemTime::now().duration_since(UNIX_EPOCH)?.as_nanos();
        let plan = plans.join(format!("claude-{nanos}.json"));
        Ok(Self {
            data,
            settings,
            exe,
            plan,
        })
    }
    fn show(&self) -> Result<()> {
        println!("Hook entries to append to {}:", self.settings.display());
        for line in claude::entry_lines(&self.plan, self.exe)? {
            println!("  {line}");
        }
        Ok(())
    }
    // Consent is given to the lines `show` printed, so the saved plan must match them.
    fn plan(&self) -> Result<()> {
        pairing::plan_with(&connection(self.data), self.settings, &self.plan, self.exe)?;
        if claude::entries(&self.plan)? != claude::entry_lines(&self.plan, self.exe)? {
            let _ = fs::remove_file(&self.plan);
            return Err("stale_plan".into());
        }
        Ok(())
    }
    fn finish(&self, runner: &dyn Runner) -> Result<()> {
        let (data, exe, plan) = (self.data, self.exe, &self.plan);
        claude::apply(plan)?;
        save(
            &data.join(RECORD),
            &json!({"version": VERSION, "executable": exe, "plan": plan, "settings": self.settings}),
        )?;
        systemd::enable(runner, &systemd::unit_dir()?, exe, &connection(data))?;
        self_test(exe, plan, &connection(data).join("outbox"))?;
        println!(
            "Self-test passed: the installed hook recorded a local test event that is never uploaded.\nThe timer syncs every 2 minutes. Run `devdiary-collector doctor` to check it."
        );
        Ok(())
    }
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

// The live hooks run the pinned path, so it is overwritten only after consent and
// removal. The old record stays until replaced, so uninstall still works on failure.
pub fn upgrade(yes: bool, runner: &dyn Runner) -> Result<()> {
    require_consent_channel(yes)?;
    let data = data_dir()?;
    let old = record(&data)?.ok_or("not_installed")?;
    if revoked(&data) {
        return Err("connection_revoked".into());
    }
    claude::preflight_replacing(&old.plan)?;
    let exe = pinned(&data);
    let registration = Registration::new(&data, &old.settings, &exe)?;
    registration.show()?;
    consent(
        yes,
        &format!(
            "Replace the hook entries of version {} with these 9 entries of version {VERSION}?",
            old.version
        ),
    )?;
    claude::remove(&old.plan)?;
    pin_binary(&data)?;
    registration.plan()?;
    registration.finish(runner)?;
    offer_orchestrators(&data)
}

fn ask_orchestrators(outbox: &Path) -> Result<()> {
    let (entries, refused) = orchestrators::pending(outbox)?;
    println!("Environment names the hook would read to recognise orchestrated runs:");
    for line in orchestrators::describe(&entries) {
        println!("  {line}");
    }
    if entries.is_empty() {
        println!("  (none: the hook would stop reading orchestrator names)");
    }
    if refused > 0 {
        println!("{refused} more refused: a name is not allowed or the entry is malformed.");
    }
    println!("IDs that are UUIDs are sent to the server. Paths stay on this machine.");
    consent(false, "Let the hook read these names?")?;
    orchestrators::approve(outbox, &entries)?;
    println!("Approved.");
    Ok(())
}
// `--yes` covers the hook entries the person chose to skip reviewing. It cannot
// cover names the server picks later, so only a terminal answer approves them.
fn offer_orchestrators(data: &Path) -> Result<()> {
    let root = connection(data);
    let outbox = root.join("outbox");
    let _ = pairing::refresh_scope(&root);
    if !orchestrators::waiting(&outbox)? {
        return Ok(());
    }
    if !std::io::stdin().is_terminal() {
        println!(
            "Orchestrator names wait for approval. In a terminal run: devdiary-collector orchestrators approve"
        );
        return Ok(());
    }
    match ask_orchestrators(&outbox) {
        Err(error) if error.to_string() == "consent_declined" => {
            println!(
                "No names approved. Hooks stay registered. Run `devdiary-collector orchestrators approve` to answer again."
            );
            Ok(())
        }
        other => other,
    }
}
pub fn approve_orchestrators(root: Option<&Path>) -> Result<()> {
    if !std::io::stdin().is_terminal() {
        return Err("approval_required".into());
    }
    let outbox = match root {
        Some(root) => root.join("outbox"),
        None => connection(&data_dir()?).join("outbox"),
    };
    if !orchestrators::waiting(&outbox)? {
        println!("No orchestrator change waits for approval.");
        return Ok(());
    }
    ask_orchestrators(&outbox)
}

pub fn uninstall(runner: &dyn Runner) -> Result<()> {
    let data = data_dir()?;
    let old = record(&data)?;
    let timer = systemd::disable(runner, &systemd::unit_dir()?);
    if let Some(old) = &old {
        claude::remove(&old.plan)?;
        fs::remove_file(data.join(RECORD))?;
        File::open(&data)?.sync_all()?;
    }
    let units = timer?;
    match (old.is_some(), units) {
        (false, false) => println!("Not installed; nothing changed."),
        (false, true) => {
            println!("Not installed; the leftover sync timer was disabled and removed.")
        }
        (true, _) => println!(
            "Hooks removed and the sync timer disabled. The connection and any pending rows stay in {}; revoke the connection in the browser to retire its credential.",
            connection(&data).display()
        ),
    }
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
            "orchestrators": orchestrators::summary(&outbox)?,
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
    match drain(|limit| pairing::sync_report(root, limit)) {
        Ok(refusal) => note_sync(root, refusal),
        Err(error) => {
            let code = code(error.as_ref());
            note_sync(root, Some(code.clone()))?;
            match code.as_str() {
                "revoked" | "sync_rate_limited" => Ok(()),
                _ => Err(error),
            }
        }
    }
}
// Rows the server refuses stay pending and the cursor wraps, so one run visits
// each row that was pending when it started at most once.
fn drain(mut batch: impl FnMut(usize) -> Result<Report>) -> Result<Option<String>> {
    let (mut visited, mut budget, mut refusal) = (0, BATCH, None);
    for round in 0..BATCHES {
        let limit = BATCH.min(budget - visited);
        let report = batch(limit)?;
        if report.stopped.is_some() {
            return Ok(report.stopped);
        }
        refusal = report.refusal.or(refusal);
        visited += report.attempted;
        if round == 0 {
            budget = report.pending as usize + report.attempted - report.refused;
        }
        if report.pending == 0 || report.attempted < limit || visited >= budget {
            break;
        }
    }
    Ok(refusal)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn report(pending: i64, attempted: usize, refused: usize) -> Report {
        Report {
            pending,
            delivered: 0,
            attempted,
            stopped: None,
            refused,
            refusal: (refused > 0).then(|| "http_422".into()),
        }
    }

    fn run(mut pending: i64, refuse: bool, stop_at: Option<usize>) -> (Vec<usize>, Option<String>) {
        let mut limits = Vec::new();
        let result = drain(|limit| {
            limits.push(limit);
            if stop_at == Some(limits.len()) {
                return Ok(Report {
                    stopped: Some("http_503".into()),
                    ..report(pending, 1, 1)
                });
            }
            let attempted = limit.min(pending as usize);
            if refuse {
                return Ok(report(pending, attempted, attempted));
            }
            pending -= attempted as i64;
            Ok(report(pending, attempted, 0))
        })
        .unwrap();
        (limits, result)
    }

    #[test]
    fn a_run_sends_at_most_ten_batches_of_one_hundred() {
        assert_eq!(run(1005, false, None), (vec![100; 10], None));
    }

    #[test]
    fn a_run_ends_when_the_spool_is_empty() {
        assert_eq!(run(150, false, None), (vec![100, 50], None));
        assert_eq!(run(200, false, None), (vec![100, 100], None));
    }

    #[test]
    fn refused_rows_are_visited_once_and_reported() {
        assert_eq!(
            run(120, true, None),
            (vec![100, 20], Some("http_422".into()))
        );
        assert_eq!(run(100, true, None), (vec![100], Some("http_422".into())));
    }

    #[test]
    fn a_stop_class_failure_ends_the_run_with_its_code() {
        assert_eq!(
            run(500, false, Some(2)),
            (vec![100, 100], Some("http_503".into()))
        );
    }

    #[test]
    fn a_local_failure_propagates() {
        assert!(drain(|_| Err("disk".into())).is_err());
    }
}
