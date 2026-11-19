#![forbid(unsafe_code)]
use devdiary::{MAX_HOOK_INPUT, Result, install, systemd::Systemctl};
use std::{io::Read, path::Path, sync::mpsc, time::Duration};

// Static refusal codes only; other errors can carry input or paths.
const REPORTED: [&str; 27] = [
    "invalid_command",
    "backup_kept",
    "python_observer_registered",
    "already_registered",
    "unsafe_settings",
    "repair_required",
    "old_collector_registered",
    "unsupported_vendor",
    "codex_hooks_json_required",
    "approval_required",
    "offered_list_unreadable",
    "connection_revoked",
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
fn hint(code: &str) -> &'static str {
    match code {
        "invalid_command" => "Run `devdiary --help` for the commands.",
        "unsupported_vendor" => "Use --vendor claude or --vendor codex.",
        "codex_hooks_json_required" => "Use a Codex hooks.json file, not config.toml.",
        "approval_required" => {
            "Approving orchestrator names needs a person at a terminal. No flag replaces this. Run this command in a terminal."
        }
        "offered_list_unreadable" => {
            "The saved orchestrator offer cannot be read. The next sync replaces it; run this command again after that."
        }
        "consent_required" => "Run setup in a terminal to review the hook entries.",
        "consent_declined" => "Nothing was registered. Settings and timers are unchanged.",
        "stale_plan" => {
            "Settings changed after you agreed to the hook entries, and were left as they are. Run setup again."
        }
        "backup_kept" => {
            "The settings file and the copy setup made before an interrupted run are both kept. Remove one of the two files, then run `devdiary setup` again."
        }
        "invalid_settings" => {
            "The selected harness settings file is not valid JSON. Fix it, then run setup again."
        }
        "already_installed" => "Already set up. Use upgrade to re-register, or uninstall.",
        "not_installed" => "Not set up. Use setup.",
        "old_collector_registered" => {
            "An older devdiary-collector install registered these hooks. Run `devdiary-collector uninstall`, then run `devdiary setup` again. Setup pairs this machine again."
        }
        "settings_missing" => {
            "No settings file at that path. Start the harness once, or pass --settings with an absolute path, then run setup again."
        }
        "timer_unavailable" => {
            "Hooks are registered but systemctl --user could not enable the timer. Run doctor."
        }
        "timer_unconfirmed" => {
            "Hooks and timer unit files are removed, but systemctl --user could not confirm the timer stopped. In a user session disable the selected harness timer: devdiary-claude-sync.timer for Claude or devdiary-codex-sync.timer for Codex"
        }
        "unsupported_path" => {
            "Use an absolute path without the characters \" \\ % or $, which systemd would reinterpret."
        }
        "connection_revoked" => {
            "The server revoked the saved connection, so the hooks would record nothing. Run uninstall if it is set up, then move the connection folder aside and run setup again. The folder is ~/.local/share/devdiary/cli/claude/connection, or codex/connection for Codex; it keeps any pending rows."
        }
        "self_test_failed" => {
            "Hooks and the timer are in place, but the installed hook did not record the local test event. Run doctor to see the drop reason, or uninstall."
        }
        "setup_incomplete" => devdiary::pairing::SETUP_INCOMPLETE,
        "owned_entry_changed" | "owned_entry_missing" => {
            "The collector's hook entries in the settings were edited, so the settings were left as they are. Restore or delete those entries, then run uninstall again."
        }
        _ => "",
    }
}
fn setup_options(
    args: &[String],
    vendor: devdiary::vendor::Vendor,
) -> Result<install::Options<'_>> {
    let mut o = install::Options {
        vendor,
        origin: None,
        trust_origin: false,
        settings: None,
        record_orchestrated_outside_repos: false,
        browser: true,
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
            "--no-browser" => o.browser = false,
            _ => return Err("invalid_command".into()),
        }
        i += 1;
    }
    Ok(o)
}
fn run() -> Result<()> {
    let args: Vec<_> = std::env::args().collect();
    if args.len() == 2 && args[1] == "--help" {
        println!("{HELP}");
        return Ok(());
    }
    let mut rest = args[2.min(args.len())..].to_vec();
    let mut vendor = devdiary::vendor::Vendor::Claude;
    if let Some(index) = rest.iter().position(|a| a == "--vendor") {
        vendor = devdiary::vendor::Vendor::parse(rest.get(index + 1).ok_or("invalid_command")?)?;
        rest.drain(index..index + 2);
        if rest.iter().any(|a| a == "--vendor") {
            return Err("invalid_command".into());
        }
    }
    let rest = rest.as_slice();
    match args.get(1).map(String::as_str) {
        Some("setup") => install::setup(setup_options(rest, vendor)?, &Systemctl),
        Some("upgrade") if rest.is_empty() || rest == ["--yes"] => {
            install::upgrade(!rest.is_empty(), &Systemctl, vendor)
        }
        Some("uninstall") if rest.is_empty() => install::uninstall(&Systemctl, vendor),
        Some("doctor") if rest.is_empty() => install::doctor(&Systemctl, vendor),
        Some("sync") => match args.get(2..).unwrap_or_default() {
            [] => install::sync_all(),
            [dir] if dir.starts_with('/') => install::timer_sync(Path::new(dir)),
            _ => Err("invalid_command".into()),
        },
        Some("outside-repos") => match rest {
            [choice] if choice == "on" => install::outside_repos(true, vendor),
            [choice] if choice == "off" => install::outside_repos(false, vendor),
            _ => Err("invalid_command".into()),
        },
        Some("orchestrators") if rest.first().is_some_and(|c| c == "approve") => match &rest[1..] {
            [] => install::approve_orchestrators(None, vendor),
            [dir] if dir.starts_with('/') => {
                install::approve_orchestrators(Some(Path::new(dir)), vendor)
            }
            _ => Err("invalid_command".into()),
        },
        _ => Err("invalid_command".into()),
    }
}
const HELP: &str = "devdiary records your coding-agent sessions and sends them to DevDiary.
Harness selector: --vendor claude|codex on setup, upgrade, doctor, uninstall, outside-repos and orchestrators approve. Claude is the default. Codex hooks must be reviewed in /hooks; runtime qualification is unverified.
setup [--settings SETTINGS] [--record-orchestrated-outside-repos]
  Pairs this machine, pins this binary under ~/.local/share/devdiary/cli/HARNESS/VERSION, asks in the terminal before appending the selected harness hook entries, then enables a systemd user timer that syncs every 2 minutes.
doctor
sync (sends every installed harness now)
upgrade [--yes]
  --yes never approves orchestrator names.
uninstall
outside-repos on|off
  Also records your sessions outside git repositories: event, time and tool name, never a path. Temp folders stay out.
orchestrators approve [CONNECTION_DIR]
  Shows the environment names the server offers and asks. Needs a terminal.";
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
fn main() {
    // Dispatch before fallible UTF-8 argument parsing or control diagnostics.
    let mut early = std::env::args_os().skip(1);
    if early.next().is_some_and(|a| a == "hook")
        && early.next().is_some_and(|a| a == "claude" || a == "codex")
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
            if args.len() != 4 {
                return Ok(());
            }
            devdiary::claude::hook_for(
                Path::new(&args[3]),
                input(MAX_HOOK_INPUT),
                if args[2] == "codex" {
                    devdiary::vendor::Vendor::Codex
                } else {
                    devdiary::vendor::Vendor::Claude
                },
            )
        });
        std::process::exit(0);
    }
    // Never print input, SQL, key paths/contents, URLs or server error bodies.
    let code = match run() {
        Ok(()) => 0,
        Err(error) => {
            let code = error.to_string();
            if let Some(code) = REPORTED.iter().find(|c| **c == code) {
                eprintln!("collector_error: {code}");
                let hint = hint(code);
                if !hint.is_empty() {
                    eprintln!("{hint}");
                }
            } else {
                let known = devdiary::pairing::known_diagnostic(error.as_ref());
                eprintln!("collector_error");
                eprintln!("{}", known.unwrap_or("Run doctor to inspect the install."));
            }
            2
        }
    };
    std::process::exit(code);
}
