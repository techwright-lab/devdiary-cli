//! One-command install: check the machine, pair, pin a versioned binary,
//! register the hooks only after consent, then deliver the spool from the
//! scheduler: a systemd user timer on Linux, a launchd agent on macOS.
use crate::{
    Object, Result, claude, drops, object, orchestrators, pairing, platform,
    scheduler::{Os, Scheduler},
    store::{Report, Store, private},
    text,
    vendor::Vendor,
};
use serde_json::{Value, json};
use std::{
    fs::{self, DirBuilder, File, OpenOptions},
    io::{BufRead, IsTerminal, Read, Write},
    os::unix::fs::{DirBuilderExt, OpenOptionsExt, PermissionsExt},
    path::{Path, PathBuf},
    process::{Command, Stdio},
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};
use uuid::Uuid;

const VERSION: &str = env!("CARGO_PKG_VERSION");
const BINARY: &str = "devdiary";
const RECORD: &str = "install.json";
const SYNC_STATUS: &str = "sync-status.json";
const BATCH: usize = 100;
const INTEGRATIONS: &str =
    "https://github.com/techwright-lab/devdiary-cli/blob/main/docs/integrations/README.md";
const OUTSIDE_ON: &str = "Your sessions outside repositories are now recorded: event, time and tool name, never a path. Temp folders stay out.";
const BATCHES: usize = 10;

pub struct Options<'a> {
    pub vendor: Vendor,
    pub origin: Option<&'a str>,
    pub trust_origin: bool,
    pub settings: Option<&'a Path>,
    pub record_orchestrated_outside_repos: bool,
    pub browser: bool,
}

fn home() -> Result<PathBuf> {
    std::env::var_os("HOME")
        .map(PathBuf::from)
        .filter(|h| h.is_absolute())
        .ok_or_else(|| "home_required".into())
}
// Fixed under HOME, not XDG_DATA_HOME: a shell without that variable must still
// find the same install record, connection and pinned binary. Never the old
// devdiary-collector folder: setup pairs again rather than adopt its connection.
fn data_dir(vendor: Vendor) -> Result<PathBuf> {
    Ok(home()?
        .join(".local/share/devdiary/cli")
        .join(vendor.slug()))
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
    save_using(path, value, true)
}
fn save_using(path: &Path, value: &Value, overwrite: bool) -> Result<()> {
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
        if overwrite {
            fs::rename(&temporary, path)?;
        } else {
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

// The install script runs setup by path, so an older `devdiary` earlier on PATH
// would still answer every later command the person types.
fn warn_other_on_path() {
    let (Ok(me), Some(path)) = (
        std::env::current_exe().and_then(fs::canonicalize),
        std::env::var_os("PATH"),
    ) else {
        return;
    };
    let first = std::env::split_paths(&path)
        .map(|dir| dir.join(BINARY))
        .find(|p| {
            fs::metadata(p).is_ok_and(|m| m.is_file() && m.permissions().mode() & 0o111 != 0)
        });
    if let Some(other) = first.filter(|p| fs::canonicalize(p).ok().as_deref() != Some(&*me)) {
        eprintln!(
            "Warning: {} comes first on PATH, so typing `devdiary` runs that program, not this one. Setup continues.",
            other.display()
        );
    }
}
pub fn setup(o: Options<'_>, scheduler: &Scheduler<'_>) -> Result<()> {
    warn_other_on_path();
    require_consent_channel(false)?;
    platform::preflight(scheduler)?;
    let data = data_dir(o.vendor)?;
    let settings = match o.settings {
        Some(path) if path.is_absolute() => path.to_path_buf(),
        Some(_) => return Err("unsupported_path".into()),
        None if o.vendor == Vendor::Codex => match std::env::var_os("CODEX_HOME") {
            Some(root) if Path::new(&root).is_absolute() => PathBuf::from(root).join("hooks.json"),
            Some(_) => return Err("unsupported_path".into()),
            None => home()?.join(".codex/hooks.json"),
        },
        None => home()?.join(".claude/settings.json"),
    };
    if record(&data)?.is_some() {
        return Err("already_installed".into());
    }
    let origin = o.origin.unwrap_or(pairing::DEFAULT_ORIGIN);
    pairing::origin(origin, o.trust_origin)?;
    if o.vendor == Vendor::Codex && settings.extension().is_some_and(|e| e == "toml") {
        return Err("codex_hooks_json_required".into());
    }
    offer_restore(&settings)?;
    // Codex need not have a hooks file yet. Never initialize or rewrite config.toml.
    if o.vendor == Vendor::Codex && !settings.exists() {
        consent(
            false,
            &format!(
                "Create an empty Codex hooks file at {}?",
                settings.display()
            ),
        )?;
        let parent = settings.parent().ok_or("invalid_path")?;
        if !parent.exists() {
            private_dir(parent)?;
        }
        crate::store::owner_writable(parent, true)?;
        save_using(&settings, &json!({}), false)?;
    }
    let agreed = claude::preflight(&settings)?;
    if revoked(&data) {
        return Err("connection_revoked".into());
    }
    // The entries depend only on the pinned path, the plan path and the harness,
    // so the person agrees to them before a connection exists.
    let exe = pinned(&data);
    let registration = Registration::new(&data, &settings, &exe, o.vendor)?;
    let shown = registration.show()?;
    consent(false, &start_question(scheduler.os, o.vendor))?;
    private_dir(&data)?;
    pairing::setup(pairing::Options {
        vendor: o.vendor,
        root: &connection(&data),
        origin,
        trust_origin: o.trust_origin,
        browser: o.browser,
        record_orchestrated_outside_repos: o.record_orchestrated_outside_repos,
    })
    .map_err(setup_failure)?;
    pin_binary(&data)?;
    registration.plan(&shown, Some(&agreed))?;
    registration.finish(scheduler, claude::apply_with_backup)?;
    offer_orchestrators(&data)
}
// Asked before the settings are read: the copy holds them as they were before
// an interrupted write.
fn offer_restore(settings: &Path) -> Result<()> {
    let backup = claude::backup(settings);
    if fs::symlink_metadata(&backup).is_err() {
        return Ok(());
    }
    println!(
        "An earlier setup stopped while it wrote {}. Its copy of the file from before that run is {}.",
        settings.display(),
        backup.display()
    );
    match consent(false, "Restore the copy? It replaces the current file.") {
        Ok(()) => {
            claude::restore(settings)?;
            println!("Restored {}.", settings.display());
            Ok(())
        }
        Err(error) if error.to_string() == "consent_declined" => Err("backup_kept".into()),
        Err(error) => Err(error),
    }
}
fn revoked(data: &Path) -> bool {
    pairing::revoked(&connection(data).join("outbox"))
}
fn setup_failure(error: Box<dyn std::error::Error>) -> Box<dyn std::error::Error> {
    if pairing::known_diagnostic(error.as_ref()).is_some() {
        error
    } else {
        "setup_incomplete".into()
    }
}

struct Registration<'a> {
    vendor: Vendor,
    data: &'a Path,
    settings: &'a Path,
    exe: &'a Path,
    plan: PathBuf,
}
impl<'a> Registration<'a> {
    fn new(data: &'a Path, settings: &'a Path, exe: &'a Path, vendor: Vendor) -> Result<Self> {
        let plans = data.join("plans");
        let nanos = SystemTime::now().duration_since(UNIX_EPOCH)?.as_nanos();
        let plan = plans.join(format!("{}-{nanos}.json", vendor.slug()));
        Ok(Self {
            vendor,
            data,
            settings,
            exe,
            plan,
        })
    }
    fn show(&self) -> Result<Vec<String>> {
        println!("Hook entries to append to {}:", self.settings.display());
        let lines = claude::entry_lines_for(&self.plan, self.exe, self.vendor)?;
        for line in &lines {
            println!("  {line}");
        }
        Ok(lines)
    }
    // Consent is given to the lines `show` printed and, during setup, to the
    // settings as they were then, so the saved plan must match both.
    fn plan(&self, shown: &[String], agreed: Option<&str>) -> Result<()> {
        private_dir(self.plan.parent().ok_or("invalid_path")?)?;
        pairing::plan_with(
            &connection(self.data),
            self.settings,
            &self.plan,
            self.exe,
            agreed,
        )?;
        if claude::entries(&self.plan)? != shown {
            let _ = fs::remove_file(&self.plan);
            return Err("stale_plan".into());
        }
        Ok(())
    }
    fn finish(&self, scheduler: &Scheduler<'_>, write: fn(&Path) -> Result<()>) -> Result<()> {
        let (data, exe, plan) = (self.data, self.exe, &self.plan);
        write(plan)?;
        if self.vendor == Vendor::Codex {
            println!(
                "Codex: review and trust these exact definitions in /hooks before capture can run. An upgrade changes definitions and requires review again. Stock runtime qualification is unverified."
            );
        }
        save(
            &data.join(RECORD),
            &json!({"version": VERSION, "executable": exe, "plan": plan, "settings": self.settings}),
        )?;
        scheduler.enable(exe, &connection(data), self.vendor)?;
        self_test(exe, plan, &connection(data).join("outbox"), self.vendor)?;
        let job = match scheduler.os {
            Os::Linux => "The timer",
            Os::Macos => "The launch agent",
        };
        println!(
            "Self-test passed: the installed hook recorded a local test event that is never uploaded.\n{job} syncs every 2 minutes. Run `devdiary doctor` (add `--vendor codex` for Codex) to check it."
        );
        Ok(())
    }
}

fn self_test(exe: &Path, plan: &Path, outbox: &Path, vendor: Vendor) -> Result<()> {
    let session = format!("{}{}", claude::SELF_TEST, Uuid::new_v4());
    let event = json!({"hook_event_name": "SessionStart", "session_id": session, "cwd": "/"});
    let mut child = Command::new(exe)
        .arg("hook")
        .arg(vendor.slug())
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
pub fn upgrade(yes: bool, scheduler: &Scheduler<'_>, vendor: Vendor) -> Result<()> {
    require_consent_channel(yes)?;
    // The old hooks are removed before the scheduler reloads, so check first.
    platform::preflight(scheduler)?;
    let data = data_dir(vendor)?;
    let old = record(&data)?.ok_or("not_installed")?;
    if revoked(&data) {
        return Err("connection_revoked".into());
    }
    claude::preflight_replacing(&old.plan)?;
    let exe = pinned(&data);
    let registration = Registration::new(&data, &old.settings, &exe, vendor)?;
    let shown = registration.show()?;
    consent(
        yes,
        &format!(
            "Replace the hook entries of version {} with these 10 entries of version {VERSION}?",
            old.version
        ),
    )?;
    claude::remove(&old.plan)?;
    pin_binary(&data)?;
    registration.plan(&shown, None)?;
    registration.finish(scheduler, claude::apply)?;
    offer_orchestrators(&data)
}

fn ask_orchestrators(outbox: &Path) -> Result<()> {
    let (entries, refused) = orchestrators::pending(outbox)?;
    println!("Environment names the hook would read to recognise orchestrated runs:");
    for line in orchestrators::describe(&entries) {
        println!("  {line}");
    }
    show_changes(outbox, &entries);
    if entries.is_empty() {
        println!("  (none: the hook would stop reading orchestrator names)");
    }
    if refused > 0 {
        println!("{refused} more refused: a name is not allowed or the entry is malformed.");
    }
    println!("IDs that are UUIDs are sent to the server. Paths stay on this machine.");
    println!("Each tool may need its own setup, or its agents record nothing. See {INTEGRATIONS}");
    consent(false, "Let the hook read these names?")?;
    orchestrators::approve(outbox, &entries)?;
    println!("Approved.");
    Ok(())
}
fn show_changes(outbox: &Path, entries: &[Value]) {
    let Ok(old) = orchestrators::approved(outbox) else {
        println!("The saved approved list cannot be read. Approving replaces it.");
        return;
    };
    if old.is_empty() {
        return;
    }
    let (added, removed) = orchestrators::changes(&old, entries);
    if added.is_empty() && removed.is_empty() {
        println!("Only the order changed. The first entry that matches wins.");
    }
    for line in added {
        println!("  added {line}");
    }
    for line in removed {
        println!("  removed {line}");
    }
}
// `--yes` covers the hook entries the person chose to skip reviewing. It cannot
// cover names the server picks later, so only a terminal answer approves them.
fn offer_orchestrators(data: &Path) -> Result<()> {
    let root = connection(data);
    let outbox = root.join("outbox");
    pairing::refresh_scope(&root)?;
    if orchestrators::offer_unreadable(&outbox) {
        println!("The saved orchestrator offer cannot be read. The next sync replaces it.");
        return Ok(());
    }
    if !orchestrators::waiting(&outbox) {
        return Ok(());
    }
    if !std::io::stdin().is_terminal() {
        println!(
            "Orchestrator names wait for approval. In a terminal run: devdiary orchestrators approve"
        );
        return Ok(());
    }
    match ask_orchestrators(&outbox) {
        Err(error) if error.to_string() == "consent_declined" => {
            println!(
                "No names approved. Hooks stay registered. Run `devdiary orchestrators approve` to answer again."
            );
            Ok(())
        }
        other => other,
    }
}
pub fn outside_repos(on: bool, vendor: Vendor) -> Result<()> {
    if on && !std::io::stdin().is_terminal() {
        return Err("approval_required".into());
    }
    crate::outside::set(&connection(&data_dir(vendor)?).join("outbox"), on)?;
    println!(
        "{}",
        if on {
            OUTSIDE_ON
        } else {
            "Sessions outside repositories are no longer recorded."
        }
    );
    Ok(())
}

pub fn approve_orchestrators(root: Option<&Path>, vendor: Vendor) -> Result<()> {
    if !std::io::stdin().is_terminal() {
        return Err("approval_required".into());
    }
    let outbox = match root {
        Some(root) => root.join("outbox"),
        None => connection(&data_dir(vendor)?).join("outbox"),
    };
    orchestrators::pending(&outbox)?;
    if !orchestrators::waiting(&outbox) {
        println!("No orchestrator change waits for approval.");
        return Ok(());
    }
    ask_orchestrators(&outbox)
}

pub fn uninstall(scheduler: &Scheduler<'_>, vendor: Vendor) -> Result<()> {
    let data = data_dir(vendor)?;
    let old = record(&data)?;
    let timer = scheduler.disable(vendor);
    if let Some(old) = &old {
        claude::remove(&old.plan)?;
        fs::remove_file(data.join(RECORD))?;
        File::open(&data)?.sync_all()?;
    }
    let units = timer?;
    let (job, stopped) = match scheduler.os {
        Os::Linux => ("sync timer", "disabled"),
        Os::Macos => ("launch agent", "unloaded"),
    };
    match (old.is_some(), units) {
        (false, false) => println!("Not installed; nothing changed."),
        (false, true) => {
            println!("Not installed; the leftover {job} was {stopped} and removed.")
        }
        (true, _) => println!(
            "Hooks removed and the {job} {stopped}. The connection and any pending rows stay in {}; revoke the connection in the browser to retire its credential.",
            connection(&data).display()
        ),
    }
    Ok(())
}

pub fn doctor(scheduler: &Scheduler<'_>, vendor: Vendor) -> Result<()> {
    let data = data_dir(vendor)?;
    let Some(current) = record(&data)? else {
        println!(
            "{}",
            json!({"installed": false, "timer": scheduler.state(vendor)})
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
    let orchestrators = orchestrators::summary(&outbox);
    if let Some(notice) = orchestrators["notice"].as_str() {
        eprintln!("{notice}");
    }
    println!(
        "{}",
        json!({
            "installed": true,
            "runtime": vendor.runtime(),
            "host_trust": "unknown",
            "runtime_qualification": "unverified",
            "version": current.version,
            "registration": claude::registration_status(&current.plan, &outbox)?,
            "timer": scheduler.state(vendor),
            "pending": pending,
            "delivered": delivered,
            "drops": drops::counts(&store, &outbox)?,
            "orchestrators": orchestrators,
            "outside_repos": crate::outside::allowed(&outbox),
            "revoked": connection(&data).join("revoked").exists(),
            "last_scope_refresh_error": store.scope_refresh_failure()?,
            "last_receipt": receipt,
            "last_sync_error": status.as_ref().and_then(|s| s.get("last_sync_error").cloned()),
            "last_sync_at": last_sync_at(status),
        })
    );
    Ok(())
}

fn start_question(os: Os, vendor: Vendor) -> String {
    match os {
        Os::Linux => format!(
            "Append these 10 entries and enable the {} user timer?",
            vendor.timer()
        ),
        Os::Macos => format!(
            "macOS will show a background-item notification and list the {} agent under Login Items. Switching it off there stops delivery.\nAppend these 10 entries and load the launch agent?",
            vendor.label()
        ),
    }
}
// Each sync run writes its time to the status file, whatever the scheduler.
fn last_sync_at(status: Option<Object>) -> Option<String> {
    let at = status?.get("at")?.as_i64()?;
    let at = chrono::DateTime::from_timestamp(at, 0)?;
    Some(at.to_rfc3339_opts(chrono::SecondsFormat::Secs, true))
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
    match drain(|limit| pairing::sync(root, limit)) {
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
pub fn sync_all() -> Result<()> {
    let mut sent = false;
    let mut first_error = None;
    for vendor in [Vendor::Claude, Vendor::Codex] {
        let data = data_dir(vendor)?;
        if record(&data)?.is_none() {
            continue;
        }
        sent = true;
        match timer_sync(&connection(&data)) {
            Ok(()) => println!("Synced {}.", vendor.runtime()),
            Err(error) => {
                first_error.get_or_insert(error);
            }
        }
    }
    if !sent {
        println!("Not installed; nothing to send. Run `devdiary setup`.");
    }
    first_error.map_or(Ok(()), Err)
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

    #[test]
    fn on_macos_the_question_names_the_login_item_and_what_switching_it_off_does() {
        let question = start_question(Os::Macos, Vendor::Codex);
        for part in [
            "background-item notification",
            "Login Items",
            "Switching it off there stops delivery",
            "me.devdiary.codex-sync",
        ] {
            assert!(question.contains(part), "{part}");
        }
        assert!(question.ends_with('?'));
    }

    #[test]
    fn on_linux_the_question_names_the_user_timer() {
        assert_eq!(
            start_question(Os::Linux, Vendor::Claude),
            "Append these 10 entries and enable the devdiary-claude-sync.timer user timer?"
        );
    }

    #[test]
    fn the_last_sync_time_comes_from_the_sync_status_file() {
        let status = |v: Value| Some(object(&serde_json::to_vec(&v).unwrap()).unwrap());
        assert_eq!(
            last_sync_at(status(json!({"at": 86400, "last_sync_error": null}))).as_deref(),
            Some("1970-01-02T00:00:00Z")
        );
        assert_eq!(last_sync_at(status(json!({"last_sync_error": null}))), None);
        assert_eq!(last_sync_at(None), None);
    }

    fn report(pending: i64, attempted: usize, refused: usize) -> Report {
        Report {
            pending,
            delivered: 0,
            attempted,
            stopped: None,
            refused,
            refusal: (refused > 0).then(|| "http_422".into()),
            scope_refresh_error: None,
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
