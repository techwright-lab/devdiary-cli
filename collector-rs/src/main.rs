#![forbid(unsafe_code)]
use devdiary::{MAX_HOOK_INPUT, Result};
use std::{io::Read, path::Path, sync::mpsc, time::Duration};

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
    std::process::exit(devdiary::cli::run(std::env::args_os().collect()));
}
