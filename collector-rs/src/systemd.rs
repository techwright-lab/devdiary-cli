//! A user timer delivers the spool; the hook itself never performs HTTP.
use crate::Result;
use std::{
    fs::{self, File, OpenOptions},
    io::Write,
    os::unix::fs::OpenOptionsExt,
    path::{Path, PathBuf},
    process::{Command, Stdio},
};

pub const SERVICE: &str = "devdiary-collector-sync.service";
pub const TIMER: &str = "devdiary-collector-sync.timer";

pub trait Runner {
    fn run(&self, args: &[&str]) -> Result<(bool, String)>;
}
pub struct Systemctl;
impl Runner for Systemctl {
    fn run(&self, args: &[&str]) -> Result<(bool, String)> {
        let output = Command::new(program().ok_or("timer_unavailable")?)
            .arg("--user")
            .args(args)
            .stdin(Stdio::null())
            .stderr(Stdio::null())
            .output()
            .map_err(|_| "timer_unavailable")?;
        let mut stdout = output.stdout;
        stdout.truncate(4096);
        Ok((
            output.status.success(),
            String::from_utf8_lossy(&stdout).into(),
        ))
    }
}
// Release builds take systemctl only from fixed system paths, never from PATH;
// only test builds take a fake.
const SYSTEMCTL: [&str; 3] = [
    "/usr/bin/systemctl",
    "/bin/systemctl",
    "/run/current-system/sw/bin/systemctl",
];
fn program() -> Option<PathBuf> {
    let fake = std::env::var_os("DEVDIARY_COLLECTOR_SYSTEMCTL")
        .map(PathBuf::from)
        .filter(|p| cfg!(debug_assertions) && p.is_absolute());
    fake.or_else(|| SYSTEMCTL.iter().map(PathBuf::from).find(|p| p.is_file()))
}

pub fn unit_dir() -> Result<PathBuf> {
    let config = std::env::var_os("XDG_CONFIG_HOME")
        .map(PathBuf::from)
        .filter(|p| p.is_absolute())
        .or_else(|| std::env::var_os("HOME").map(|h| PathBuf::from(h).join(".config")))
        .filter(|p| p.is_absolute())
        .ok_or("home_required")?;
    Ok(config.join("systemd/user"))
}

// systemd expands %, $ and backslash escapes even inside quotes.
fn quoted(path: &Path) -> Result<String> {
    let s = path.to_str().ok_or("unsupported_path")?;
    if !path.is_absolute() || s.chars().any(|c| c.is_control() || "\"\\%$".contains(c)) {
        return Err("unsupported_path".into());
    }
    Ok(format!("\"{s}\""))
}
pub fn service(exe: &Path, connection: &Path) -> Result<String> {
    Ok(format!(
        "[Unit]\nDescription=DevDiary collector sync\n\n[Service]\nType=oneshot\nNice=10\nNoNewPrivileges=true\nExecStart={} timer-sync {}\n",
        quoted(exe)?,
        quoted(connection)?
    ))
}
// OnUnitActiveSec counts from the last run, so OnActiveSec starts the first one.
pub fn timer() -> String {
    format!(
        "[Unit]\nDescription=DevDiary collector sync every 2 minutes\n\n[Timer]\nOnActiveSec=30s\nOnUnitActiveSec=2min\nRandomizedDelaySec=30\nPersistent=true\nUnit={SERVICE}\n\n[Install]\nWantedBy=timers.target\n"
    )
}

fn write(dir: &Path, name: &str, body: &str) -> Result<()> {
    let temporary = dir.join(format!(".{name}.{}.tmp", uuid::Uuid::new_v4()));
    let result = (|| -> Result<()> {
        let mut file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o644)
            .open(&temporary)?;
        file.write_all(body.as_bytes())?;
        file.sync_all()?;
        fs::rename(&temporary, dir.join(name))?;
        File::open(dir)?.sync_all()?;
        Ok(())
    })();
    let _ = fs::remove_file(&temporary);
    result
}
fn check(runner: &dyn Runner, args: &[&str]) -> Result<()> {
    if !runner.run(args)?.0 {
        return Err("timer_unavailable".into());
    }
    Ok(())
}
pub fn enable(runner: &dyn Runner, dir: &Path, exe: &Path, connection: &Path) -> Result<()> {
    let service = service(exe, connection)?;
    fs::create_dir_all(dir)?;
    write(dir, SERVICE, &service)?;
    write(dir, TIMER, &timer())?;
    check(runner, &["daemon-reload"])?;
    check(runner, &["enable", "--now", TIMER])
}
/// Returns false when no timer unit was installed. The unit files go even when
/// systemctl fails, so nothing restarts the timer at the next login.
pub fn disable(runner: &dyn Runner, dir: &Path) -> Result<bool> {
    if !dir.join(TIMER).exists() && !dir.join(SERVICE).exists() {
        return Ok(false);
    }
    let stopped = check(runner, &["disable", "--now", TIMER]);
    for name in [TIMER, SERVICE] {
        match fs::remove_file(dir.join(name)) {
            Err(e) if e.kind() != std::io::ErrorKind::NotFound => return Err(e.into()),
            _ => {}
        }
    }
    let reloaded = check(runner, &["daemon-reload"]);
    if stopped.is_err() || reloaded.is_err() {
        return Err("timer_unconfirmed".into());
    }
    Ok(true)
}
pub fn state(runner: &dyn Runner) -> serde_json::Value {
    let ask = |verb: &str| {
        runner
            .run(&[verb, TIMER])
            .map(|(_, out)| out.trim().to_owned())
            .unwrap_or_else(|_| "unknown".into())
    };
    serde_json::json!({"enabled": ask("is-enabled"), "active": ask("is-active")})
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::Path;

    #[test]
    fn service_runs_the_bounded_sync_loop_from_the_versioned_binary() {
        let unit = service(
            Path::new("/home/u/.local/share/devdiary/collector/0.1.0/devdiary-collector"),
            Path::new("/home/u/.local/share/devdiary/collector/connection"),
        )
        .unwrap();
        assert_eq!(
            unit,
            "[Unit]\nDescription=DevDiary collector sync\n\n[Service]\nType=oneshot\nNice=10\nNoNewPrivileges=true\nExecStart=\"/home/u/.local/share/devdiary/collector/0.1.0/devdiary-collector\" timer-sync \"/home/u/.local/share/devdiary/collector/connection\"\n"
        );
    }

    #[test]
    fn timer_fires_every_two_minutes_and_catches_up() {
        assert_eq!(
            timer(),
            "[Unit]\nDescription=DevDiary collector sync every 2 minutes\n\n[Timer]\nOnActiveSec=30s\nOnUnitActiveSec=2min\nRandomizedDelaySec=30\nPersistent=true\nUnit=devdiary-collector-sync.service\n\n[Install]\nWantedBy=timers.target\n"
        );
    }

    #[test]
    fn paths_systemd_would_reinterpret_are_refused() {
        let exe = Path::new("/opt/collector");
        for bad in ["/a\"b", "/a\\b", "/a%h", "/a$HOME", "/a\nb", "relative"] {
            assert!(service(exe, Path::new(bad)).is_err(), "{bad:?}");
            assert!(service(Path::new(bad), exe).is_err(), "{bad:?}");
        }
        assert!(service(Path::new("/with space/x"), exe).is_ok());
    }

    struct Recorder(std::cell::RefCell<Vec<String>>);
    impl Runner for Recorder {
        fn run(&self, args: &[&str]) -> crate::Result<(bool, String)> {
            self.0.borrow_mut().push(args.join(" "));
            Ok((true, "enabled\n".into()))
        }
    }

    struct NoBus;
    impl Runner for NoBus {
        fn run(&self, _: &[&str]) -> crate::Result<(bool, String)> {
            Err("timer_unavailable".into())
        }
    }

    #[test]
    fn disable_without_a_user_bus_still_removes_the_units() {
        let dir = tempfile::tempdir().unwrap();
        for name in [SERVICE, TIMER] {
            fs::write(dir.path().join(name), "").unwrap();
        }
        let error = disable(&NoBus, dir.path()).unwrap_err();
        assert_eq!(error.to_string(), "timer_unconfirmed");
        assert!(!dir.path().join(SERVICE).exists() && !dir.path().join(TIMER).exists());
        assert!(!disable(&NoBus, dir.path()).unwrap());
    }

    #[test]
    fn enable_writes_units_before_enabling_and_disable_is_idempotent() {
        let dir = tempfile::tempdir().unwrap();
        let units = dir.path().join("systemd/user");
        let runner = Recorder(Default::default());
        enable(
            &runner,
            &units,
            Path::new("/opt/collector"),
            Path::new("/opt/connection"),
        )
        .unwrap();
        assert!(units.join(SERVICE).is_file() && units.join(TIMER).is_file());
        assert_eq!(
            *runner.0.borrow(),
            [
                "daemon-reload",
                "enable --now devdiary-collector-sync.timer"
            ]
        );
        assert!(disable(&runner, &units).unwrap());
        assert!(!units.join(SERVICE).exists() && !units.join(TIMER).exists());
        assert!(!disable(&runner, &units).unwrap());
        assert_eq!(
            runner.0.borrow()[2..],
            [
                "disable --now devdiary-collector-sync.timer",
                "daemon-reload"
            ]
        );
    }
}
