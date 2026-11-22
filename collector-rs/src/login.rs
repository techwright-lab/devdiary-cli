//! `devdiary login`: move this machine to another DevDiary account. It sends
//! what waits to the old account, revokes the old connections, sets their
//! folders aside, pairs every harness that is set up with one approval and
//! points the hooks at the new connections. The sync jobs stay as they are:
//! they run the same pinned copy on the same connection folder.
use crate::{
    Result, claude, exit,
    install::{self, Journal, Registration, Stopped},
    pairing, platform,
    scheduler::Scheduler,
    store::Store,
    ui::{self, Mark, Out},
    vendor::Vendor,
};
use serde_json::json;
use std::{cell::Cell, path::PathBuf};

pub struct Options<'a> {
    pub origin: Option<&'a str>,
    pub trust_origin: bool,
    pub browser: bool,
}

/// Where a harness that is set up stands before login changes anything.
enum Standing {
    /// Its hooks record to its saved connection, which the server may have revoked.
    Recording { revoked: bool },
    /// An earlier login paired it, but its hooks still name the old connection.
    Moved,
    /// It has no saved connection.
    Unpaired,
}

struct Harness {
    vendor: Vendor,
    root: PathBuf,
    plan: PathBuf,
    settings: PathBuf,
    standing: Standing,
    /// Whether its connection records orchestrated runs outside repositories.
    outside: bool,
    registration: Registration,
    shown: Vec<String>,
}

pub fn login(o: Options<'_>, scheduler: &Scheduler<'_>) -> Result<()> {
    let mut journal = Journal::default();
    let result = guided(&o, scheduler, &mut journal);
    install::reported("login", None, &journal, result)
}

fn guided(o: &Options<'_>, scheduler: &Scheduler<'_>, journal: &mut Journal) -> Result<()> {
    let out = Out::stdout();
    out.header();
    install::require_consent_channel(false)?;
    out.step(1, "Check this machine");
    platform::preflight(scheduler)?;
    let origin = o.origin.unwrap_or(pairing::DEFAULT_ORIGIN);
    pairing::origin(origin, o.trust_origin)?;
    let harnesses = check()?;

    out.step(2, "Choose what DevDiary may change");
    let recording = harnesses
        .iter()
        .any(|h| matches!(h.standing, Standing::Recording { .. }));
    for h in &harnesses {
        let name = format!("{:<14}", h.vendor.name());
        let settings = ui::path(&h.settings);
        out.raw(&format!(
            "  {name}replaces its {} hooks in {settings}",
            h.shown.len()
        ));
        out.raw(&format!(
            "  {}so they record to the new connection; its sync job stays",
            " ".repeat(14)
        ));
    }
    if recording {
        out.say(
            "First, what waits is sent to the account this machine records to now, and its connection there is revoked. What cannot be sent stays on this machine and is never sent.",
        );
    }
    out.dim("Type d to see the exact entries first.");
    install::consent_to(
        if recording {
            "Sign this machine in to another account?"
        } else {
            "Finish signing this machine in?"
        },
        &|| {
            for h in &harnesses {
                h.registration.print(&h.shown);
            }
        },
    )?;

    out.step(3, "Retire the old connections");
    retire(&harnesses, journal)?;

    out.step(4, "Approve this machine in DevDiary");
    pair(o, origin, &harnesses, journal)?;
    install::pause("login_paired");
    let mut stopped = Vec::new();
    let mut ready = Vec::new();
    for h in &harnesses {
        match rewrite(h, scheduler, journal) {
            Ok(()) => ready.push(h),
            Err(error) => {
                let code = exit::code(error.as_ref());
                out.failed(&format!("{}: stopped with {code}.", h.vendor.name()));
                stopped.push(Stopped {
                    vendor: h.vendor,
                    error,
                });
            }
        }
    }
    if let Some(first) = stopped.into_iter().next() {
        return Err(Box::new(first));
    }

    out.raw("");
    for h in &ready {
        let workspace = pairing::workspace(&h.root);
        let done = install::recording(h.vendor, workspace.as_deref());
        out.line("", Some(Mark::Done), None, &done);
    }
    if ready.iter().any(|h| h.vendor == Vendor::Codex) {
        out.line("", Some(Mark::Attention), None, install::CODEX_TRUST);
    }
    out.line("", Some(Mark::Done), None, "Sync runs every 2 minutes");
    out.raw("");
    journal.print(out);
    out.raw("");
    out.next("devdiary doctor");
    Ok(())
}

/// Step 1: each harness that is set up, where it records now, and the hook
/// entries that would replace its own.
fn check() -> Result<Vec<Harness>> {
    let out = Out::stdout();
    let mut harnesses = Vec::new();
    for vendor in [Vendor::Claude, Vendor::Codex] {
        let data = install::data_dir(vendor)?;
        let Some(record) = install::record(&data)? else {
            continue;
        };
        claude::preflight_replacing(&record.plan)?;
        let root = install::connection(&data);
        let scope = claude::plan_scope(&record.plan)?;
        let name = vendor.name();
        let at = || {
            pairing::workspace(&root)
                .map_or("DevDiary".into(), |name| format!("the workspace {name}"))
        };
        let standing = if !root.join("connection.json").exists() {
            out.attention(&format!(
                "{name}: not paired. An earlier `devdiary login` may have stopped before the approval."
            ));
            Standing::Unpaired
        } else if Store::open(&root.join("outbox"), None)?.config != scope {
            out.attention(&format!(
                "{name}: connected to {}, but its hook entries still name the old connection.",
                at()
            ));
            Standing::Moved
        } else if pairing::revoked(&root.join("outbox")) {
            out.attention(&format!("{name}: its connection to {} was revoked.", at()));
            Standing::Recording { revoked: true }
        } else {
            out.done(&install::recording(
                vendor,
                pairing::workspace(&root).as_deref(),
            ));
            Standing::Recording { revoked: false }
        };
        let registration = Registration::new(
            &data,
            &record.settings,
            &record.executable,
            vendor,
            &record.version,
        )?;
        harnesses.push(Harness {
            vendor,
            root,
            plan: record.plan,
            settings: record.settings,
            standing,
            outside: scope.get("record_orchestrated_outside_repos") == Some(&json!(true)),
            shown: registration.entries()?,
            registration,
        });
    }
    if harnesses.is_empty() {
        return Err("not_installed".into());
    }
    Ok(harnesses)
}

/// Step 3: send what waits on each old connection, revoke them all, and only
/// then set their folders aside. Any revoke that fails stops login before
/// anything on this machine changes.
fn retire(harnesses: &[Harness], journal: &mut Journal) -> Result<()> {
    let out = Out::stdout();
    let old: Vec<_> = harnesses
        .iter()
        .filter(|h| matches!(h.standing, Standing::Recording { .. }))
        .collect();
    for h in &old {
        if matches!(h.standing, Standing::Recording { revoked: true }) {
            continue;
        }
        let (_, before) = install::counts(&h.root)?;
        install::timer_sync(&h.root)?;
        let sent = install::counts(&h.root)?.1 - before;
        if sent > 0 {
            let line = format!(
                "{}: sent {} to the old account.",
                h.vendor.name(),
                events(sent)
            );
            out.done(&line);
            journal.sent.push(line);
        }
    }
    for h in &old {
        let now = pairing::revoke(&h.root)?;
        let name = h.vendor.name();
        journal
            .sent
            .push(format!("{name}: a request to revoke its old connection."));
        out.done(&if now {
            format!("{name}: its old connection is revoked.")
        } else {
            format!("{name}: its old connection was already revoked.")
        });
    }
    let mut moved = false;
    for h in harnesses {
        let name = h.vendor.name();
        if matches!(h.standing, Standing::Unpaired) && pairing::stranded(&h.root)? {
            // A successful exchange keeps the marker too, so it only means
            // something in a folder that never received its connection.
            let attempted = pairing::attempted(&h.root);
            let shown = ui::path(&pairing::retire(&h.root)?);
            let line = format!("{name}: moved the folder an earlier pairing left to {shown}.");
            out.done(&line);
            journal.changed.push(line);
            if attempted {
                out.attention(&format!(
                    "{name}: that pairing may have left a connection in DevDiary that this machine never received. Revoke it under Settings › Collectors in the account you approved it in."
                ));
            }
            moved = true;
            continue;
        }
        if !matches!(h.standing, Standing::Recording { .. }) {
            continue;
        }
        let retired = pairing::retire(&h.root)?;
        let shown = ui::path(&retired);
        journal.changed.push(format!(
            "{name}: moved its old connection folder to {shown}."
        ));
        moved = true;
        let waiting = install::counts(&retired).map_or(0, |(pending, _)| pending);
        if waiting == 1 {
            out.attention(&format!(
                "{name}: 1 event that was not sent stays in {shown} and is never sent."
            ));
        } else if waiting > 1 {
            out.attention(&format!(
                "{name}: {waiting} events that were not sent stay in {shown} and are never sent."
            ));
        } else {
            out.done(&format!(
                "{name}: moved its old connection folder to {shown}."
            ));
        }
    }
    if !moved {
        out.say("No saved connection to retire.");
    }
    Ok(())
}
fn events(n: i64) -> String {
    match n {
        1 => "1 event".into(),
        n => format!("{n} events"),
    }
}

/// Step 4's one approval, for every harness without a saved connection now.
fn pair(o: &Options<'_>, origin: &str, harnesses: &[Harness], journal: &mut Journal) -> Result<()> {
    let unpaired: Vec<_> = harnesses
        .iter()
        .filter(|h| !h.root.join("connection.json").exists())
        .collect();
    if unpaired.is_empty() {
        return Ok(());
    }
    Out::stdout().say("Approve while signed in to the account this machine should record to.");
    let request: Vec<_> = unpaired
        .iter()
        .map(|h| pairing::Harness {
            vendor: h.vendor,
            root: &h.root,
        })
        .collect();
    let sent = Cell::new(false);
    let paired = pairing::setup(pairing::Options {
        harnesses: &request,
        origin,
        trust_origin: o.trust_origin,
        browser: o.browser,
        // Never widens what a harness records: on only if every one had it on.
        record_orchestrated_outside_repos: unpaired.iter().all(|h| h.outside),
        sent: &sent,
    });
    if sent.get() {
        let vendors: Vec<_> = unpaired.iter().map(|h| h.vendor).collect();
        journal.sent.push(install::pairing_request(&vendors));
    }
    for h in &unpaired {
        if h.root.join("connection.json").exists() {
            journal.changed.push(format!(
                "{}: saved its new connection in {}.",
                h.vendor.name(),
                ui::path(&h.root)
            ));
        }
    }
    paired.map(|_| ()).map_err(|error| {
        if exit::known_code(&error.to_string()) {
            error
        } else {
            "login_incomplete".into()
        }
    })
}

/// Replaces a harness's hook entries with ones for its new connection, through
/// the same plan and checks as `upgrade`, and keeps its pinned copy and sync job.
fn rewrite(h: &Harness, scheduler: &Scheduler<'_>, journal: &mut Journal) -> Result<()> {
    claude::remove(&h.plan)?;
    h.registration.plan(&h.shown, None)?;
    let name = h.vendor.name();
    h.registration.write(false, &mut |done| {
        journal.changed.push(format!("{name}: {done}."))
    })?;
    h.registration.check(scheduler.os)
}
