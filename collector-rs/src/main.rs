#![forbid(unsafe_code)]
use devdiary::{MAX_HOOK_INPUT, Result, exit, install, scheduler::Scheduler};
use std::{io::Read, path::Path, sync::mpsc, time::Duration};

fn setup_options(
    args: &[String],
    vendor: Option<devdiary::vendor::Vendor>,
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
/// The exit status: 0, or 1 when doctor printed its report and a check failed.
fn run() -> Result<i32> {
    let args: Vec<_> = std::env::args().collect();
    if args.len() == 2 && args[1] == "--help" {
        println!("{HELP}");
        return Ok(0);
    }
    let mut rest = args[2.min(args.len())..].to_vec();
    let mut named = None;
    if let Some(index) = rest.iter().position(|a| a == "--vendor") {
        named = Some(devdiary::vendor::Vendor::parse(
            rest.get(index + 1).ok_or("invalid_command")?,
        )?);
        rest.drain(index..index + 2);
        if rest.iter().any(|a| a == "--vendor") {
            return Err("invalid_command".into());
        }
    }
    let vendor = named.unwrap_or_default();
    let rest = rest.as_slice();
    let scheduler = Scheduler::system();
    match args.get(1).map(String::as_str) {
        Some("setup") => install::setup(setup_options(rest, named)?, &scheduler),
        Some("upgrade") if rest.is_empty() || rest == ["--yes"] => {
            install::upgrade(!rest.is_empty(), &scheduler, vendor)
        }
        Some("uninstall") if rest.is_empty() => install::uninstall(&scheduler, vendor),
        Some("doctor") if rest.is_empty() => {
            return install::doctor(&scheduler, vendor).map(|outcome| match outcome {
                install::Doctor::Reported => 0,
                install::Doctor::CheckFailed => 1,
            });
        }
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
    .map(|()| 0)
}
const HELP: &str = "devdiary records your coding-agent sessions and sends them to DevDiary.
Harness selector: --vendor claude|codex on setup, upgrade, doctor, uninstall, outside-repos and orchestrators approve. Claude is the default. Codex hooks must be reviewed in /hooks; runtime qualification is unverified.
setup [--settings SETTINGS] [--record-orchestrated-outside-repos]
  Finds Claude Code and Codex on this machine and asks, for each, before it appends its hook entries. One browser approval then pairs every harness you accepted. For each, setup pins this binary under ~/.local/share/devdiary/cli/HARNESS/VERSION, writes the hooks and starts a job that syncs every 2 minutes: a systemd user timer on Linux, a launchd agent on macOS. It then waits up to 15 minutes for your first session. --vendor sets up one harness without asking which.
doctor
  Exits 1 after its report when a settings file is not valid JSON.
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
        Ok(code) => code,
        Err(error) => {
            let code = exit::code(error.as_ref());
            let guidance = exit::guidance(&code);
            if !error.is::<exit::Shown>() && !guidance.is_empty() {
                eprintln!("{guidance}");
            }
            eprintln!("error_code: {code}");
            2
        }
    };
    std::process::exit(code);
}
