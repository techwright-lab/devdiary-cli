#![forbid(unsafe_code)]
use devdiary_collector::{
    MAX_INPUT, Result,
    store::{Store, sync},
};
use std::{io::Read, path::Path, sync::mpsc, time::Duration};

fn input() -> Result<Vec<u8>> {
    let (tx, rx) = mpsc::sync_channel(1);
    std::thread::spawn(move || {
        let mut data = Vec::new();
        let result = std::io::stdin()
            .take((MAX_INPUT + 1) as u64)
            .read_to_end(&mut data)
            .map(|_| data);
        let _ = tx.send(result);
    });
    let data = rx.recv_timeout(Duration::from_millis(700))??;
    if data.len() > MAX_INPUT {
        return Err("input_too_large".into());
    }
    Ok(data)
}
fn run() -> Result<i32> {
    let args: Vec<_> = std::env::args().collect();
    if args.len() == 2 && args[1] == "--help" {
        println!(
            "Experimental Linux collector; no vendor registration.\ninit STATE --consent < scope.json\ncollect STATE < normalized-local-metadata.json\nsync STATE PRIVATE_KEY_FILE LIMIT\nstatus STATE\nSTATE must be an existing empty private absolute directory; never a Python spool."
        );
        return Ok(0);
    }
    let state = Path::new(args.get(2).ok_or("state_required")?);
    let counts = match args.get(1).map(String::as_str) {
        Some("init") if args.len() == 4 && args[3] == "--consent" => {
            Store::open(state, Some(&input()?))?.counts()?
        }
        Some("collect") if args.len() == 3 => {
            let raw = input()?;
            let mut store = Store::open(state, None)?;
            store.collect(&raw)?;
            store.counts()?
        }
        Some("sync") if args.len() == 5 => sync(state, Path::new(&args[3]), args[4].parse()?)?,
        Some("status") if args.len() == 3 => Store::open(state, None)?.counts()?,
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
    // Never print input, SQL, key paths/contents, URLs or server error bodies.
    let code = match run() {
        Ok(code) => code,
        Err(_) => {
            eprintln!("collector_error");
            2
        }
    };
    std::process::exit(code);
}
