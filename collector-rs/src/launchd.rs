//! On macOS a launchd agent in the GUI login domain delivers the spool.
//! Apple states that `launchctl print` output is not an API, so only exit
//! statuses are read.
use crate::{Result, scheduler::Runner, vendor::Vendor};
use std::{
    fs::{self, DirBuilder},
    os::unix::fs::DirBuilderExt,
    path::{Path, PathBuf},
    process::{Command, Stdio},
};

pub struct Launchctl;
impl Runner for Launchctl {
    fn run(&self, args: &[&str]) -> Result<(bool, String)> {
        let status = Command::new(program().ok_or("timer_unavailable")?)
            .args(args)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status()
            .map_err(|_| "timer_unavailable")?;
        Ok((status.success(), String::new()))
    }
}
// Release builds run launchctl from its system path, never from PATH; only
// debug builds take a fake.
fn program() -> Option<PathBuf> {
    let fake = std::env::var_os("DEVDIARY_COLLECTOR_LAUNCHCTL")
        .map(PathBuf::from)
        .filter(|p| cfg!(debug_assertions) && p.is_absolute());
    fake.or_else(|| Some(PathBuf::from("/bin/launchctl")).filter(|p| p.is_file()))
}

pub fn agent_dir() -> Result<PathBuf> {
    let home = std::env::var_os("HOME")
        .map(PathBuf::from)
        .filter(|h| h.is_absolute())
        .ok_or("home_required")?;
    Ok(home.join("Library/LaunchAgents"))
}
pub fn uid() -> u32 {
    rustix::process::getuid().as_raw()
}
fn name(vendor: Vendor) -> String {
    format!("{}.plist", vendor.label())
}
fn target(uid: u32, vendor: Vendor) -> String {
    format!("gui/{uid}/{}", vendor.label())
}

fn string(path: &Path) -> Result<String> {
    let refuse = |why: &str| crate::exit::Refusal::with("unsupported_path", path, why);
    let s = path.to_str().ok_or_else(|| refuse("is not valid UTF-8"))?;
    if !path.is_absolute() {
        return Err(refuse("is not an absolute path"));
    }
    if s.chars().any(char::is_control) {
        return Err(refuse("holds a control character"));
    }
    Ok(s.replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;"))
}
// Every path is in ProgramArguments, so the agent needs nothing from launchd's
// environment. No log paths: the sync status file records each run.
pub fn plist(label: &str, exe: &Path, connection: &Path) -> Result<String> {
    Ok(format!(
        r#"<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>Label</key>
	<string>{label}</string>
	<key>ProgramArguments</key>
	<array>
		<string>{}</string>
		<string>sync</string>
		<string>{}</string>
	</array>
	<key>StartInterval</key>
	<integer>120</integer>
	<key>RunAtLoad</key>
	<true/>
	<key>ProcessType</key>
	<string>Background</string>
	<key>Nice</key>
	<integer>10</integer>
	<key>LowPriorityIO</key>
	<true/>
</dict>
</plist>
"#,
        string(exe)?,
        string(connection)?
    ))
}

/// Only a GUI login session has a `gui/<uid>` domain; SSH alone has none.
pub fn reachable(runner: &dyn Runner, uid: u32) -> Result<()> {
    match runner.run(&["print", &format!("gui/{uid}")]) {
        Ok((true, _)) => Ok(()),
        _ => Err("scheduler_unreachable".into()),
    }
}
fn check(runner: &dyn Runner, args: &[&str]) -> Result<()> {
    if !runner.run(args)?.0 {
        return Err("timer_unavailable".into());
    }
    Ok(())
}
// A loaded agent keeps its old arguments, so an upgrade unloads it before it
// loads the rewritten plist.
pub fn enable(
    runner: &dyn Runner,
    dir: &Path,
    uid: u32,
    exe: &Path,
    connection: &Path,
    vendor: Vendor,
) -> Result<()> {
    let body = plist(vendor.label(), exe, connection)?;
    DirBuilder::new().recursive(true).mode(0o755).create(dir)?;
    crate::systemd::write(dir, &name(vendor), &body)?;
    let path = dir.join(name(vendor));
    let target = target(uid, vendor);
    if runner.run(&["print", &target])?.0 {
        check(runner, &["bootout", &target])?;
    }
    check(
        runner,
        &[
            "bootstrap",
            &format!("gui/{uid}"),
            path.to_str().ok_or("unsupported_path")?,
        ],
    )
}
/// Returns false when no plist was installed. The plist goes even when the
/// agent cannot be confirmed stopped, so nothing loads it at the next login.
pub fn disable(runner: &dyn Runner, dir: &Path, uid: u32, vendor: Vendor) -> Result<bool> {
    let path = dir.join(name(vendor));
    if !path.exists() {
        return Ok(false);
    }
    let target = target(uid, vendor);
    // "Not loaded" counts as done; only an agent still loaded is a failure.
    let stopped = matches!(runner.run(&["bootout", &target]), Ok((true, _)))
        || matches!(runner.run(&["print", &target]), Ok((false, _)));
    match fs::remove_file(&path) {
        Err(e) if e.kind() != std::io::ErrorKind::NotFound => return Err(e.into()),
        _ => {}
    }
    if !stopped {
        return Err("timer_unconfirmed".into());
    }
    Ok(true)
}
pub fn state(runner: &dyn Runner, uid: u32, vendor: Vendor) -> serde_json::Value {
    let loaded = match runner.run(&["print", &target(uid, vendor)]) {
        Ok((loaded, _)) => serde_json::json!(loaded),
        Err(_) => serde_json::json!("unknown"),
    };
    serde_json::json!({ "loaded": loaded })
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use std::{cell::RefCell, fs, os::unix::fs::MetadataExt};

    const EXE: &str = "/Users/u/.local/share/devdiary/cli/claude/0.4.0/devdiary";
    const CONNECTION: &str = "/Users/u/.local/share/devdiary/cli/claude/connection";

    /// Answers each call from `answer`, which sees the arguments, and logs it.
    struct Fake<F: Fn(&[&str]) -> Result<bool>> {
        answer: F,
        calls: RefCell<Vec<String>>,
    }
    impl<F: Fn(&[&str]) -> Result<bool>> Runner for Fake<F> {
        fn run(&self, args: &[&str]) -> Result<(bool, String)> {
            self.calls.borrow_mut().push(args.join(" "));
            Ok(((self.answer)(args)?, String::new()))
        }
    }
    fn fake<F: Fn(&[&str]) -> Result<bool>>(answer: F) -> Fake<F> {
        Fake {
            answer,
            calls: RefCell::default(),
        }
    }
    fn agent(dir: &Path) -> PathBuf {
        dir.join("me.devdiary.claude-sync.plist")
    }

    #[test]
    fn the_agent_runs_sync_every_two_minutes_at_background_priority() {
        assert_eq!(
            plist(
                "me.devdiary.claude-sync",
                Path::new(EXE),
                Path::new(CONNECTION)
            )
            .unwrap(),
            format!(
                r#"<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>Label</key>
	<string>me.devdiary.claude-sync</string>
	<key>ProgramArguments</key>
	<array>
		<string>{EXE}</string>
		<string>sync</string>
		<string>{CONNECTION}</string>
	</array>
	<key>StartInterval</key>
	<integer>120</integer>
	<key>RunAtLoad</key>
	<true/>
	<key>ProcessType</key>
	<string>Background</string>
	<key>Nice</key>
	<integer>10</integer>
	<key>LowPriorityIO</key>
	<true/>
</dict>
</plist>
"#
            )
        );
    }

    #[test]
    fn markup_in_a_path_is_escaped_and_unusable_paths_are_refused() {
        let body = plist("l", Path::new("/a & <b>/x"), Path::new(CONNECTION)).unwrap();
        assert!(body.contains("<string>/a &amp; &lt;b&gt;/x</string>"));
        for bad in ["relative", "/a\nb", "/a\u{7}b"] {
            assert!(plist("l", Path::new(bad), Path::new(CONNECTION)).is_err());
            assert!(plist("l", Path::new(EXE), Path::new(bad)).is_err());
        }
    }

    #[test]
    fn the_gui_domain_must_exist() {
        let gui = fake(|_| Ok(true));
        reachable(&gui, 501).unwrap();
        assert_eq!(*gui.calls.borrow(), ["print gui/501"]);
        let ssh = fake(|_| Ok(false));
        assert_eq!(
            reachable(&ssh, 501).unwrap_err().to_string(),
            "scheduler_unreachable"
        );
        let missing = fake(|_| Err("timer_unavailable".into()));
        assert_eq!(
            reachable(&missing, 501).unwrap_err().to_string(),
            "scheduler_unreachable"
        );
    }

    #[test]
    fn enable_writes_a_private_user_plist_then_bootstraps_it() {
        let dir = crate::tempdir();
        let agents = dir.path().join("Library/LaunchAgents");
        let launchctl = fake(|args| Ok(args[0] != "print"));
        enable(
            &launchctl,
            &agents,
            501,
            Path::new(EXE),
            Path::new(CONNECTION),
            Vendor::Claude,
        )
        .unwrap();
        let written = fs::metadata(agent(&agents)).unwrap();
        assert_eq!(written.uid(), rustix::process::geteuid().as_raw());
        assert_eq!(written.mode() & 0o022, 0);
        assert_eq!(
            fs::read_to_string(agent(&agents)).unwrap(),
            plist(
                "me.devdiary.claude-sync",
                Path::new(EXE),
                Path::new(CONNECTION)
            )
            .unwrap()
        );
        assert_eq!(
            *launchctl.calls.borrow(),
            [
                "print gui/501/me.devdiary.claude-sync".to_owned(),
                format!("bootstrap gui/501 {}", agent(&agents).display()),
            ]
        );
    }

    #[test]
    fn enable_unloads_a_loaded_agent_so_it_reloads_the_new_arguments() {
        let dir = crate::tempdir();
        let launchctl = fake(|_| Ok(true));
        enable(
            &launchctl,
            dir.path(),
            501,
            Path::new(EXE),
            Path::new(CONNECTION),
            Vendor::Codex,
        )
        .unwrap();
        assert_eq!(
            *launchctl.calls.borrow(),
            [
                "print gui/501/me.devdiary.codex-sync".to_owned(),
                "bootout gui/501/me.devdiary.codex-sync".to_owned(),
                format!(
                    "bootstrap gui/501 {}",
                    dir.path().join("me.devdiary.codex-sync.plist").display()
                ),
            ]
        );
    }

    #[test]
    fn a_failed_bootstrap_is_timer_unavailable() {
        let dir = crate::tempdir();
        let error = enable(
            &fake(|_| Ok(false)),
            dir.path(),
            501,
            Path::new(EXE),
            Path::new(CONNECTION),
            Vendor::Claude,
        )
        .unwrap_err();
        assert_eq!(error.to_string(), "timer_unavailable");
    }

    #[test]
    fn disable_boots_out_then_deletes_the_plist_and_repeats_as_a_no_op() {
        let dir = crate::tempdir();
        fs::write(agent(dir.path()), "").unwrap();
        let launchctl = fake(|_| Ok(true));
        assert!(disable(&launchctl, dir.path(), 501, Vendor::Claude).unwrap());
        assert!(!agent(dir.path()).exists());
        assert!(!disable(&launchctl, dir.path(), 501, Vendor::Claude).unwrap());
        assert_eq!(
            *launchctl.calls.borrow(),
            ["bootout gui/501/me.devdiary.claude-sync"]
        );
    }

    #[test]
    fn an_agent_that_is_not_loaded_counts_as_stopped() {
        let dir = crate::tempdir();
        fs::write(agent(dir.path()), "").unwrap();
        let launchctl = fake(|_| Ok(false));
        assert!(disable(&launchctl, dir.path(), 501, Vendor::Claude).unwrap());
        assert!(!agent(dir.path()).exists());
        assert_eq!(
            *launchctl.calls.borrow(),
            [
                "bootout gui/501/me.devdiary.claude-sync",
                "print gui/501/me.devdiary.claude-sync"
            ]
        );
    }

    #[test]
    fn an_agent_still_loaded_after_bootout_is_unconfirmed_but_its_plist_goes() {
        let dir = crate::tempdir();
        fs::write(agent(dir.path()), "").unwrap();
        let launchctl = fake(|args| Ok(args[0] == "print"));
        let error = disable(&launchctl, dir.path(), 501, Vendor::Claude).unwrap_err();
        assert_eq!(error.to_string(), "timer_unconfirmed");
        assert!(!agent(dir.path()).exists());
    }

    #[test]
    fn loaded_comes_from_the_exit_status_of_print() {
        assert_eq!(
            state(&fake(|_| Ok(true)), 501, Vendor::Claude),
            json!({"loaded": true})
        );
        assert_eq!(
            state(&fake(|_| Ok(false)), 501, Vendor::Claude),
            json!({"loaded": false})
        );
        assert_eq!(
            state(
                &fake(|_| Err("timer_unavailable".into())),
                501,
                Vendor::Claude
            ),
            json!({"loaded": "unknown"})
        );
    }
}
