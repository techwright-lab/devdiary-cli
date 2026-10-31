#![forbid(unsafe_code)]
use devdiary_collector::{
    MAX_HOOK_INPUT, MAX_INPUT, Result, drops, install, orchestrators,
    store::{Report, Store, repair, sync_outcome},
    systemd::Systemctl,
};
use std::{io::Read, path::Path, sync::mpsc, time::Duration};

// Static refusal codes only; other errors can carry input or paths.
const REPORTED: [&str; 5] = [
    "python_observer_registered",
    "already_registered",
    "unsafe_settings",
    "stale_plan",
    "repair_required",
];
const INSTALL_REPORTED: [&str; 19] = [
    "approval_required",
    "offered_list_unreadable",
    "connection_revoked",
    "connection_active",
    "consent_required",
    "consent_declined",
    "stale_plan",
    "invalid_settings",
    "settings_missing",
    "already_installed",
    "not_installed",
    "timer_unavailable",
    "timer_unconfirmed",
    "self_test_failed",
    "unsupported_path",
    "home_required",
    "setup_incomplete",
    "owned_entry_changed",
    "owned_entry_missing",
];
fn install_hint(code: &str) -> &'static str {
    match code {
        "approval_required" => {
            "Approving orchestrator names needs a person at a terminal. No flag replaces this. Run this command in a terminal."
        }
        "offered_list_unreadable" => {
            "The saved orchestrator offer cannot be read. The next sync replaces it; run this command again after that."
        }
        "consent_required" => {
            "Run install in a terminal to review the hook entries, or pass --yes."
        }
        "consent_declined" => "Nothing was registered. Settings and timers are unchanged.",
        "stale_plan" => {
            "Settings changed after the plan was made and were left as they are. Run install again."
        }
        "invalid_settings" => {
            "The Claude settings file is not valid JSON. Fix it, then run install again."
        }
        "already_installed" => "Already installed. Use upgrade to re-register, or uninstall.",
        "not_installed" => "Not installed. Use install.",
        "settings_missing" => {
            "No Claude settings file at that path. Start Claude Code once, or pass --settings with an absolute path, then run install again."
        }
        "timer_unavailable" => {
            "Hooks are registered but systemctl --user could not enable the timer. Run doctor."
        }
        "timer_unconfirmed" => {
            "Hooks and timer unit files are removed, but systemctl --user could not confirm the timer stopped. In a user session run: systemctl --user disable --now devdiary-collector-sync.timer"
        }
        "unsupported_path" => {
            "Use an absolute path without the characters \" \\ % or $, which systemd would reinterpret."
        }
        "connection_revoked" => {
            "The server revoked the saved connection, so the hooks would record nothing. Run uninstall if it is installed, then install --new-pair to set the old connection aside and pair again."
        }
        "connection_active" => {
            "The saved connection was not revoked. Run install without --new-pair to reuse it."
        }
        "self_test_failed" => {
            "Hooks and the timer are in place, but the installed hook did not record the local test event. Run doctor to see the drop reason, or uninstall."
        }
        "setup_incomplete" => devdiary_collector::pairing::SETUP_INCOMPLETE,
        "owned_entry_changed" | "owned_entry_missing" => {
            "The collector's hook entries in the settings were edited, so the settings were left as they are. Restore or delete those entries, then run uninstall again."
        }
        _ => "",
    }
}
fn install_options(args: &[String]) -> Result<install::Options<'_>> {
    let mut o = install::Options {
        origin: None,
        trust_origin: false,
        settings: None,
        record_orchestrated_outside_repos: false,
        yes: false,
        browser: true,
        new_pair: false,
    };
    let mut seen = std::collections::HashSet::new();
    let mut i = 0;
    while i < args.len() {
        if !seen.insert(args[i].as_str()) {
            return Err("invalid_command".into());
        }
        match args[i].as_str() {
            "--origin" => {
                i += 1;
                o.origin = Some(args.get(i).ok_or("invalid_command")?);
            }
            "--settings" => {
                i += 1;
                o.settings = Some(Path::new(args.get(i).ok_or("invalid_command")?));
            }
            "--trust-origin" => o.trust_origin = true,
            "--record-orchestrated-outside-repos" => o.record_orchestrated_outside_repos = true,
            "--new-pair" => o.new_pair = true,
            "--yes" => o.yes = true,
            "--no-browser" => o.browser = false,
            _ => return Err("invalid_command".into()),
        }
        i += 1;
    }
    Ok(o)
}
fn lifecycle(args: &[String]) -> Result<Option<i32>> {
    let rest = &args[2.min(args.len())..];
    match args.get(1).map(String::as_str) {
        Some("install") => install::install(install_options(rest)?, &Systemctl)?,
        Some("upgrade") if rest.is_empty() || rest == ["--yes"] => {
            install::upgrade(!rest.is_empty(), &Systemctl)?
        }
        Some("uninstall") if rest.is_empty() => install::uninstall(&Systemctl)?,
        Some("doctor") if rest.is_empty() => install::doctor(&Systemctl)?,
        Some("timer-sync") if rest.len() == 1 => install::timer_sync(Path::new(&rest[0]))?,
        Some("orchestrators") if rest.first().is_some_and(|c| c == "approve") => match &rest[1..] {
            [] => install::approve_orchestrators(None)?,
            [dir] if dir.starts_with('/') => install::approve_orchestrators(Some(Path::new(dir)))?,
            _ => return Err("invalid_command".into()),
        },
        _ => return Ok(None),
    }
    Ok(Some(0))
}
fn input(limit: usize) -> Result<Vec<u8>> {
    let (tx, rx) = mpsc::sync_channel(1);
    std::thread::spawn(move || {
        let mut data = Vec::new();
        let result = std::io::stdin()
            .take((limit + 1) as u64)
            .read_to_end(&mut data)
            .map(|_| data);
        let _ = tx.send(result);
    });
    let data = rx.recv_timeout(Duration::from_millis(700))??;
    if data.len() > limit {
        return Err("input_too_large".into());
    }
    Ok(data)
}
fn finish_sync(report: &Report) -> i32 {
    let failed = report.scope_refresh_error;
    let scope = failed.map_or(String::new(), |code| {
        format!(",\"scope_refresh_error\":\"{code}\"")
    });
    println!(
        "{{\"pending\":{},\"delivered\":{}{scope}}}",
        report.pending, report.delivered
    );
    if let Some(hint) = failed.and_then(devdiary_collector::pairing::guidance) {
        eprintln!("{hint}");
    }
    i32::from(report.pending > 0)
}
fn run() -> Result<i32> {
    let args: Vec<_> = std::env::args().collect();
    if let Some(code) = lifecycle(&args)? {
        return Ok(code);
    }
    if args.get(1).is_some_and(|s| s == "setup") && args.len() >= 3 {
        let repository = args.get(3).filter(|a| !a.starts_with("--"));
        let mut options = devdiary_collector::pairing::Options {
            root: Path::new(&args[2]),
            repository: repository.map(Path::new),
            remote: None,
            origin: devdiary_collector::pairing::DEFAULT_ORIGIN,
            trust_origin: false,
            browser: true,
            new_pair: false,
            record_orchestrated_outside_repos: false,
            next_steps: true,
        };
        let mut i = if repository.is_some() { 4 } else { 3 };
        let mut seen = std::collections::HashSet::new();
        while i < args.len() {
            if !seen.insert(args[i].as_str()) {
                return Err("invalid_command".into());
            }
            match args[i].as_str() {
                "--remote" => {
                    i += 1;
                    options.remote = Some(args.get(i).ok_or("invalid_command")?);
                }
                "--origin" => {
                    i += 1;
                    options.origin = args.get(i).ok_or("invalid_command")?;
                }
                "--trust-origin" => options.trust_origin = true,
                "--no-browser" => options.browser = false,
                "--new-pair" => options.new_pair = true,
                "--record-orchestrated-outside-repos" => {
                    options.record_orchestrated_outside_repos = true
                }
                _ => return Err("invalid_command".into()),
            }
            i += 1;
        }
        devdiary_collector::pairing::setup(options)?;
        return Ok(0);
    }
    match args.get(1).map(String::as_str) {
        Some("setup-plan") if args.len() == 5 => {
            devdiary_collector::pairing::plan(
                Path::new(&args[2]),
                Path::new(&args[3]),
                Path::new(&args[4]),
            )?;
            return Ok(0);
        }
        Some("connection-status") if (3..=4).contains(&args.len()) => {
            devdiary_collector::pairing::health(Path::new(&args[2]), args.get(3).map(Path::new))?;
            return Ok(0);
        }
        Some("connection-sync") if args.len() == 4 => {
            let report = devdiary_collector::pairing::sync(Path::new(&args[2]), args[3].parse()?)?;
            return Ok(finish_sync(&report));
        }
        _ => {}
    }
    if args.len() == 2 && args[1] == "--help" {
        println!(
            "install [--origin ORIGIN --trust-origin] [--settings SETTINGS] [--record-orchestrated-outside-repos] [--yes] [--no-browser] [--new-pair]\n  Pairs this machine, pins this binary under ~/.local/share/devdiary/collector/VERSION, asks before appending the 9 hook entries, then enables a systemd user timer that syncs every 2 minutes.\n  --yes never approves orchestrator names.\ndoctor\nupgrade [--yes]\nuninstall\ntimer-sync CONNECTION_DIR (run by the timer: up to 10 batches of 100)\norchestrators approve [CONNECTION_DIR]\n  Shows the environment names the server offers and asks. Needs a terminal."
        );
        println!(
            "setup PRIVATE_CONNECTION_DIR [REPOSITORY [--remote NAME]] [--record-orchestrated-outside-repos] [--origin ORIGIN --trust-origin] [--no-browser] [--new-pair]\n  Without REPOSITORY the connection covers every active repository of the workspace (installation scope).\nsetup-plan CONNECTION_DIR SETTINGS NEW_PLAN\nconnection-status CONNECTION_DIR\nconnection-sync CONNECTION_DIR LIMIT\nPairing requires server PR #571 deployed. Approval is separate from local hook consent and host trust."
        );
        println!(
            "Experimental Linux collector; Claude adapter runtime-unqualified.\ninit STATE --consent < scope.json\ncollect STATE < normalized-local-metadata.json\nsync STATE PRIVATE_KEY_FILE LIMIT\nstatus STATE\nrepair STATE (migrates an older spool; pending rows keep their bytes)\nclaude-plan STATE SETTINGS NEW_PLAN\nclaude-apply PLAN --consent\nclaude-remove PLAN --consent\nclaude-hook PLAN (host-only, silent, failure-neutral)\nSTATE must be an existing empty private absolute directory; never a Python spool."
        );
        return Ok(0);
    }
    match args.get(1).map(String::as_str) {
        Some("claude-plan") if args.len() == 5 => {
            devdiary_collector::claude::plan(
                Path::new(&args[2]),
                Path::new(&args[3]),
                Path::new(&args[4]),
            )?;
            return Ok(0);
        }
        Some("claude-apply" | "claude-remove") if args.len() == 4 && args[3] == "--consent" => {
            if args[1] == "claude-apply" {
                devdiary_collector::claude::apply(Path::new(&args[2]))?;
            } else {
                devdiary_collector::claude::remove(Path::new(&args[2]))?;
            }
            return Ok(0);
        }
        _ => {}
    }
    let state = Path::new(args.get(2).ok_or("state_required")?);
    let counts = match args.get(1).map(String::as_str) {
        Some("init") if args.len() == 4 && args[3] == "--consent" => {
            Store::open(state, Some(&input(MAX_INPUT)?))?.counts()?
        }
        Some("collect") if args.len() == 3 => {
            let raw = input(MAX_INPUT)?;
            let mut store = Store::open(state, None)?;
            store.collect(&raw)?;
            store.counts()?
        }
        Some("sync") if args.len() == 5 => {
            let report = sync_outcome(state, Path::new(&args[3]), args[4].parse()?)?;
            return Ok(finish_sync(&report));
        }
        Some("status") if args.len() == 3 => {
            let store = Store::open(state, None)?;
            let (pending, delivered) = store.counts()?;
            let drops = serde_json::to_string(&drops::counts(&store, state)?)?;
            let waiting = if orchestrators::waiting(state) {
                ",\"orchestrators_waiting\":true"
            } else {
                ""
            };
            let unreadable = if orchestrators::offer_unreadable(state) {
                ",\"orchestrators_offered_unreadable\":true"
            } else {
                ""
            };
            println!(
                "{{\"pending\":{pending},\"delivered\":{delivered},\"drops\":{drops}{waiting}{unreadable}}}"
            );
            return Ok(0);
        }
        Some("repair") if args.len() == 3 => {
            repair(state)?;
            Store::open(state, None)?.counts()?
        }
        _ => return Err("invalid_command".into()),
    };
    println!("{{\"pending\":{},\"delivered\":{}}}", counts.0, counts.1);
    Ok(0)
}
fn main() {
    // Dispatch before fallible UTF-8 argument parsing or control diagnostics.
    if std::env::args_os()
        .nth(1)
        .is_some_and(|a| a == "claude-hook")
    {
        std::panic::set_hook(Box::new(|_| {}));
        // Whole-process deadline includes blocked stdin, filesystem and SQLite.
        // No child is spawned and no worker can survive this process exit.
        if std::thread::Builder::new()
            .spawn(|| {
                std::thread::sleep(Duration::from_millis(700));
                std::process::exit(0);
            })
            .is_err()
        {
            std::process::exit(0);
        }
        let _ = std::panic::catch_unwind(|| -> Result<()> {
            let args: Vec<_> = std::env::args_os().collect();
            if args.len() != 3 {
                return Ok(());
            }
            devdiary_collector::claude::hook(Path::new(&args[2]), input(MAX_HOOK_INPUT))
        });
        std::process::exit(0);
    }
    // Never print input, SQL, key paths/contents, URLs or server error bodies.
    let code = match run() {
        Ok(code) => code,
        Err(error) => {
            let command = std::env::args().nth(1).unwrap_or_default();
            let code = error.to_string();
            let lifecycle = [
                "install",
                "upgrade",
                "uninstall",
                "doctor",
                "timer-sync",
                "orchestrators",
            ];
            if lifecycle.contains(&command.as_str())
                && let Some(code) = INSTALL_REPORTED
                    .iter()
                    .chain(REPORTED.iter())
                    .find(|c| **c == code)
            {
                eprintln!("collector_error: {code}");
                let hint = install_hint(code);
                if !hint.is_empty() {
                    eprintln!("{hint}");
                }
            } else if lifecycle.contains(&command.as_str()) {
                let known = devdiary_collector::pairing::known_diagnostic(error.as_ref());
                eprintln!("collector_error");
                eprintln!("{}", known.unwrap_or("Run doctor to inspect the install."));
            } else if matches!(
                command.as_str(),
                "setup" | "setup-plan" | "connection-status" | "connection-sync"
            ) {
                eprintln!(
                    "{}",
                    devdiary_collector::pairing::diagnostic(error.as_ref())
                );
            } else if let Some(code) = REPORTED.iter().find(|c| **c == code) {
                eprintln!("collector_error: {code}");
            } else if let Some(hint) = devdiary_collector::pairing::known_diagnostic(error.as_ref())
            {
                eprintln!("collector_error");
                eprintln!("{hint}");
            } else {
                eprintln!("collector_error");
            }
            2
        }
    };
    std::process::exit(code);
}
