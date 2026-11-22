//! One-command install: check the machine, ask for each harness, pair them all
//! with one approval, pin a versioned binary and register each harness's hooks,
//! then deliver the spool from the scheduler: a systemd user timer on Linux, a
//! launchd agent on macOS.
use crate::{
    Object, Result, claude, drops, exit, object, orchestrators, pairing, platform,
    scheduler::{Os, Scheduler},
    store::{Report, Store, private},
    text,
    ui::{self, Mark, Out, Style},
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
pub(crate) fn data_dir(vendor: Vendor) -> Result<PathBuf> {
    Ok(home()?
        .join(".local/share/devdiary/cli")
        .join(vendor.slug()))
}
pub(crate) fn connection(data: &Path) -> PathBuf {
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

pub(crate) struct Record {
    pub(crate) settings: PathBuf,
    pub(crate) plan: PathBuf,
    pub(crate) version: String,
    /// The pinned copy the hooks and the scheduler run.
    pub(crate) executable: PathBuf,
}
pub(crate) fn record(data: &Path) -> Result<Option<Record>> {
    let Some(doc) = load(&data.join(RECORD))? else {
        return Ok(None);
    };
    Ok(Some(Record {
        settings: text(&doc, "settings")?.into(),
        plan: text(&doc, "plan")?.into(),
        version: text(&doc, "version")?.into(),
        executable: text(&doc, "executable")?.into(),
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

pub(crate) fn require_consent_channel(yes: bool) -> Result<()> {
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
    match answer(question, "y/N")?.as_str() {
        "y" | "Y" | "yes" => Ok(()),
        _ => Err("consent_declined".into()),
    }
}
fn answer(question: &str, choices: &str) -> Result<String> {
    Out::stdout().ask("  ", question, choices)?;
    let mut line = String::new();
    std::io::stdin().lock().take(64).read_line(&mut line)?;
    Ok(line.trim().to_owned())
}
/// A yes or no to a summarised change, where `d` prints the exact entries first.
pub(crate) fn consent_to(question: &str, details: &dyn Fn()) -> Result<()> {
    require_consent_channel(false)?;
    loop {
        match answer(question, "y/N/d")?.as_str() {
            "y" | "Y" | "yes" => return Ok(()),
            "d" | "D" => details(),
            _ => return Err("consent_declined".into()),
        }
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
        Out::stderr().line(
            "",
            Some(Mark::Attention),
            None,
            &format!(
                "{} comes first on PATH, so typing `devdiary` runs that program, not this one. Setup continues.",
                ui::path(&other)
            ),
        );
    }
}
/// The guided setup: check this machine, ask for each harness found, pair every
/// accepted harness with one approval, set each up, then wait for a first session.
pub fn setup(o: Options<'_>, scheduler: &Scheduler<'_>) -> Result<()> {
    let mut journal = Journal::default();
    let result = guided(&o, scheduler, &mut journal);
    reported("setup", o.vendor, &journal, result)
}
/// A guided command's failure as printed: what happened, what it changed and
/// sent, and the next command. `named` is the harness the command was limited to.
pub(crate) fn reported(
    command: &'static str,
    named: Option<Vendor>,
    journal: &Journal,
    result: Result<()>,
) -> Result<()> {
    let Err(error) = result else {
        return Ok(());
    };
    let stopped = error.downcast_ref::<Stopped>();
    let harness = stopped.map(|s| s.vendor);
    let cause = stopped.map_or(error.as_ref(), |s| s.error.as_ref());
    let code = exit::code(cause);
    let context = exit::Context {
        command,
        vendor: harness.or(named),
    };
    let out = Out::stderr();
    let report = exit::explain(cause, &context).render(
        out.mode(),
        harness.map(Vendor::name),
        &journal.lines(),
    );
    out.raw(&report);
    Err(Box::new(exit::Shown(code)))
}

/// What a guided command changed on this machine and sent to DevDiary, for its exit.
#[derive(Default)]
pub(crate) struct Journal {
    pub(crate) changed: Vec<String>,
    pub(crate) sent: Vec<String>,
}
impl Journal {
    /// Both lists, each item indented under its title.
    fn lines(&self) -> Vec<String> {
        let mut rows = Vec::new();
        for (title, items) in [
            ("Changed on this machine", &self.changed),
            ("Sent to DevDiary", &self.sent),
        ] {
            if items.is_empty() {
                rows.push(format!("{title}: nothing."));
            } else {
                rows.push(format!("{title}:"));
                rows.extend(items.iter().map(|item| format!("  {item}")));
            }
        }
        rows
    }
    pub(crate) fn print(&self, out: Out) {
        for row in self.lines() {
            let item = row.trim_start();
            let indent = 2 + row.len() - item.len();
            out.line(&" ".repeat(indent), None, Some(Style::Dim), item);
        }
    }
}

/// A harness that stopped in step 3 while the others went on, and why.
#[derive(Debug)]
pub(crate) struct Stopped {
    pub(crate) vendor: Vendor,
    pub(crate) error: Box<dyn std::error::Error>,
}
impl std::fmt::Display for Stopped {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&exit::code(self.error.as_ref()))
    }
}
impl std::error::Error for Stopped {}

/// A harness found in step 1 and not set up yet.
struct Found {
    vendor: Vendor,
    data: PathBuf,
    settings: PathBuf,
    /// The settings as checked, or None for a Codex hooks file that does not exist yet.
    agreed: Option<String>,
    /// DevDiary entries whose binary or plan is gone, which setup offers to remove.
    stale: Vec<claude::Entry>,
}
/// A harness the person agreed to, with the entries they were shown.
struct Accepted {
    registration: Registration,
    shown: Vec<String>,
    agreed: String,
    /// The stale entries they agreed to remove, as `d` shows them.
    stale: Vec<String>,
}

fn guided(o: &Options<'_>, scheduler: &Scheduler<'_>, journal: &mut Journal) -> Result<()> {
    let out = Out::stdout();
    out.header();
    warn_other_on_path();
    require_consent_channel(false)?;
    out.step(1, "Check this machine");
    platform::preflight(scheduler)?;
    out.done(match scheduler.os {
        Os::Linux => "This machine: Linux, with a systemd user session",
        Os::Macos => "This machine: macOS, with a GUI login session for launchd",
    });
    let origin = o.origin.unwrap_or(pairing::DEFAULT_ORIGIN);
    pairing::origin(origin, o.trust_origin)?;
    let found = check(o, journal)?;
    if found.is_empty() {
        out.raw("");
        out.done(
            "Every harness setup found is already set up, so nothing changed. To move its hooks to this build, run `devdiary upgrade`.",
        );
        journal.print(out);
        out.raw("");
        out.next("devdiary doctor");
        return Ok(());
    }

    out.step(2, "Choose what DevDiary may change");
    let accepted = choose(found, scheduler.os, journal)?;
    if accepted.is_empty() {
        return Err("consent_declined".into());
    }

    out.step(3, "Approve this machine in DevDiary");
    let paired = pair(o, origin, &accepted, journal)?;
    let mut stopped = Vec::new();
    let mut ready = Vec::new();
    for a in &accepted {
        let vendor = a.registration.vendor;
        match set_up(a, scheduler, journal) {
            Ok(()) => ready.push(a),
            Err(error) => {
                let code = exit::code(error.as_ref());
                out.failed(&format!("{}: stopped with {code}.", vendor.name()));
                stopped.push(Stopped { vendor, error });
            }
        }
    }
    for a in &ready {
        let vendor = a.registration.vendor;
        if let Err(error) = offer_orchestrators(&a.registration.data, vendor) {
            stopped.push(Stopped { vendor, error });
        }
    }
    if let Some(first) = stopped.into_iter().next() {
        return Err(Box::new(first));
    }

    // The summary prints before the wait, which Ctrl+C may end.
    out.raw("");
    for a in &ready {
        let vendor = a.registration.vendor;
        let workspace = paired
            .newly
            .contains(&vendor)
            .then_some(paired.workspace.as_deref())
            .flatten();
        out.line("", Some(Mark::Done), None, &recording(vendor, workspace));
    }
    if ready.iter().any(|a| a.registration.vendor == Vendor::Codex) {
        out.line("", Some(Mark::Attention), None, CODEX_TRUST);
    }
    out.line("", Some(Mark::Done), None, "Sync runs every 2 minutes");
    out.raw("");
    journal.print(out);

    let names = |or: &str| {
        let names: Vec<_> = ready.iter().map(|a| a.registration.vendor.name()).collect();
        names.join(or)
    };
    out.step(4, "Wait for your first session");
    out.say(&format!(
        "Start a session in {} in a repository DevDiary tracks. Setup waits up to 15 minutes for DevDiary to receive it. Ctrl+C stops only this wait: recording stays on, and `devdiary doctor` shows its state.",
        names(" or ")
    ));
    let roots: Vec<_> = ready
        .iter()
        .map(|a| (a.registration.vendor, connection(&a.registration.data)))
        .collect();
    let limit = first_session_wait();
    let mut live = ui::Live::new(out);
    let received = first_session(&roots, limit, &mut |elapsed| {
        live.show(&ui::waiting(elapsed.min(limit), limit))
    });
    live.end()?;
    match received? {
        Some((vendor, at)) => out.done(&format!(
            "DevDiary received the first session from {} at {at}.",
            vendor.name()
        )),
        None => out.attention(&format!(
            "No session reached DevDiary while setup waited. Recording is on: the {} sends sessions every 2 minutes.",
            match scheduler.os {
                Os::Linux => "timer",
                Os::Macos => "launch agent",
            }
        )),
    }
    out.raw("");
    out.next("devdiary doctor");
    Ok(())
}
pub(crate) const CODEX_TRUST: &str = "Codex: trust the DevDiary entries in /hooks";
/// The checklist line for a harness that records now.
pub(crate) fn recording(vendor: Vendor, workspace: Option<&str>) -> String {
    match workspace {
        Some(workspace) => format!("{} records to the workspace {workspace}", vendor.name()),
        None => format!("{} records to DevDiary", vendor.name()),
    }
}

fn settings_path(vendor: Vendor, given: Option<&Path>) -> Result<PathBuf> {
    match given {
        Some(path) if path.is_absolute() => Ok(path.to_path_buf()),
        Some(path) => Err(exit::Refusal::with(
            "unsupported_path",
            path,
            "is given to --settings, which needs an absolute path",
        )),
        None if vendor == Vendor::Codex => match std::env::var_os("CODEX_HOME") {
            Some(root) if Path::new(&root).is_absolute() => {
                Ok(PathBuf::from(root).join("hooks.json"))
            }
            Some(root) => Err(exit::Refusal::with(
                "unsupported_path",
                Path::new(&root),
                "is CODEX_HOME, which needs to be an absolute path",
            )),
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
        let out = Out::stdout();
        if named.is_none() && !on_this_machine(vendor, &settings) {
            out.line(
                "    ",
                None,
                Some(Style::Dim),
                &format!(
                    "{}: not found (checked {})",
                    vendor.name(),
                    ui::path(searched(vendor, &settings))
                ),
            );
            continue;
        }
        if record(&data)?.is_some() {
            out.done(&format!(
                "{}: already set up ({})",
                vendor.name(),
                ui::path(&settings)
            ));
            installed = true;
            continue;
        }
        out.done(&format!("{}: {}", vendor.name(), ui::path(&settings)));
        if vendor == Vendor::Codex && settings.extension().is_some_and(|e| e == "toml") {
            return Err(exit::Refusal::about("codex_hooks_json_required", &settings));
        }
        offer_restore(&data, &settings, journal)?;
        let (agreed, stale) = if vendor == Vendor::Codex && !settings.exists() {
            (None, Vec::new())
        } else {
            let checked = claude::preflight(&settings)?;
            let stale = left_behind(vendor, &data, &settings, checked.entries)?;
            (Some(checked.digest), stale)
        };
        if !stale.is_empty() {
            show_stale(vendor, &settings, &stale);
        }
        if revoked(&data) {
            return Err("connection_revoked".into());
        }
        found.push(Found {
            vendor,
            data,
            settings,
            agreed,
            stale,
        });
    }
    if found.is_empty() && !installed {
        return Err("no_harness_found".into());
    }
    Ok(found)
}

/// The DevDiary entries a settings file holds before setup adds its own. When
/// every one is stale, setup offers to remove them. Any whose binary and plan
/// exist belongs to an install, so setup stops and names it.
fn left_behind(
    vendor: Vendor,
    data: &Path,
    settings: &Path,
    entries: Vec<claude::Entry>,
) -> Result<Vec<claude::Entry>> {
    let working: Vec<_> = entries.iter().filter(|e| !e.stale()).collect();
    let Some(first) = working.first() else {
        return Ok(entries);
    };
    let held = claude::entries_named(entries.len());
    // Uninstall finds a run that stopped before its record by its plan.
    if first.plan.starts_with(data) {
        return Err(exit::Refusal::with_next(
            "already_registered",
            settings,
            format!(
                "{held} from an earlier setup on this machine that stopped before it saved its install record"
            ),
            format!("devdiary uninstall{}", vendor_flag(vendor)),
        ));
    }
    let runs = match (working.len(), entries.len()) {
        (n, total) if n == total => format!("that run{}", if n == 1 { "s" } else { "" }),
        (n, _) => format!(", and {n} of them run{}", if n == 1 { "s" } else { "" }),
    };
    Err(exit::Refusal::with(
        "already_registered",
        settings,
        format!(
            "{held}{}{runs} {}, which still exists: another DevDiary install uses it. Remove its entries with that install's `devdiary uninstall`, or move that install to this build with its `devdiary upgrade`",
            if runs.starts_with(',') { "" } else { " " },
            ui::path(&first.executable)
        ),
    ))
}
/// Step 1's note on stale entries, with the binaries they run.
fn show_stale(vendor: Vendor, settings: &Path, stale: &[claude::Entry]) {
    let out = Out::stdout();
    out.attention(&format!(
        "{}: {} holds {} whose binary or plan no longer exists, so they record nothing. Setup offers to remove them.",
        vendor.name(),
        ui::path(settings),
        claude::entries_named(stale.len())
    ));
    let mut shown = Vec::new();
    for entry in stale {
        if !shown.contains(&&entry.executable) {
            shown.push(&entry.executable);
            out.line(
                "    ",
                None,
                Some(Style::Dim),
                &format!("They run {}", ui::path(&entry.executable)),
            );
        }
    }
}
fn vendor_flag(vendor: Vendor) -> &'static str {
    match vendor {
        Vendor::Codex => " --vendor codex",
        Vendor::Claude => "",
    }
}

/// Step 2: which harnesses, then a separate yes or no for each one's entries.
fn choose(found: Vec<Found>, os: Os, journal: &mut Journal) -> Result<Vec<Accepted>> {
    let chosen = if found.len() > 1 {
        which(found)?
    } else {
        found
    };
    let out = Out::stdout();
    let mut accepted = Vec::new();
    for h in chosen {
        let name = h.vendor.name();
        out.raw("");
        let agreed = match h.agreed {
            Some(agreed) => agreed,
            None => {
                let question = format!(
                    "Create an empty Codex hooks file at {}?",
                    ui::path(&h.settings)
                );
                if !agrees(consent(false, &question))? {
                    out.attention(&format!("{name}: not set up."));
                    continue;
                }
                create_codex_hooks(&h.settings)?;
                journal.changed.push(format!(
                    "{name}: created an empty hooks file at {}.",
                    ui::path(&h.settings)
                ));
                claude::preflight(&h.settings)?.digest
            }
        };
        let exe = pinned(&h.data);
        let registration = Registration::new(&h.data, &h.settings, &exe, h.vendor, VERSION)?;
        let shown = registration.entries()?;
        let stale: Vec<_> = h.stale.iter().map(claude::Entry::line).collect();
        for row in summary(
            os,
            h.vendor,
            shown.len(),
            Path::new(&ui::path(&h.settings)),
            None,
            stale.len(),
        ) {
            out.raw(&format!("  {row}"));
        }
        if let Some(note) = macos_note(os, h.vendor) {
            out.attention(&note);
        }
        out.dim("Type d to see the exact entries first.");
        let question = format!("Set up {name}?");
        let details = || {
            if !stale.is_empty() {
                out.say(&format!(
                    "Stale DevDiary hook entries to remove from {}:",
                    ui::path(&h.settings)
                ));
                for line in &stale {
                    out.raw(&format!("    {line}"));
                }
            }
            registration.print(&shown)
        };
        if !agrees(consent_to(&question, &details))? {
            out.attention(&format!("{name}: not set up."));
            continue;
        }
        accepted.push(Accepted {
            registration,
            shown,
            agreed,
            stale,
        });
    }
    Ok(accepted)
}
fn agrees(answer: Result<()>) -> Result<bool> {
    match answer {
        Ok(()) => Ok(true),
        Err(error) if error.to_string() == "consent_declined" => Ok(false),
        Err(error) => Err(error),
    }
}
fn which(found: Vec<Found>) -> Result<Vec<Found>> {
    let out = Out::stdout();
    out.say("Found Claude Code and Codex.");
    loop {
        out.ask(
            "  ",
            "Set up which? Type both, claude or codex.",
            "both/claude/codex",
        )?;
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

/// Who step 3 paired, and the workspace they joined.
struct Paired {
    newly: Vec<Vendor>,
    workspace: Option<String>,
}
/// Step 3's one pairing request, for every accepted harness without a saved connection.
fn pair(
    o: &Options<'_>,
    origin: &str,
    accepted: &[Accepted],
    journal: &mut Journal,
) -> Result<Paired> {
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
        let vendors: Vec<_> = unpaired.iter().map(|(v, _)| *v).collect();
        journal.sent.push(pairing_request(&vendors));
    }
    for (vendor, root) in &unpaired {
        if root.join("connection.json").exists() {
            journal.changed.push(format!(
                "{}: saved its connection in {}.",
                vendor.name(),
                ui::path(root)
            ));
        }
    }
    Ok(Paired {
        newly: unpaired.iter().map(|(vendor, _)| *vendor).collect(),
        workspace: paired.map_err(setup_failure)?,
    })
}
/// What a pairing request sent, for the journal.
pub(crate) fn pairing_request(vendors: &[Vendor]) -> String {
    let names: Vec<_> = vendors.iter().map(|v| v.name()).collect();
    format!(
        "A pairing request for {}, with this machine's hostname, OS name and version, CPU and CLI version.",
        names.join(" and ")
    )
}
/// Step 3 for one harness, after the exchange.
fn set_up(a: &Accepted, scheduler: &Scheduler<'_>, journal: &mut Journal) -> Result<()> {
    let r = &a.registration;
    let name = r.vendor.name();
    pin_binary(&r.data)?;
    let agreed = if a.stale.is_empty() {
        a.agreed.clone()
    } else {
        let (agreed, copy) = claude::remove_stale(&r.settings, &a.agreed, &a.stale)?;
        journal.changed.push(format!(
            "{name}: removed {} from {}. The file as it was is kept at {}.",
            stale_named(a.stale.len()),
            ui::path(&r.settings),
            ui::path(&copy)
        ));
        agreed
    };
    r.plan(&a.shown, Some(&agreed))?;
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
/// The rows a connection folder's outbox has waiting, and how many it delivered.
pub(crate) fn counts(root: &Path) -> Result<(i64, i64)> {
    Store::open(&root.join("outbox"), None)?.counts()
}
/// Step 4: the first receipt any harness's outbox records, and when, read
/// locally. Setup sends a harness's pending rows itself and reports nothing.
/// `tick` hears the time waited so far, about once a second.
fn first_session(
    roots: &[(Vendor, PathBuf)],
    limit: Duration,
    tick: &mut dyn FnMut(Duration) -> std::io::Result<()>,
) -> Result<Option<(Vendor, String)>> {
    let before = roots
        .iter()
        .map(|(_, root)| counts(root).map(|(_, delivered)| delivered))
        .collect::<Result<Vec<_>>>()?;
    let began = Instant::now();
    let deadline = began + limit;
    let mut sync_at = vec![Instant::now(); roots.len()];
    loop {
        tick(began.elapsed())?;
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
    let out = Out::stdout();
    out.attention(&format!(
        "An earlier setup stopped while it wrote {}. Its copy of the file from before that run is {}.",
        ui::path(settings),
        ui::path(&backup)
    ));
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
            out.done(&format!("Restored {}.", ui::path(settings)));
            journal.changed.push(format!(
                "Restored {} from the copy an earlier setup left.",
                ui::path(settings)
            ));
            Ok(())
        }
        Err(error) if error.to_string() == "consent_declined" => Err(exit::Refusal::with(
            "backup_kept",
            settings,
            ui::path(&backup),
        )),
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
    if exit::known_code(&error.to_string()) {
        error
    } else {
        "setup_incomplete".into()
    }
}

/// One harness's hook entries for a new plan, run by the pinned copy `exe` of `version`.
pub(crate) struct Registration {
    vendor: Vendor,
    data: PathBuf,
    settings: PathBuf,
    exe: PathBuf,
    plan: PathBuf,
    version: String,
}
impl Registration {
    pub(crate) fn new(
        data: &Path,
        settings: &Path,
        exe: &Path,
        vendor: Vendor,
        version: &str,
    ) -> Result<Self> {
        let plans = data.join("plans");
        let nanos = SystemTime::now().duration_since(UNIX_EPOCH)?.as_nanos();
        let plan = plans.join(format!("{}-{nanos}.json", vendor.slug()));
        Ok(Self {
            vendor,
            data: data.into(),
            settings: settings.into(),
            exe: exe.into(),
            plan,
            version: version.into(),
        })
    }
    /// The exact entries the hooks would add, as consent is given to them.
    pub(crate) fn entries(&self) -> Result<Vec<String>> {
        claude::entry_lines_for(&self.plan, &self.exe, self.vendor)
    }
    /// The exact entries, for a person who asked to see them.
    pub(crate) fn print(&self, entries: &[String]) {
        let out = Out::stdout();
        out.say(&format!(
            "{} hook entries to append to {}:",
            self.vendor.name(),
            ui::path(&self.settings)
        ));
        for line in entries {
            out.raw(&format!("    {line}"));
        }
    }
    // Consent is given to the lines `show` printed and, during setup, to the
    // settings as they were then, so the saved plan must match both.
    pub(crate) fn plan(&self, shown: &[String], agreed: Option<&str>) -> Result<()> {
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
            return Err(exit::Refusal::about("stale_plan", &self.settings));
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
        self.write(backup, done)?;
        scheduler.enable(&self.exe, &connection(&self.data), self.vendor)?;
        done(format!("started {}", job(scheduler.os, self.vendor)));
        self.check(scheduler.os)
    }
    /// Applies the plan and saves the install record that names it.
    pub(crate) fn write(&self, backup: bool, done: &mut dyn FnMut(String)) -> Result<()> {
        let plan = &self.plan;
        if backup {
            claude::apply_with_backup(plan)?;
        } else {
            claude::apply(plan)?;
        }
        done(format!(
            "wrote its hook entries to {}",
            ui::path(&self.settings)
        ));
        pause("hooks_written");
        if self.vendor == Vendor::Codex {
            Out::stdout().attention(&format!(
                "{CODEX_TRUST} before it can record. After an upgrade, trust them again."
            ));
        }
        save(
            &self.data.join(RECORD),
            &json!({"version": self.version, "executable": self.exe, "plan": plan, "settings": self.settings}),
        )?;
        if backup {
            claude::discard_backup(&self.settings)?;
        }
        Ok(())
    }
    /// The local self-test of the hooks this registration wrote.
    pub(crate) fn check(&self, os: Os) -> Result<()> {
        let outbox = connection(&self.data).join("outbox");
        self_test(&self.exe, &self.plan, &outbox, self.vendor)?;
        Out::stdout().done(&format!(
            "Self-test passed for {}: the installed hook recorded a local test event that is never uploaded. {} syncs every 2 minutes.",
            self.vendor.name(),
            job(os, self.vendor)
        ));
        Ok(())
    }
}
fn job(os: Os, vendor: Vendor) -> &'static str {
    match os {
        Os::Linux => vendor.timer(),
        Os::Macos => vendor.label(),
    }
}

// Debug builds wait here when a test names the stage, so it can kill setup at
// that exact point. Release builds never read the variable.
pub(crate) fn pause(stage: &str) {
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
    let out = Out::stdout();
    out.header();
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
    let registration = Registration::new(&data, &old.settings, &exe, vendor, VERSION)?;
    let shown = registration.entries()?;
    out.raw("");
    let settings = ui::path(&old.settings);
    for row in summary(
        scheduler.os,
        vendor,
        shown.len(),
        Path::new(&settings),
        Some(&old.version),
        0,
    ) {
        out.raw(&format!("  {row}"));
    }
    if !yes {
        out.dim("Type d to see the exact entries first.");
        consent_to(&format!("Upgrade {}?", vendor.name()), &|| {
            registration.print(&shown)
        })?;
    }
    claude::remove(&old.plan)?;
    pin_binary(&data)?;
    registration.plan(&shown, None)?;
    registration.finish(scheduler, false, &mut |_| {})?;
    offer_orchestrators(&data, vendor)
}

fn ask_orchestrators(outbox: &Path, vendor: Vendor) -> Result<()> {
    let (entries, refused) = orchestrators::pending(outbox)?;
    let out = Out::stdout();
    out.raw("");
    out.say(&format!(
        "Environment names the {} hook would read to recognise orchestrated runs:",
        vendor.name()
    ));
    for line in orchestrators::describe(&entries) {
        out.raw(&format!("    {line}"));
    }
    show_changes(outbox, &entries);
    if entries.is_empty() {
        out.raw("    (none: the hook would stop reading orchestrator names)");
    }
    if refused > 0 {
        out.attention(&format!(
            "{refused} more refused: a name is not allowed or the entry is malformed."
        ));
    }
    out.dim("IDs that are UUIDs are sent to the server. Paths stay on this machine.");
    out.dim(&format!(
        "Each tool may need its own setup, or its agents record nothing. See {INTEGRATIONS}"
    ));
    consent(
        false,
        &format!("Let the {} hook read these names?", vendor.name()),
    )?;
    orchestrators::approve(outbox, &entries)?;
    out.done("Approved.");
    Ok(())
}
fn show_changes(outbox: &Path, entries: &[Value]) {
    let out = Out::stdout();
    let Ok(old) = orchestrators::approved(outbox) else {
        out.attention("The saved approved list cannot be read. Approving replaces it.");
        return;
    };
    if old.is_empty() {
        return;
    }
    let (added, removed) = orchestrators::changes(&old, entries);
    if added.is_empty() && removed.is_empty() {
        out.say("Only the order changed. The first entry that matches wins.");
    }
    for line in added {
        out.raw(&format!("    added {line}"));
    }
    for line in removed {
        out.raw(&format!("    removed {line}"));
    }
}
// `--yes` covers the hook entries the person chose to skip reviewing. It cannot
// cover names the server picks later, so only a terminal answer approves them.
fn offer_orchestrators(data: &Path, vendor: Vendor) -> Result<()> {
    let root = connection(data);
    let outbox = root.join("outbox");
    pairing::refresh_scope(&root)?;
    let out = Out::stdout();
    if orchestrators::offer_unreadable(&outbox) {
        out.attention("The saved orchestrator offer cannot be read. The next sync replaces it.");
        return Ok(());
    }
    if !orchestrators::waiting(&outbox) {
        return Ok(());
    }
    if !std::io::stdin().is_terminal() {
        out.attention(
            "Orchestrator names wait for approval. In a terminal run: devdiary orchestrators approve",
        );
        return Ok(());
    }
    match ask_orchestrators(&outbox, vendor) {
        Err(error) if error.to_string() == "consent_declined" => {
            out.attention(
                "No names approved. Hooks stay registered. Run `devdiary orchestrators approve` to answer again.",
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
    Out::stdout().line(
        "",
        Some(Mark::Done),
        None,
        if on {
            OUTSIDE_ON
        } else {
            "Sessions outside repositories are no longer recorded."
        },
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
        Out::stdout().line(
            "",
            Some(Mark::Done),
            None,
            "No orchestrator change waits for approval.",
        );
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
    let out = Out::stdout();
    let done = |text: &str| out.line("", Some(Mark::Done), None, text);
    if let Some((plan, settings)) = &left {
        claude::remove(plan)?;
        claude::discard_backup(settings)?;
        done(&format!(
            "An earlier setup stopped before it saved its install record. Its hook entries are removed from {}.",
            ui::path(settings)
        ));
    }
    let units = timer?;
    let (job, stopped) = match scheduler.os {
        Os::Linux => ("sync timer", "disabled"),
        Os::Macos => ("launch agent", "unloaded"),
    };
    match (old.is_some() || left.is_some(), units) {
        (false, false) => done("Not installed; nothing changed."),
        (false, true) => done(&format!(
            "Not installed; the leftover {job} was {stopped} and removed."
        )),
        (true, _) => done(&format!(
            "Hooks removed and the {job} {stopped}. The connection and any pending rows stay in {}; revoke the connection in the browser to retire its credential.",
            ui::path(&connection(&data))
        )),
    }
    Ok(())
}

/// How a `doctor` run ended. It prints its report either way.
#[derive(Debug, PartialEq, Eq)]
pub enum Doctor {
    Reported,
    CheckFailed,
}
/// `doctor`: a readable report on each harness, or with `json` the JSON report
/// of one harness, for scripts. `named` limits the report to one harness.
pub fn doctor(scheduler: &Scheduler<'_>, named: Option<Vendor>, json: bool) -> Result<Doctor> {
    if json {
        return doctor_json(scheduler, named.unwrap_or_default());
    }
    let out = Out::stdout();
    out.header();
    out.raw("");
    let (mut failed, mut next) = (false, None);
    for vendor in named.map_or(vec![Vendor::Claude, Vendor::Codex], |v| vec![v]) {
        let rows = match health(scheduler, vendor).and_then(|h| {
            failed |= h.unreadable.is_some();
            let settings = match record(&data_dir(vendor)?)? {
                Some(current) => current.settings,
                None => settings_path(vendor, None)?,
            };
            let found = on_this_machine(vendor, &settings);
            let workspace = workspace(&data_dir(vendor)?);
            Ok(assess(
                scheduler.os,
                vendor,
                &settings,
                found,
                workspace.as_deref(),
                &h.report,
            ))
        }) {
            Ok(rows) => rows,
            Err(error) => vec![Finding {
                mark: Some(Mark::Failed),
                text: format!(
                    "{}: doctor could not read its state ({}).",
                    vendor.name(),
                    exit::code(error.as_ref())
                ),
                why: None,
                next: Some(format!(
                    "devdiary doctor --json{}",
                    if vendor == Vendor::Codex {
                        " --vendor codex"
                    } else {
                        ""
                    }
                )),
            }],
        };
        for (i, row) in rows.iter().enumerate() {
            let indent = if i == 0 { "" } else { "  " };
            let style = row.mark.is_none().then_some(Style::Dim);
            out.line(indent, row.mark, style, &row.text);
            if let Some(why) = &row.why {
                out.line(&format!("{indent}  "), None, Some(Style::Dim), why);
            }
        }
        next = next.or_else(|| rows.iter().find_map(|r| r.next.clone()));
    }
    if let Some(next) = next {
        out.raw("");
        out.next(&next);
    }
    Ok(if failed {
        Doctor::CheckFailed
    } else {
        Doctor::Reported
    })
}
fn doctor_json(scheduler: &Scheduler<'_>, vendor: Vendor) -> Result<Doctor> {
    let h = health(scheduler, vendor)?;
    if let Some(notice) = h.report["orchestrators"]["notice"].as_str() {
        eprintln!("{notice}");
    }
    println!("{}", h.report);
    Ok(match h.unreadable {
        Some(unreadable) => {
            eprintln!("{}", unreadable.next_step());
            Doctor::CheckFailed
        }
        None => Doctor::Reported,
    })
}
/// The workspace a harness's connection joined, when pairing kept its name.
fn workspace(data: &Path) -> Option<String> {
    pairing::workspace(&connection(data))
}
/// What a drop reason means, as the end of "N came …".
/// The positive counts in a JSON object, largest first.
fn positive(value: &Value) -> Vec<(&str, u64)> {
    let mut out: Vec<(&str, u64)> = value
        .as_object()
        .into_iter()
        .flatten()
        .filter_map(|(key, n)| Some((key.as_str(), n.as_u64().filter(|n| *n > 0)?)))
        .collect();
    out.sort_by(|a, b| b.1.cmp(&a.1).then(a.0.cmp(b.0)));
    out
}
/// `n` drops for one reason in plain words, naming the hook events where they
/// are known: "2 Stop events repeated for a turn already recorded".
fn dropped(reason: &str, n: u64, events: &[(&str, u64)]) -> String {
    let one = n == 1;
    let (was, its) = if one {
        ("was", "its")
    } else {
        ("were", "their")
    };
    let known: u64 = events.iter().map(|(_, k)| k).sum();
    let single = match events {
        [(event, k)] if *k == n => Some(*event),
        _ => None,
    };
    let what = match single {
        Some(event) => format!("{n} {event} event{}", if one { "" } else { "s" }),
        None if events.is_empty() => n.to_string(),
        None => {
            let mut parts: Vec<_> = events.iter().map(|(e, k)| format!("{k} {e}")).collect();
            if known < n {
                parts.push(format!("{} other", n - known));
            }
            format!("{n} ({})", parts.join(", "))
        }
    };
    let repeated = |event| match event {
        "PreToolUse" | "PostToolUse" | "PostToolUseFailure" => Some("a tool call"),
        "UserPromptSubmit" | "Stop" | "Interrupt" => Some("a turn"),
        "SubagentStart" | "SubagentStop" => Some("a subagent"),
        _ => None,
    };
    let how = match reason {
        "outside_scope" => "came from sessions outside the recording scope, such as a folder that is not a git repository".into(),
        "unknown_repository" => "came from sessions in a repository this workspace doesn't track".into(),
        "spool_full" => "arrived while the outbox on this machine was full".into(),
        "permission" => "arrived while a DevDiary file or folder had the wrong owner or permissions".into(),
        "invalid" => "had an event name, session ID or working folder the hook can't use".into(),
        "observation_conflict" => match single.and_then(repeated) {
            Some(identity) => format!("repeated for {identity} already recorded"),
            None => "repeated an event already recorded".into(),
        },
        "reflog_rotated" => format!("{was} recorded without {its} commits, because the git reflog was rewritten while the command ran"),
        "revoked" => "arrived after DevDiary revoked this connection".into(),
        "stale_plan" => "arrived while the hook entries did not match this install".into(),
        "busy" => "arrived while another DevDiary process held the outbox for too long".into(),
        "error" => "failed on an unexpected error in the hook".into(),
        "invalid_json" => format!("{was} not valid JSON"),
        "invalid_metadata" => "had a value DevDiary doesn't accept in a field it sends, such as the session ID".into(),
        "input_too_large" => format!("{was} larger than the hook's 64 KiB input limit"),
        "invalid_prompt_id" => "had a prompt ID the hook can't use".into(),
        "invalid_tool_use_id" => "had a tool-use ID the hook can't use".into(),
        "invalid_agent_id" => "had an agent ID the hook can't use".into(),
        "invalid_agent_type" => "had an agent type the hook can't use".into(),
        other => format!("for the reason {other}"),
    };
    format!("{what} {how}")
}
fn events_dropped(total: u64) -> String {
    match total {
        1 => "1 event was dropped".into(),
        n => format!("{n} events were dropped"),
    }
}

/// What doctor finds for one harness: the JSON report, and the settings file
/// it could not read, if any.
struct Health {
    report: Value,
    unreadable: Option<Box<claude::UnreadableSettings>>,
}
fn health(scheduler: &Scheduler<'_>, vendor: Vendor) -> Result<Health> {
    let data = data_dir(vendor)?;
    let Some(current) = record(&data)? else {
        let stale = claude::stale_entries(&settings_path(vendor, None)?, None);
        return Ok(Health {
            report: json!({"installed": false, "timer": scheduler.state(vendor), "stale_entries": stale}),
            unreadable: None,
        });
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
    let drops = drops::counts(&store, &outbox)?;
    let report = json!({
        "installed": true,
        "runtime": vendor.runtime(),
        "host_trust": "unknown",
        "runtime_qualification": "unverified",
        "version": current.version,
        "registration": registration,
        "timer": scheduler.state(vendor),
        "pending": pending,
        "delivered": delivered,
        "drops": drops.reasons,
        "drops_by_event": drops.events,
        "orchestrators": orchestrators::summary(&outbox),
        "outside_repos": crate::outside::allowed(&outbox),
        "revoked": connection(&data).join("revoked").exists(),
        "last_scope_refresh_error": store.scope_refresh_failure()?,
        "last_receipt": receipt,
        "last_sync_error": status.as_ref().and_then(|s| s.get("last_sync_error").cloned()),
        "last_sync_at": last_sync_at(status),
        "stale_entries": claude::stale_entries(&current.settings, Some(&current.plan)),
    });
    Ok(Health { report, unreadable })
}

/// One line of doctor's readable report: a mark, what it found, why it
/// matters, and the command that fixes it.
#[derive(Debug)]
struct Finding {
    mark: Option<Mark>,
    text: String,
    why: Option<String>,
    next: Option<String>,
}
/// Doctor's findings for one harness from its JSON report: one line when it
/// records, otherwise one line for each problem, the worst first.
fn assess(
    os: Os,
    vendor: Vendor,
    settings: &Path,
    found: bool,
    workspace: Option<&str>,
    report: &Value,
) -> Vec<Finding> {
    let name = vendor.name();
    let flag = if vendor == Vendor::Codex {
        " --vendor codex"
    } else {
        ""
    };
    let finding = |mark, text: String, why: Option<String>, next: Option<String>| Finding {
        mark: Some(mark),
        text,
        why,
        next,
    };
    let stale = report["stale_entries"].as_u64().unwrap_or(0) as usize;
    let f = ui::path(settings);
    if report["installed"] != json!(true) {
        let mut rows = vec![if found {
            finding(
                Mark::Attention,
                format!("{name}: not set up."),
                None,
                Some(format!("devdiary setup{flag}")),
            )
        } else {
            Finding {
                mark: None,
                text: format!("{name}: not found on this machine."),
                why: None,
                next: None,
            }
        }];
        if stale > 0 {
            rows.push(finding(
                Mark::Attention,
                format!("{name}: {f} holds {}.", stale_named(stale)),
                Some(format!(
                    "{} no longer exists, so {} nothing. Setup offers to remove {}.",
                    if stale == 1 {
                        "Its binary or plan"
                    } else {
                        "Their binary or plan"
                    },
                    if stale == 1 {
                        "it records"
                    } else {
                        "they record"
                    },
                    if stale == 1 { "it" } else { "them" },
                )),
                Some(format!("devdiary setup{flag}")),
            ));
        }
        return rows;
    }
    let context = exit::Context {
        command: "doctor",
        vendor: Some(vendor),
    };
    let mut rows = Vec::new();
    let revoked = report["revoked"] == json!(true);
    if revoked {
        let g = exit::guidance("connection_revoked", &context);
        rows.push(finding(
            Mark::Failed,
            format!("{name}: the server revoked this connection, so the hooks record nothing."),
            g.why,
            g.next,
        ));
    }
    let registration = &report["registration"];
    match registration["registration"].as_str() {
        Some("installed") => {}
        Some("settings_unreadable") => {
            let file = registration["settings"].as_str().map_or(f.clone(), |s| ui::path(Path::new(s)));
            let fix = match registration["backup"].as_str() {
                Some(copy) => format!(
                    "Fix it by hand, or replace it with {}, the copy setup saved before a write that did not finish.",
                    ui::path(Path::new(copy))
                ),
                None => "Fix it by hand.".into(),
            };
            rows.push(finding(
                Mark::Failed,
                format!(
                    "{name}: {file} is not valid JSON: the error is at line {}, column {}.",
                    registration["line"], registration["column"]
                ),
                Some(format!("doctor cannot check the hooks in it and changed nothing. {fix}")),
                Some(format!("devdiary doctor{flag}, after you fix the JSON in {file}")),
            ));
        }
        Some("changed") => rows.push(finding(
            Mark::Failed,
            format!("{name}: DevDiary's hook entries in {f} were edited."),
            Some(
                "The hooks may not record. Restore the entries as setup wrote them, or remove them with `devdiary uninstall` and run setup again."
                    .into(),
            ),
            Some(format!("devdiary uninstall{flag}")),
        )),
        other => rows.push(finding(
            Mark::Failed,
            format!(
                "{name}: the hooks in {f} are not in place ({}).",
                other.unwrap_or("unknown")
            ),
            None,
            Some(format!("devdiary setup{flag}")),
        )),
    }
    if registration["target_settings_block"] == json!(true) {
        rows.push(finding(
            Mark::Attention,
            format!("{name}: {f} turns hooks off with disableAllHooks or allowManagedHooksOnly."),
            Some("Claude Code runs no DevDiary hook while either is set.".into()),
            Some(format!(
                "devdiary doctor{flag}, after you remove it from {f}"
            )),
        ));
    }
    let timer = &report["timer"];
    let (job, running, start) = match os {
        Os::Linux => (
            vendor.timer().to_owned(),
            timer["enabled"] == json!("enabled") && timer["active"] == json!("active"),
            format!("systemctl --user enable --now {}", vendor.timer()),
        ),
        Os::Macos => (
            vendor.label().to_owned(),
            timer["loaded"] == json!(true),
            format!(
                "launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/{}.plist",
                vendor.label()
            ),
        ),
    };
    if timer
        .as_object()
        .is_some_and(|t| t.values().any(|v| v == "unknown"))
    {
        rows.push(finding(
            Mark::Attention,
            format!("{name}: doctor could not check the sync job {job}."),
            None,
            None,
        ));
    } else if !running {
        rows.push(finding(
            Mark::Failed,
            format!("{name}: the sync job {job} is not running, so nothing is sent."),
            Some("Recorded sessions wait on this machine until it runs.".into()),
            Some(start),
        ));
    }
    if let Some(code) = report["last_sync_error"]
        .as_str()
        .filter(|c| !revoked && *c != "revoked")
    {
        let why = if exit::known_code(code) {
            exit::guidance(code, &context).what
        } else {
            "Rows stay on this machine, and the sync job tries again every 2 minutes.".into()
        };
        rows.push(finding(
            Mark::Attention,
            format!("{name}: the last sync failed ({code})."),
            Some(why),
            Some("devdiary sync".into()),
        ));
    }
    if let Some(code) = report["last_scope_refresh_error"].as_str() {
        let g = exit::guidance(code, &context);
        rows.push(finding(
            Mark::Attention,
            format!("{name}: {}", g.what),
            g.why,
            Some("devdiary sync".into()),
        ));
    }
    if let Some(notice) = report["orchestrators"]["notice"].as_str() {
        rows.push(finding(
            Mark::Attention,
            format!("{name}: {notice}"),
            None,
            Some(format!("devdiary orchestrators approve{flag}")),
        ));
    }
    let target = workspace.map_or("DevDiary".into(), |w| format!("the workspace {w}"));
    let say = |(reason, n): &(&str, u64)| {
        dropped(reason, *n, &positive(&report["drops_by_event"][*reason]))
    };
    // A rewritten reflog loses an event's commits, not the event.
    let (unlinked, drops): (Vec<_>, Vec<_>) = positive(&report["drops"])
        .into_iter()
        .partition(|(reason, _)| *reason == "reflog_rotated");
    let total: u64 = drops.iter().map(|(_, n)| n).sum();
    let delivered = report["delivered"].as_u64().unwrap_or(0);
    let has = |reason| drops.iter().any(|(r, _)| *r == reason);
    if total > 0 && delivered == 0 {
        let why: Vec<_> = drops.iter().map(|d| format!("{}.", say(d))).collect();
        let mut todo = Vec::new();
        if has("unknown_repository") {
            todo.push("track the repository in DevDiary".to_owned());
        }
        if has("outside_scope") && report["outside_repos"] != json!(true) {
            todo.push(format!("run devdiary outside-repos on{flag}"));
        }
        rows.push(finding(
            Mark::Attention,
            format!(
                "{name} records to {target}, but nothing was sent: {}.",
                events_dropped(total)
            ),
            Some(why.join(" ")),
            (!todo.is_empty()).then(|| todo.join(", or ")),
        ));
    }
    if rows.is_empty() {
        rows.push(finding(
            Mark::Done,
            format!(
                "{name} records to {target}: {} waiting to send, {delivered} sent, last sync {}.",
                report["pending"],
                report["last_sync_at"].as_str().unwrap_or("not yet")
            ),
            None,
            None,
        ));
    }
    if total > 0 && delivered > 0 {
        let reasons: Vec<_> = drops.iter().map(say).collect();
        rows.push(Finding {
            mark: None,
            text: format!("{}: {}.", events_dropped(total), reasons.join("; ")),
            why: None,
            next: None,
        });
    }
    for lost in &unlinked {
        rows.push(Finding {
            mark: None,
            text: format!("{}.", say(lost)),
            why: None,
            next: None,
        });
    }
    // Stale entries do nothing, so they never hide that the harness records.
    if stale > 0 {
        let (its, it) = if stale == 1 {
            ("Its", "it")
        } else {
            ("Their", "them")
        };
        rows.push(finding(
            Mark::Attention,
            format!("{name}: {f} also holds {}.", stale_named(stale)),
            Some(format!(
                "{its} binary or plan no longer exists, so {} nothing. Remove {it} from {f} by hand: setup removes stale entries only while it sets a harness up.",
                if stale == 1 { "it records" } else { "they record" }
            )),
            None,
        ));
    }
    // The worst first: failures, then what needs attention.
    rows.sort_by_key(|r| r.mark != Some(Mark::Failed));
    rows
}

/// One harness's change in two rows: what it adds or replaces in which file,
/// then the sync job or the new version. `old` is the version an upgrade replaces.
/// `stale` entries to remove come first, on their own row.
fn summary(
    os: Os,
    vendor: Vendor,
    entries: usize,
    settings: &Path,
    old: Option<&str>,
    stale: usize,
) -> Vec<String> {
    let name = format!("{:<14}", vendor.name());
    let under = " ".repeat(14);
    let settings = settings.display();
    let mut rows = match old {
        Some(old) => vec![
            format!("replaces its hooks of version {old} in {settings}"),
            format!("with {entries} hooks of version {VERSION}"),
        ],
        None => vec![
            format!("adds {entries} hooks to {settings}"),
            format!(
                "and a sync job, {}, that runs every 2 minutes",
                match os {
                    Os::Linux => vendor.timer().to_owned(),
                    Os::Macos => format!("the launch agent {}", vendor.label()),
                }
            ),
        ],
    };
    if stale > 0 {
        let hooks = if stale == 1 { "hook" } else { "hooks" };
        rows.insert(
            0,
            format!("removes {stale} stale DevDiary {hooks} from {settings}"),
        );
    }
    rows.iter()
        .enumerate()
        .map(|(i, row)| format!("{}{row}", if i == 0 { &name } else { &under }))
        .collect()
}
/// "1 stale DevDiary hook entry", "10 stale DevDiary hook entries".
fn stale_named(n: usize) -> String {
    format!(
        "{n} stale DevDiary hook {}",
        if n == 1 { "entry" } else { "entries" }
    )
}
fn macos_note(os: Os, vendor: Vendor) -> Option<String> {
    (os == Os::Macos).then(|| format!(
        "macOS will show a background-item notification and list the {} agent under Login Items. Switching it off there stops delivery.",
        vendor.label()
    ))
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
    // Between a login's retirement and its approval the folder holds no
    // connection, and the next pairing needs it left as it is.
    if !root.join("connection.json").exists() {
        return Err("not_paired".into());
    }
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
            Ok(()) => Out::stdout().line(
                "",
                Some(Mark::Done),
                None,
                &format!("Synced {}.", vendor.runtime()),
            ),
            Err(error) => {
                first_error.get_or_insert(error);
            }
        }
    }
    if !sent {
        Out::stdout().line(
            "",
            Some(Mark::Attention),
            None,
            "Not installed; nothing to send. Run `devdiary setup`.",
        );
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
    fn on_macos_the_consent_step_names_the_login_item_and_what_switching_it_off_does() {
        let note = macos_note(Os::Macos, Vendor::Codex).unwrap();
        for part in [
            "background-item notification",
            "Login Items",
            "Switching it off there stops delivery",
            "me.devdiary.codex-sync",
        ] {
            assert!(note.contains(part), "{part}");
        }
        assert_eq!(macos_note(Os::Linux, Vendor::Codex), None);
    }

    #[test]
    fn the_consent_step_summarises_the_hooks_and_the_sync_job() {
        let settings = Path::new("/home/ada/.claude/settings.json");
        assert_eq!(
            summary(Os::Linux, Vendor::Claude, 10, settings, None, 0),
            [
                "Claude Code   adds 10 hooks to /home/ada/.claude/settings.json",
                "              and a sync job, devdiary-claude-sync.timer, that runs every 2 minutes",
            ]
        );
        assert_eq!(
            summary(Os::Macos, Vendor::Codex, 10, settings, None, 0)[1],
            "              and a sync job, the launch agent me.devdiary.codex-sync, that runs every 2 minutes"
        );
        assert_eq!(
            summary(Os::Linux, Vendor::Claude, 10, settings, Some("0.3.0"), 0),
            [
                "Claude Code   replaces its hooks of version 0.3.0 in /home/ada/.claude/settings.json",
                &format!("              with 10 hooks of version {VERSION}"),
            ]
        );
    }

    #[test]
    fn stale_entries_to_remove_are_their_own_line_of_the_consent_summary() {
        let settings = Path::new("/home/ada/.codex/hooks.json");
        assert_eq!(
            summary(Os::Linux, Vendor::Codex, 10, settings, None, 10),
            [
                "Codex         removes 10 stale DevDiary hooks from /home/ada/.codex/hooks.json",
                "              adds 10 hooks to /home/ada/.codex/hooks.json",
                "              and a sync job, devdiary-codex-sync.timer, that runs every 2 minutes",
            ]
        );
        assert_eq!(
            summary(Os::Linux, Vendor::Codex, 10, settings, None, 1)[0],
            "Codex         removes 1 stale DevDiary hook from /home/ada/.codex/hooks.json"
        );
    }

    /// An entry of `exe` and `plan` under Stop.
    fn found(exe: &Path, plan: &Path) -> claude::Entry {
        claude::Entry {
            event: "Stop".into(),
            command: format!(
                "'{}' hook codex '{}' >/dev/null 2>&1 || :",
                exe.display(),
                plan.display()
            ),
            executable: exe.into(),
            plan: plan.into(),
        }
    }

    #[test]
    fn only_entries_that_are_all_stale_are_offered_for_removal() {
        let dir = crate::tempdir();
        let data = dir.path().join("cli/codex");
        let settings = dir.path().join("hooks.json");
        let gone = found(
            &dir.path().join("gone/devdiary"),
            &dir.path().join("gone/plan.json"),
        );
        assert_eq!(
            left_behind(
                Vendor::Codex,
                &data,
                &settings,
                vec![gone.clone(), gone.clone()]
            )
            .unwrap(),
            [gone.clone(), gone.clone()]
        );
        assert!(
            left_behind(Vendor::Codex, &data, &settings, vec![])
                .unwrap()
                .is_empty()
        );

        // A binary and plan that exist belong to an install, so setup stops.
        let other = dir.path().join("other");
        fs::create_dir_all(&other).unwrap();
        for name in ["devdiary", "plan.json"] {
            fs::write(other.join(name), b"").unwrap();
        }
        let working = found(&other.join("devdiary"), &other.join("plan.json"));
        let error =
            left_behind(Vendor::Codex, &data, &settings, vec![gone.clone(), working]).unwrap_err();
        let refusal = error.downcast_ref::<exit::Refusal>().unwrap();
        assert_eq!(
            (refusal.code, refusal.file.as_path()),
            ("already_registered", settings.as_path())
        );
        let detail = refusal.detail.as_deref().unwrap();
        assert!(
            detail.starts_with("2 DevDiary hook entries, and 1 of them runs "),
            "{detail}"
        );
        assert!(
            detail.contains(&ui::path(&other.join("devdiary"))),
            "{detail}"
        );
        assert!(detail.contains("devdiary uninstall"), "{detail}");
        assert!(detail.contains("devdiary upgrade"), "{detail}");
        assert_eq!(refusal.next, None);
    }

    #[test]
    fn entries_of_an_earlier_setup_on_this_machine_point_to_uninstall() {
        let dir = crate::tempdir();
        let data = dir.path().join("cli/codex");
        fs::create_dir_all(data.join("plans")).unwrap();
        fs::create_dir_all(data.join(VERSION)).unwrap();
        let (exe, plan) = (
            data.join(VERSION).join("devdiary"),
            data.join("plans/codex-1.json"),
        );
        for path in [&exe, &plan] {
            fs::write(path, b"").unwrap();
        }
        let settings = dir.path().join("hooks.json");
        let error =
            left_behind(Vendor::Codex, &data, &settings, vec![found(&exe, &plan)]).unwrap_err();
        let refusal = error.downcast_ref::<exit::Refusal>().unwrap();
        assert_eq!(
            refusal.detail.as_deref(),
            Some(
                "1 DevDiary hook entry from an earlier setup on this machine that stopped before it saved its install record"
            )
        );
        assert_eq!(
            refusal.next.as_deref(),
            Some("devdiary uninstall --vendor codex")
        );
    }

    #[test]
    fn the_checklist_names_the_workspace_when_setup_paired_this_run() {
        assert_eq!(
            recording(Vendor::Codex, Some("TWL")),
            "Codex records to the workspace TWL"
        );
        assert_eq!(
            recording(Vendor::Claude, None),
            "Claude Code records to DevDiary"
        );
    }

    fn healthy() -> Value {
        json!({
            "installed": true,
            "version": VERSION,
            "registration": {"registration": "installed", "target_settings_block": false},
            "timer": {"enabled": "enabled", "active": "active"},
            "pending": 2,
            "delivered": 12,
            "orchestrators": {"notice": null},
            "revoked": false,
            "last_scope_refresh_error": null,
            "last_sync_error": null,
            "last_sync_at": "2026-11-22T00:10:00Z",
        })
    }
    fn check(report: Value) -> Vec<Finding> {
        assess(
            Os::Linux,
            Vendor::Claude,
            Path::new("/s.json"),
            true,
            None,
            &report,
        )
    }

    #[test]
    fn a_working_harness_is_one_line_with_its_counts() {
        let rows = check(healthy());
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].mark, Some(Mark::Done));
        assert_eq!(
            rows[0].text,
            "Claude Code records to DevDiary: 2 waiting to send, 12 sent, last sync 2026-11-22T00:10:00Z."
        );
        assert_eq!(rows[0].next, None);
    }

    fn with_drops(delivered: u64, outside_repos: bool) -> Vec<Finding> {
        let mut report = healthy();
        report["delivered"] = json!(delivered);
        report["outside_repos"] = json!(outside_repos);
        report["drops"] = json!({"outside_scope": 2, "unknown_repository": 4});
        assess(
            Os::Linux,
            Vendor::Claude,
            Path::new("/s.json"),
            true,
            Some("TWL"),
            &report,
        )
    }

    #[test]
    fn a_working_harness_names_the_workspace_it_sends_to() {
        let rows = assess(
            Os::Linux,
            Vendor::Claude,
            Path::new("/s.json"),
            true,
            Some("TWL"),
            &healthy(),
        );
        assert_eq!(
            rows[0].text,
            "Claude Code records to the workspace TWL: 2 waiting to send, 12 sent, last sync 2026-11-22T00:10:00Z."
        );
    }

    #[test]
    fn drops_with_nothing_sent_need_attention_and_say_what_each_reason_means() {
        let rows = with_drops(0, false);
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].mark, Some(Mark::Attention));
        assert_eq!(
            rows[0].text,
            "Claude Code records to the workspace TWL, but nothing was sent: 6 events were dropped."
        );
        let why = rows[0].why.as_deref().unwrap();
        assert!(
            why.contains("4 came from sessions in a repository this workspace doesn't track."),
            "{why}"
        );
        assert!(
            why.contains("2 came from sessions outside the recording scope"),
            "{why}"
        );
        assert_eq!(
            rows[0].next.as_deref(),
            Some("track the repository in DevDiary, or run devdiary outside-repos on")
        );
        let on = with_drops(0, true);
        assert_eq!(
            on[0].next.as_deref(),
            Some("track the repository in DevDiary")
        );
    }

    #[test]
    fn drops_beside_deliveries_are_a_dim_line_under_the_working_harness() {
        let rows = with_drops(12, false);
        assert_eq!(rows.len(), 2);
        assert_eq!(rows[0].mark, Some(Mark::Done));
        assert_eq!(rows[1].mark, None);
        assert_eq!(rows[1].next, None);
        assert!(
            rows[1].text.starts_with("6 events were dropped:"),
            "{}",
            rows[1].text
        );
        assert!(rows[1].text.contains("doesn't track"), "{}", rows[1].text);
    }

    #[test]
    fn every_drop_reason_has_plain_words() {
        let cases = [
            (
                "outside_scope",
                "came from sessions outside the recording scope, such as a folder that is not a git repository",
            ),
            (
                "unknown_repository",
                "came from sessions in a repository this workspace doesn't track",
            ),
            (
                "spool_full",
                "arrived while the outbox on this machine was full",
            ),
            (
                "permission",
                "arrived while a DevDiary file or folder had the wrong owner or permissions",
            ),
            (
                "invalid",
                "had an event name, session ID or working folder the hook can't use",
            ),
            ("observation_conflict", "repeated an event already recorded"),
            (
                "reflog_rotated",
                "were recorded without their commits, because the git reflog was rewritten while the command ran",
            ),
            ("revoked", "arrived after DevDiary revoked this connection"),
            (
                "stale_plan",
                "arrived while the hook entries did not match this install",
            ),
            (
                "busy",
                "arrived while another DevDiary process held the outbox for too long",
            ),
            ("error", "failed on an unexpected error in the hook"),
            ("invalid_json", "were not valid JSON"),
            (
                "invalid_metadata",
                "had a value DevDiary doesn't accept in a field it sends, such as the session ID",
            ),
            (
                "input_too_large",
                "were larger than the hook's 64 KiB input limit",
            ),
            ("invalid_prompt_id", "had a prompt ID the hook can't use"),
            (
                "invalid_tool_use_id",
                "had a tool-use ID the hook can't use",
            ),
            ("invalid_agent_id", "had an agent ID the hook can't use"),
            ("invalid_agent_type", "had an agent type the hook can't use"),
        ];
        assert_eq!(cases.len(), drops::REASONS.len());
        for (reason, words) in cases {
            assert!(drops::REASONS.contains(&reason), "{reason}");
            assert_eq!(dropped(reason, 2, &[]), format!("2 {words}"), "{reason}");
        }
    }

    #[test]
    fn one_drop_reads_in_the_singular() {
        assert_eq!(dropped("invalid_json", 1, &[]), "1 was not valid JSON");
        assert_eq!(
            dropped("reflog_rotated", 1, &[("PostToolUse", 1)]),
            "1 PostToolUse event was recorded without its commits, because the git reflog was rewritten while the command ran"
        );
    }

    #[test]
    fn a_repeat_names_the_event_and_what_it_repeated() {
        assert_eq!(
            dropped("observation_conflict", 2, &[("Stop", 2)]),
            "2 Stop events repeated for a turn already recorded"
        );
        assert_eq!(
            dropped("observation_conflict", 1, &[("PreToolUse", 1)]),
            "1 PreToolUse event repeated for a tool call already recorded"
        );
        assert_eq!(
            dropped("observation_conflict", 1, &[("SubagentStop", 1)]),
            "1 SubagentStop event repeated for a subagent already recorded"
        );
        assert_eq!(
            dropped(
                "observation_conflict",
                3,
                &[("Stop", 2), ("PostToolUse", 1)]
            ),
            "3 (2 Stop, 1 PostToolUse) repeated an event already recorded"
        );
    }

    #[test]
    fn drops_with_only_some_events_known_count_the_rest_as_other() {
        assert_eq!(
            dropped("unknown_repository", 4, &[("SessionStart", 3)]),
            "4 (3 SessionStart, 1 other) came from sessions in a repository this workspace doesn't track"
        );
        assert_eq!(
            dropped("unknown_repository", 3, &[("SessionStart", 3)]),
            "3 SessionStart events came from sessions in a repository this workspace doesn't track"
        );
    }

    #[test]
    fn doctor_names_the_repeated_event_beside_deliveries() {
        let mut report = healthy();
        report["drops"] = json!({"observation_conflict": 2});
        report["drops_by_event"] = json!({"observation_conflict": {"Stop": 2}});
        let rows = check(report);
        assert_eq!(rows.len(), 2);
        assert_eq!(
            rows[1].text,
            "2 events were dropped: 2 Stop events repeated for a turn already recorded."
        );
    }

    #[test]
    fn commits_lost_to_a_rewritten_reflog_are_not_counted_as_dropped_events() {
        let mut report = healthy();
        report["delivered"] = json!(0);
        report["drops"] = json!({"reflog_rotated": 1});
        let rows = check(report);
        assert_eq!(rows.len(), 2);
        assert_eq!(rows[0].mark, Some(Mark::Done));
        assert_eq!(
            rows[1].text,
            "1 was recorded without its commits, because the git reflog was rewritten while the command ran."
        );
        let mut report = healthy();
        report["drops"] = json!({"reflog_rotated": 1, "busy": 2});
        let rows = check(report);
        assert_eq!(
            rows[1].text,
            "2 events were dropped: 2 arrived while another DevDiary process held the outbox for too long."
        );
        assert!(
            rows[2]
                .text
                .starts_with("1 was recorded without its commits")
        );
    }

    #[test]
    fn a_harness_not_set_up_names_setup_when_it_is_on_this_machine() {
        let missing = json!({"installed": false, "timer": {}});
        let rows = assess(
            Os::Linux,
            Vendor::Codex,
            Path::new("/h.json"),
            true,
            None,
            &missing,
        );
        assert_eq!(rows[0].mark, Some(Mark::Attention));
        assert_eq!(rows[0].text, "Codex: not set up.");
        assert_eq!(
            rows[0].next.as_deref(),
            Some("devdiary setup --vendor codex")
        );
        let absent = assess(
            Os::Linux,
            Vendor::Codex,
            Path::new("/h.json"),
            false,
            None,
            &missing,
        );
        assert_eq!((absent[0].mark, absent[0].next.as_deref()), (None, None));
        assert_eq!(absent[0].text, "Codex: not found on this machine.");
    }

    #[test]
    fn doctor_names_stale_entries_and_what_removes_them() {
        let missing = json!({"installed": false, "timer": {}, "stale_entries": 10});
        let rows = assess(
            Os::Linux,
            Vendor::Codex,
            Path::new("/h.json"),
            true,
            None,
            &missing,
        );
        assert_eq!(rows.len(), 2, "{rows:?}");
        assert_eq!(rows[0].text, "Codex: not set up.");
        assert_eq!(
            rows[0].next.as_deref(),
            Some("devdiary setup --vendor codex")
        );
        assert_eq!(rows[1].mark, Some(Mark::Attention));
        assert_eq!(
            rows[1].text,
            "Codex: /h.json holds 10 stale DevDiary hook entries."
        );
        assert_eq!(
            rows[1].why.as_deref(),
            Some(
                "Their binary or plan no longer exists, so they record nothing. Setup offers to remove them."
            )
        );

        let mut report = healthy();
        report["stale_entries"] = json!(1);
        let rows = check(report);
        assert_eq!(rows.len(), 2, "{rows:?}");
        assert_eq!(rows[0].mark, Some(Mark::Done));
        assert_eq!(rows[1].mark, Some(Mark::Attention));
        assert_eq!(
            rows[1].text,
            "Claude Code: /s.json also holds 1 stale DevDiary hook entry."
        );
        assert_eq!(
            rows[1].why.as_deref(),
            Some(
                "Its binary or plan no longer exists, so it records nothing. Remove it from /s.json by hand: setup removes stale entries only while it sets a harness up."
            )
        );
        assert_eq!(rows[1].next, None);
    }

    #[test]
    fn each_problem_gets_a_mark_a_reason_and_a_next_command() {
        let mut stopped = healthy();
        stopped["timer"] = json!({"enabled": "disabled", "active": "inactive"});
        let rows = check(stopped);
        assert_eq!(rows[0].mark, Some(Mark::Failed));
        assert!(
            rows[0]
                .text
                .contains("devdiary-claude-sync.timer is not running")
        );
        assert_eq!(
            rows[0].next.as_deref(),
            Some("systemctl --user enable --now devdiary-claude-sync.timer")
        );

        let mut revoked = healthy();
        revoked["revoked"] = json!(true);
        revoked["last_sync_error"] = json!("revoked");
        let rows = check(revoked);
        assert_eq!(
            rows.len(),
            1,
            "a revoked connection is not also a failed sync"
        );
        assert_eq!(rows[0].mark, Some(Mark::Failed));

        let mut failing = healthy();
        failing["last_sync_error"] = json!("http_503");
        failing["orchestrators"] = json!({"notice": "paperclip names wait for approval."});
        let rows = check(failing);
        assert_eq!(rows.len(), 2);
        assert!(rows.iter().all(|r| r.mark == Some(Mark::Attention)));
        assert_eq!(rows[0].next.as_deref(), Some("devdiary sync"));
        assert_eq!(
            rows[1].next.as_deref(),
            Some("devdiary orchestrators approve")
        );

        let mut edited = healthy();
        edited["registration"]["registration"] = json!("changed");
        let rows = check(edited);
        assert!(rows[0].text.contains("/s.json"), "{}", rows[0].text);
        assert!(rows[0].why.is_some());
    }

    #[test]
    fn unreadable_settings_name_the_file_the_position_and_the_fix() {
        let mut broken = healthy();
        broken["registration"] = json!({"registration": "settings_unreadable",
            "settings": "/s.json", "line": 3, "column": 3, "backup": "/s.json.devdiary-backup"});
        let rows = check(broken);
        assert_eq!(rows[0].mark, Some(Mark::Failed));
        assert_eq!(
            rows[0].text,
            "Claude Code: /s.json is not valid JSON: the error is at line 3, column 3."
        );
        assert!(
            rows[0]
                .why
                .as_deref()
                .unwrap()
                .contains("/s.json.devdiary-backup")
        );
        assert_eq!(
            rows[0].next.as_deref(),
            Some("devdiary doctor, after you fix the JSON in /s.json")
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
