#![forbid(unsafe_code)]
use devdiary_collector::{
    MAX_HOOK_INPUT, MAX_INPUT, Result, drops,
    store::{Store, repair, sync},
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
fn run() -> Result<i32> {
    let args: Vec<_> = std::env::args().collect();
    if args.get(1).is_some_and(|s| s == "setup") && args.len() >= 4 {
        let mut options = devdiary_collector::pairing::Options {
            root: Path::new(&args[2]),
            repository: Path::new(&args[3]),
            remote: None,
            origin: devdiary_collector::pairing::DEFAULT_ORIGIN,
            trust_origin: false,
            browser: true,
            new_pair: false,
        };
        let mut i = 4;
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
            let (pending, delivered) =
                devdiary_collector::pairing::sync(Path::new(&args[2]), args[3].parse()?)?;
            println!("{{\"pending\":{pending},\"delivered\":{delivered}}}");
            return Ok(if pending > 0 { 1 } else { 0 });
        }
        _ => {}
    }
    if args.len() == 2 && args[1] == "--help" {
        println!(
            "setup PRIVATE_CONNECTION_DIR REPOSITORY [--remote NAME] [--origin ORIGIN --trust-origin] [--no-browser] [--new-pair]\nsetup-plan CONNECTION_DIR SETTINGS NEW_PLAN\nconnection-status CONNECTION_DIR\nconnection-sync CONNECTION_DIR LIMIT\nPairing requires server PR #571 deployed. Approval is separate from local hook consent and host trust."
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
        Some("sync") if args.len() == 5 => sync(state, Path::new(&args[3]), args[4].parse()?)?,
        Some("status") if args.len() == 3 => {
            let store = Store::open(state, None)?;
            let (pending, delivered) = store.counts()?;
            let drops = serde_json::to_string(&drops::counts(&store, state)?)?;
            println!("{{\"pending\":{pending},\"delivered\":{delivered},\"drops\":{drops}}}");
            return Ok(0);
        }
        Some("repair") if args.len() == 3 => {
            repair(state)?;
            Store::open(state, None)?.counts()?
        }
        _ => return Err("invalid_command".into()),
    };
    println!("{{\"pending\":{},\"delivered\":{}}}", counts.0, counts.1);
    Ok(if args[1] == "sync" && counts.0 > 0 {
        1
    } else {
        0
    })
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
            if std::env::args().nth(1).is_some_and(|s| {
                matches!(
                    s.as_str(),
                    "setup" | "setup-plan" | "connection-status" | "connection-sync"
                )
            }) {
                eprintln!(
                    "{}",
                    devdiary_collector::pairing::diagnostic(error.as_ref())
                );
            } else if let Some(code) = REPORTED.iter().find(|c| **c == error.to_string()) {
                eprintln!("collector_error: {code}");
            } else {
                eprintln!("collector_error");
            }
            2
        }
    };
    std::process::exit(code);
}
