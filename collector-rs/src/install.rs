//! One-command install: check the machine, ask for each harness, pair them all
//! with one approval, pin a versioned binary and register each harness's hooks,
//! then deliver the spool from the scheduler: a systemd user timer on Linux, a
//! launchd agent on macOS.
use crate::{
    Object, Result, claude, drops, exit, object, orchestrators, pairing, platform,
    scheduler::{Os, Scheduler},
    store::{Report, Store, private},
    text,
    vendor::Vendor,
};
use serde_json::{Value, json};
use std::{
    cell::Cell,
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
    /// The harness `--vendor` named, or None to set up every harness found.
    pub vendor: Option<Vendor>,
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
        return Err("no_terminal".into());
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
/// The guided setup: check this machine, ask for each harness found, pair every
/// accepted harness with one approval, set each up, then wait for a first session.
pub fn setup(o: Options<'_>, scheduler: &Scheduler<'_>) -> Result<()> {
    let mut journal = Journal::default();
    let Err(error) = guided(&o, scheduler, &mut journal) else {
        return Ok(());
    };
    let harness = error.downcast_ref::<Stopped>().map(|s| s.vendor);
    let code = exit::code(error.as_ref());
    let guidance = exit::guidance(&code);
    match harness {
        Some(vendor) => eprintln!("{}: {guidance}", vendor.name()),
        None if !guidance.is_empty() => eprintln!("{guidance}"),
        None => {}
    }
    for line in journal.lines() {
        eprintln!("{line}");
    }
    if let Some(next) = next(&code, harness) {
        eprintln!("Next: {next}");
    }
    Err(Box::new(exit::Shown(code)))
}

/// What setup changed on this machine and sent to DevDiary, for its exit.
#[derive(Default)]
struct Journal {
    changed: Vec<String>,
    sent: Vec<String>,
}
impl Journal {
    fn lines(&self) -> [String; 2] {
        let list = |title: &str, items: &[String]| {
            if items.is_empty() {
                format!("{title}: nothing.")
            } else {
                format!("{title}:\n  {}", items.join("\n  "))
            }
        };
        [
            list("Changed on this machine", &self.changed),
            list("Sent to DevDiary", &self.sent),
        ]
    }
}

/// A harness that stopped in step 3 while the others went on.
#[derive(Debug)]
struct Stopped {
    vendor: Vendor,
    code: String,
}
impl std::fmt::Display for Stopped {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.code)
    }
}
impl std::error::Error for Stopped {}

/// The one command a failed setup names.
fn next(code: &str, harness: Option<Vendor>) -> Option<String> {
    let doctor = match harness {
        Some(Vendor::Codex) => "devdiary doctor --vendor codex",
        _ => "devdiary doctor",
    };
    Some(
        match code {
            "platform_unsupported" => return None,
            "no_terminal" => "devdiary setup, in a terminal",
            "executable_format_unsupported" => "curl -fsSL https://devdiary.me/cli/install.sh | sh",
            "old_collector_registered" => "devdiary-collector uninstall",
            "backup_kept" => "devdiary setup, after you remove one of the two files",
            "no_harness_found" => "devdiary setup, after you start Claude Code or Codex once",
            "invalid_settings"
            | "unsafe_settings"
            | "python_observer_registered"
            | "already_registered" => "devdiary setup, after you fix the settings file",
            "connection_revoked"
            | "setup_incomplete"
            | "approved_scope_mismatch"
            | "frozen_scope_conflict"
            | "repair_required" => "devdiary setup, after you move the connection folder aside",
            "start_rate_limited" => "devdiary setup, once the wait has passed",
            "self_test_failed" | "timer_unavailable" | "unexpected_error" => doctor,
            refresh if refresh.starts_with("scope_refresh_") => doctor,
            _ => "devdiary setup",
        }
        .into(),
    )
}

/// A harness found in step 1 and not set up yet.
struct Found {
    vendor: Vendor,
    data: PathBuf,
    settings: PathBuf,
    /// The settings as checked, or None for a Codex hooks file that does not exist yet.
    agreed: Option<String>,
}
/// A harness the person agreed to, with the entries they were shown.
struct Accepted {
    registration: Registration,
    shown: Vec<String>,
    agreed: String,
}

fn guided(o: &Options<'_>, scheduler: &Scheduler<'_>, journal: &mut Journal) -> Result<()> {
    warn_other_on_path();
    require_consent_channel(false)?;
    println!("1. Check this machine");
    platform::preflight(scheduler)?;
    println!(
        "  This machine: {}",
        match scheduler.os {
            Os::Linux => "Linux, with a systemd user session",
            Os::Macos => "macOS, with a GUI login session for launchd",
        }
    );
    let origin = o.origin.unwrap_or(pairing::DEFAULT_ORIGIN);
    pairing::origin(origin, o.trust_origin)?;
    let found = check(o, journal)?;
    if found.is_empty() {
        println!(
            "Every harness setup found is already set up, so nothing changed. To move its hooks to this build, run `devdiary upgrade`."
        );
        for line in journal.lines() {
            println!("{line}");
        }
        println!("Next: devdiary doctor");
        return Ok(());
    }

    println!("\n2. Choose what DevDiary may change");
    let accepted = choose(found, scheduler.os, journal)?;
    if accepted.is_empty() {
        return Err("consent_declined".into());
    }

    println!("\n3. Approve this machine in DevDiary");
    pair(o, origin, &accepted, journal)?;
    let mut stopped = Vec::new();
    let mut ready = Vec::new();
    for a in &accepted {
        let vendor = a.registration.vendor;
        match set_up(a, scheduler, journal) {
            Ok(()) => ready.push(a),
            Err(error) => {
                let code = exit::code(error.as_ref());
                println!("  {}: stopped with {code}.", vendor.name());
                stopped.push(Stopped { vendor, code });
            }
        }
    }
    for a in &ready {
        let vendor = a.registration.vendor;
        if let Err(error) = offer_orchestrators(&a.registration.data, vendor) {
            let code = exit::code(error.as_ref());
            stopped.push(Stopped { vendor, code });
        }
    }
    if let Some(first) = stopped.into_iter().next() {
        return Err(Box::new(first));
    }

    let names = |or: &str| {
        let names: Vec<_> = ready.iter().map(|a| a.registration.vendor.name()).collect();
        names.join(or)
    };
    println!("Recording is on for {}.", names(" and "));
    for line in journal.lines() {
        println!("{line}");
    }
    println!("\n4. Wait for your first session");
    println!(
        "Start a session in {} in a repository DevDiary tracks. Setup waits up to 15 minutes for DevDiary to receive it. Ctrl+C stops only this wait: recording stays on, and `devdiary doctor` shows its state.",
        names(" or ")
    );
    std::io::stdout().flush()?;
    let roots: Vec<_> = ready
        .iter()
        .map(|a| (a.registration.vendor, connection(&a.registration.data)))
        .collect();
    match first_session(&roots, first_session_wait())? {
        Some((vendor, at)) => println!(
            "DevDiary received the first session from {} at {at}.",
            vendor.name()
        ),
        None => println!(
            "No session reached DevDiary while setup waited. Recording is on: the {} sends sessions every 2 minutes.",
            match scheduler.os {
                Os::Linux => "timer",
                Os::Macos => "launch agent",
            }
        ),
    }
    println!("Next: devdiary doctor");
    Ok(())
}

fn settings_path(vendor: Vendor, given: Option<&Path>) -> Result<PathBuf> {
    match given {
        Some(path) if path.is_absolute() => Ok(path.to_path_buf()),
        Some(_) => Err("unsupported_path".into()),
        None if vendor == Vendor::Codex => match std::env::var_os("CODEX_HOME") {
            Some(root) if Path::new(&root).is_absolute() => {
                Ok(PathBuf::from(root).join("hooks.json"))
            }
            Some(_) => Err("unsupported_path".into()),
            None => Ok(home()?.join(".codex/hooks.json")),
        },
        None => Ok(home()?.join(".claude/settings.json")),
    }
}
// Claude Code is found by its settings file. Codex by its home folder, because
// its hooks file need not exist yet.
fn searched(vendor: Vendor, settings: &Path) -> &Path {
    match vendor {
        Vendor::Claude => settings,
        Vendor::Codex => settings.parent().unwrap_or(settings),
    }
}
fn on_this_machine(vendor: Vendor, settings: &Path) -> bool {
    searched(vendor, settings).exists() || fs::symlink_metadata(claude::backup(settings)).is_ok()
}

/// Step 1 for each harness: find it, skip it if installed, and stop on any
/// refusal before anything is asked or sent.
fn check(o: &Options<'_>, journal: &mut Journal) -> Result<Vec<Found>> {
    // `--settings` names a Claude Code file unless `--vendor` says otherwise.
    let named = o.vendor.or(o.settings.map(|_| Vendor::Claude));
    let vendors = named.map_or(vec![Vendor::Claude, Vendor::Codex], |v| vec![v]);
    let (mut found, mut installed) = (Vec::new(), false);
    for vendor in vendors {
        let settings = settings_path(vendor, o.settings)?;
        let data = data_dir(vendor)?;
        if named.is_none() && !on_this_machine(vendor, &settings) {
            println!(
                "  {}: not found (checked {})",
                vendor.name(),
                searched(vendor, &settings).display()
            );
            continue;
        }
        if record(&data)?.is_some() {
            println!(
                "  {}: already set up ({})",
                vendor.name(),
                settings.display()
            );
            installed = true;
            continue;
        }
        println!("  {}: {}", vendor.name(), settings.display());
        if vendor == Vendor::Codex && settings.extension().is_some_and(|e| e == "toml") {
            return Err("codex_hooks_json_required".into());
        }
        offer_restore(&data, &settings, journal)?;
        let agreed = if vendor == Vendor::Codex && !settings.exists() {
            None
        } else {
            Some(claude::preflight(&settings)?)
        };
        if revoked(&data) {
            return Err("connection_revoked".into());
        }
        found.push(Found {
            vendor,
            data,
            settings,
            agreed,
        });
    }
    if found.is_empty() && !installed {
        return Err("no_harness_found".into());
    }
    Ok(found)
}

/// Step 2: which harnesses, then a separate yes or no for each one's entries.
fn choose(found: Vec<Found>, os: Os, journal: &mut Journal) -> Result<Vec<Accepted>> {
    let chosen = if found.len() > 1 {
        which(found)?
    } else {
        found
    };
    let mut accepted = Vec::new();
    for h in chosen {
        let name = h.vendor.name();
        let agreed = match h.agreed {
            Some(agreed) => agreed,
            None => {
                let question = format!(
                    "Create an empty Codex hooks file at {}?",
                    h.settings.display()
                );
                if !agrees(&question)? {
                    println!("  {name}: not set up.");
                    continue;
                }
                create_codex_hooks(&h.settings)?;
                journal.changed.push(format!(
                    "{name}: created an empty hooks file at {}.",
                    h.settings.display()
                ));
                claude::preflight(&h.settings)?
            }
        };
        let exe = pinned(&h.data);
        let registration = Registration::new(&h.data, &h.settings, &exe, h.vendor)?;
        let shown = registration.show()?;
        if !agrees(&start_question(os, h.vendor))? {
            println!("  {name}: not set up.");
            continue;
        }
        accepted.push(Accepted {
            registration,
            shown,
            agreed,
        });
    }
    Ok(accepted)
}
fn agrees(question: &str) -> Result<bool> {
    match consent(false, question) {
        Ok(()) => Ok(true),
        Err(error) if error.to_string() == "consent_declined" => Ok(false),
        Err(error) => Err(error),
    }
}
fn which(found: Vec<Found>) -> Result<Vec<Found>> {
    println!("Found Claude Code and Codex.");
    loop {
        print!("Set up which? Type both, claude or codex. [both/claude/codex] ");
        std::io::stdout().flush()?;
        let mut line = String::new();
        if std::io::stdin().lock().take(64).read_line(&mut line)? == 0 {
            return Err("consent_declined".into());
        }
        match line.trim().to_ascii_lowercase().as_str() {
            "" | "both" => return Ok(found),
            answer => {
                if let Ok(vendor) = Vendor::parse(answer) {
                    return Ok(found.into_iter().filter(|h| h.vendor == vendor).collect());
                }
            }
        }
    }
}
// Codex need not have a hooks file yet. Never initialize or rewrite config.toml.
fn create_codex_hooks(settings: &Path) -> Result<()> {
    let parent = settings.parent().ok_or("invalid_path")?;
    if !parent.exists() {
        private_dir(parent)?;
    }
    crate::store::owner_writable(parent, true)?;
    save_using(settings, &json!({}), false)
}

/// Step 3's one pairing request, for every accepted harness without a saved connection.
fn pair(o: &Options<'_>, origin: &str, accepted: &[Accepted], journal: &mut Journal) -> Result<()> {
    let roots: Vec<_> = accepted
        .iter()
        .map(|a| connection(&a.registration.data))
        .collect();
    let mut unpaired = Vec::new();
    for (a, root) in accepted.iter().zip(&roots) {
        private_dir(&a.registration.data)?;
        if !root.join("connection.json").exists() {
            unpaired.push((a.registration.vendor, root));
        }
    }
    let harnesses: Vec<_> = accepted
        .iter()
        .zip(&roots)
        .map(|(a, root)| pairing::Harness {
            vendor: a.registration.vendor,
            root,
        })
        .collect();
    let sent = Cell::new(false);
    let paired = pairing::setup(pairing::Options {
        harnesses: &harnesses,
        origin,
        trust_origin: o.trust_origin,
        browser: o.browser,
        record_orchestrated_outside_repos: o.record_orchestrated_outside_repos,
        sent: &sent,
    });
    if sent.get() {
        let names: Vec<_> = unpaired.iter().map(|(v, _)| v.name()).collect();
        journal.sent.push(format!(
            "A pairing request for {}, with this machine's hostname, OS name and version, CPU and CLI version.",
            names.join(" and ")
        ));
    }
    for (vendor, root) in &unpaired {
        if root.join("connection.json").exists() {
            journal.changed.push(format!(
                "{}: saved its connection in {}.",
                vendor.name(),
                root.display()
            ));
        }
    }
    paired.map_err(setup_failure)
}
/// Step 3 for one harness, after the exchange.
fn set_up(a: &Accepted, scheduler: &Scheduler<'_>, journal: &mut Journal) -> Result<()> {
    let r = &a.registration;
    pin_binary(&r.data)?;
    r.plan(&a.shown, Some(&a.agreed))?;
    let name = r.vendor.name();
    r.finish(scheduler, true, &mut |done| {
        journal.changed.push(format!("{name}: {done}."))
    })
}

const FIRST_SESSION_WAIT: Duration = Duration::from_secs(15 * 60);
// The scheduler sends too. Setup's own sync only shortens the wait, so a failed
// one is tried again later rather than at every check.
const RESYNC: Duration = Duration::from_secs(30);
// Debug builds let the install harness shorten the wait. Release builds never read it.
fn first_session_wait() -> Duration {
    cfg!(debug_assertions)
        .then(|| std::env::var("DEVDIARY_DEBUG_FIRST_SESSION_WAIT").ok())
        .flatten()
        .and_then(|seconds| seconds.parse().ok())
        .map_or(FIRST_SESSION_WAIT, Duration::from_secs)
}
fn counts(root: &Path) -> Result<(i64, i64)> {
    Store::open(&root.join("outbox"), None)?.counts()
}
/// Step 4: the first receipt any harness's outbox records, and when, read
/// locally. Setup sends a harness's pending rows itself and reports nothing.
fn first_session(roots: &[(Vendor, PathBuf)], limit: Duration) -> Result<Option<(Vendor, String)>> {
    let before = roots
        .iter()
        .map(|(_, root)| counts(root).map(|(_, delivered)| delivered))
        .collect::<Result<Vec<_>>>()?;
    let deadline = Instant::now() + limit;
    let mut sync_at = vec![Instant::now(); roots.len()];
    loop {
        for (i, (vendor, root)) in roots.iter().enumerate() {
            if counts(root).is_ok_and(|(pending, _)| pending > 0) && Instant::now() >= sync_at[i] {
                let _ = timer_sync(root);
                sync_at[i] = Instant::now() + RESYNC;
            }
            if counts(root).is_ok_and(|(_, delivered)| delivered > before[i]) {
                let now = SystemTime::now().duration_since(UNIX_EPOCH)?.as_secs();
                let at = chrono::DateTime::from_timestamp(now as i64, 0).ok_or("invalid_time")?;
                let at = at.to_rfc3339_opts(chrono::SecondsFormat::Secs, true);
                return Ok(Some((*vendor, at)));
            }
        }
        let left = deadline.saturating_duration_since(Instant::now());
        if left.is_zero() {
            return Ok(None);
        }
        std::thread::sleep(left.min(Duration::from_secs(1)));
    }
}
// Asked before the settings are read: the copy holds them as they were before
// an interrupted write.
fn offer_restore(data: &Path, settings: &Path, journal: &mut Journal) -> Result<()> {
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
            // The file is now the one that run's plan was made from, so its
            // registration ends here rather than waiting for a later uninstall.
            if let Some((plan, written)) = interrupted(data)?
                && written == settings
            {
                claude::remove(&plan)?;
            }
            println!("Restored {}.", settings.display());
            journal.changed.push(format!(
                "Restored {} from the copy an earlier setup left.",
                settings.display()
            ));
            Ok(())
        }
        Err(error) if error.to_string() == "consent_declined" => Err("backup_kept".into()),
        Err(error) => Err(error),
    }
}
// The newest plan whose registration began and was not removed, with its
// settings file. Only a setup that stopped before saving the install record
// leaves one that no record names.
fn interrupted(data: &Path) -> Result<Option<(PathBuf, PathBuf)>> {
    let entries = match fs::read_dir(data.join("plans")) {
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        entries => entries?,
    };
    let mut plans = Vec::new();
    for entry in entries {
        let path = entry?.path();
        let nanos = path
            .file_name()
            .and_then(|n| n.to_str())
            .and_then(|n| n.strip_suffix(".json"))
            .and_then(|n| n.rsplit_once('-'))
            .and_then(|(_, n)| n.parse::<u128>().ok());
        if let Some(nanos) = nanos {
            plans.push((nanos, path));
        }
    }
    plans.sort_unstable_by(|a, b| b.cmp(a));
    for (_, plan) in plans {
        if let Some(settings) = claude::started(&plan)? {
            return Ok(Some((plan, settings)));
        }
    }
    Ok(None)
}
fn revoked(data: &Path) -> bool {
    pairing::revoked(&connection(data).join("outbox"))
}
fn setup_failure(error: Box<dyn std::error::Error>) -> Box<dyn std::error::Error> {
    if pairing::guidance(&error.to_string()).is_some() {
        error
    } else {
        "setup_incomplete".into()
    }
}

struct Registration {
    vendor: Vendor,
    data: PathBuf,
    settings: PathBuf,
    exe: PathBuf,
    plan: PathBuf,
}
impl Registration {
    fn new(data: &Path, settings: &Path, exe: &Path, vendor: Vendor) -> Result<Self> {
        let plans = data.join("plans");
        let nanos = SystemTime::now().duration_since(UNIX_EPOCH)?.as_nanos();
        let plan = plans.join(format!("{}-{nanos}.json", vendor.slug()));
        Ok(Self {
            vendor,
            data: data.into(),
            settings: settings.into(),
            exe: exe.into(),
            plan,
        })
    }
    fn show(&self) -> Result<Vec<String>> {
        println!(
            "{} hook entries to append to {}:",
            self.vendor.name(),
            self.settings.display()
        );
        let lines = claude::entry_lines_for(&self.plan, &self.exe, self.vendor)?;
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
            &connection(&self.data),
            &self.settings,
            &self.plan,
            &self.exe,
            agreed,
        )?;
        if claude::entries(&self.plan)? != shown {
            let _ = fs::remove_file(&self.plan);
            return Err("stale_plan".into());
        }
        Ok(())
    }
    // With a backup, setup keeps the copy until the install record names the
    // write: a run killed in between leaves the copy, and the next setup offers it.
    // `done` hears each change as it lands.
    fn finish(
        &self,
        scheduler: &Scheduler<'_>,
        backup: bool,
        done: &mut dyn FnMut(String),
    ) -> Result<()> {
        let (data, exe, plan) = (&self.data, &self.exe, &self.plan);
        if backup {
            claude::apply_with_backup(plan)?;
        } else {
            claude::apply(plan)?;
        }
        done(format!(
            "wrote its hook entries to {}",
            self.settings.display()
        ));
        pause("hooks_written");
        if self.vendor == Vendor::Codex {
            println!(
                "Codex: review and trust these exact definitions in /hooks before capture can run. An upgrade changes definitions and requires review again. Stock runtime qualification is unverified."
            );
        }
        save(
            &data.join(RECORD),
            &json!({"version": VERSION, "executable": exe, "plan": plan, "settings": self.settings}),
        )?;
        if backup {
            claude::discard_backup(&self.settings)?;
        }
        scheduler.enable(exe, &connection(data), self.vendor)?;
        let job = match scheduler.os {
            Os::Linux => self.vendor.timer(),
            Os::Macos => self.vendor.label(),
        };
        done(format!("started {job}"));
        self_test(exe, plan, &connection(data).join("outbox"), self.vendor)?;
        println!(
            "Self-test passed for {}: the installed hook recorded a local test event that is never uploaded. {job} syncs every 2 minutes.",
            self.vendor.name()
        );
        Ok(())
    }
}

// Debug builds wait here when a test names the stage, so it can kill setup at
// that exact point. Release builds never read the variable.
fn pause(stage: &str) {
    if cfg!(debug_assertions)
        && std::env::var_os("DEVDIARY_DEBUG_PAUSE_AT").is_some_and(|s| s == stage)
    {
        eprintln!("paused at {stage}");
        loop {
            std::thread::sleep(Duration::from_secs(1));
        }
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
    registration.finish(scheduler, false, &mut |_| {})?;
    offer_orchestrators(&data, vendor)
}

fn ask_orchestrators(outbox: &Path, vendor: Vendor) -> Result<()> {
    let (entries, refused) = orchestrators::pending(outbox)?;
    println!(
        "Environment names the {} hook would read to recognise orchestrated runs:",
        vendor.name()
    );
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
fn offer_orchestrators(data: &Path, vendor: Vendor) -> Result<()> {
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
    match ask_orchestrators(&outbox, vendor) {
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
    ask_orchestrators(&outbox, vendor)
}

pub fn uninstall(scheduler: &Scheduler<'_>, vendor: Vendor) -> Result<()> {
    let data = data_dir(vendor)?;
    let old = record(&data)?;
    let left = match old {
        None => interrupted(&data)?,
        Some(_) => None,
    };
    let timer = scheduler.disable(vendor);
    if let Some(old) = &old {
        claude::remove(&old.plan)?;
        // Setup saves the record, then discards its copy, so a copy may be left.
        claude::discard_backup(&old.settings)?;
        fs::remove_file(data.join(RECORD))?;
        File::open(&data)?.sync_all()?;
    }
    if let Some((plan, settings)) = &left {
        claude::remove(plan)?;
        claude::discard_backup(settings)?;
        println!(
            "An earlier setup stopped before it saved its install record. Its hook entries are removed from {}.",
            settings.display()
        );
    }
    let units = timer?;
    let (job, stopped) = match scheduler.os {
        Os::Linux => ("sync timer", "disabled"),
        Os::Macos => ("launch agent", "unloaded"),
    };
    match (old.is_some() || left.is_some(), units) {
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

/// How a `doctor` run ended. It prints its report either way.
#[derive(Debug, PartialEq, Eq)]
pub enum Doctor {
    Reported,
    CheckFailed,
}
pub fn doctor(scheduler: &Scheduler<'_>, vendor: Vendor) -> Result<Doctor> {
    let data = data_dir(vendor)?;
    let Some(current) = record(&data)? else {
        println!(
            "{}",
            json!({"installed": false, "timer": scheduler.state(vendor)})
        );
        return Ok(Doctor::Reported);
    };
    let outbox = connection(&data).join("outbox");
    // Unreadable settings are the case a person most needs doctor for, so the
    // other checks still run.
    let (registration, unreadable) = match claude::registration_status(&current.plan, &outbox) {
        Ok(status) => (Value::Object(status), None),
        Err(error) => {
            let unreadable = error.downcast::<claude::UnreadableSettings>()?;
            (unreadable.report(), Some(unreadable))
        }
    };
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
            "registration": registration,
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
    Ok(match unreadable {
        Some(unreadable) => {
            eprintln!("{}", unreadable.next_step());
            Doctor::CheckFailed
        }
        None => Doctor::Reported,
    })
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
