//! What a failed command prints: a mark and what happened, why, the next
//! command, then `error_code: <code>` as the last line, and exit status 2.
//! Codes are stable; the last line is for scripts and never changes shape.
use crate::{
    scheduler::Os,
    ui::{self, Mark, Mode, Style},
    vendor::Vendor,
};

// Static refusal codes only; other errors can carry input or paths.
const REPORTED: [&str; 35] = [
    "invalid_command",
    "no_terminal",
    "no_harness_found",
    "pairing_denied",
    "backup_kept",
    "platform_unsupported",
    "scheduler_unreachable",
    "executable_format_unsupported",
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
    "consent_declined",
    "stale_plan",
    "invalid_settings",
    "settings_missing",
    "not_installed",
    "timer_unavailable",
    "timer_unconfirmed",
    "self_test_failed",
    "unsupported_path",
    "home_required",
    "setup_incomplete",
    "owned_entry_changed",
    "owned_entry_missing",
    "not_paired",
    "revoke_unreachable",
    "revoke_refused",
    "login_incomplete",
];
// Codes from pairing, scope refresh and sync.
const PAIRING: [&str; 14] = [
    "trust_origin_required",
    "invalid_origin",
    "frozen_scope_conflict",
    "start_rate_limited",
    "pairing_unavailable",
    "scope_refresh_network_failure",
    "scope_refresh_read_failure",
    "scope_refresh_http_failure",
    "scope_refresh_invalid_response",
    "scope_refresh_response_too_large",
    "scope_refresh_offer_refused",
    "revoked",
    "sync_rate_limited",
    "approved_scope_mismatch",
];
const UNEXPECTED: &str = "unexpected_error";
const MACOS: bool = matches!(Os::THIS, Os::Macos);
const MOVE_ASIDE: &str = "devdiary setup, after you move the connection folder aside";

/// A refusal about one file: its stable code, the file, and what in the file
/// to fix. It prints as its code alone, so it is safe wherever a code is.
#[derive(Debug)]
pub struct Refusal {
    pub code: &'static str,
    pub file: std::path::PathBuf,
    pub detail: Option<String>,
    /// The command that fixes it, when it is not the code's usual one.
    pub next: Option<String>,
}
impl Refusal {
    pub fn about(code: &'static str, file: &std::path::Path) -> Box<dyn std::error::Error> {
        Box::new(Self {
            code,
            file: file.to_path_buf(),
            detail: None,
            next: None,
        })
    }
    pub fn with(
        code: &'static str,
        file: &std::path::Path,
        detail: impl Into<String>,
    ) -> Box<dyn std::error::Error> {
        Box::new(Self {
            code,
            file: file.to_path_buf(),
            detail: Some(detail.into()),
            next: None,
        })
    }
    pub fn with_next(
        code: &'static str,
        file: &std::path::Path,
        detail: impl Into<String>,
        next: impl Into<String>,
    ) -> Box<dyn std::error::Error> {
        Box::new(Self {
            code,
            file: file.to_path_buf(),
            detail: Some(detail.into()),
            next: Some(next.into()),
        })
    }
}
impl std::fmt::Display for Refusal {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(self.code)
    }
}
impl std::error::Error for Refusal {}

/// A failure whose guidance the command printed already, so only its code is left.
#[derive(Debug)]
pub struct Shown(pub String);
impl std::fmt::Display for Shown {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}
impl std::error::Error for Shown {}

/// Whether `code` is a stable code, safe to print as itself.
pub fn known_code(code: &str) -> bool {
    REPORTED.contains(&code) || PAIRING.contains(&code) || code == UNEXPECTED
}
/// The code to print for `error`: its own when it is a known code, and
/// `unexpected_error` for any other, whose text may hold input or a path.
pub fn code(error: &dyn std::error::Error) -> String {
    let text = error.to_string();
    if known_code(&text) {
        text
    } else {
        UNEXPECTED.into()
    }
}

/// The command that failed, so the next step can name it.
pub struct Context {
    /// As typed after `devdiary`, such as `setup` or `orchestrators approve`.
    pub command: &'static str,
    /// The harness it acted on, when one was named or stopped.
    pub vendor: Option<Vendor>,
}
impl Context {
    fn flag(&self) -> &'static str {
        match self.vendor {
            Some(Vendor::Codex) => " --vendor codex",
            _ => "",
        }
    }
    fn again(&self, after: &str) -> Option<String> {
        Some(format!("devdiary {}{}{after}", self.command, self.flag()))
    }
    fn doctor(&self) -> Option<String> {
        Some(format!("devdiary doctor{}", self.flag()))
    }
    /// The connection folder of this command's harness, or of both.
    fn folder(&self) -> String {
        match self.vendor {
            Some(vendor) => format!(
                "The folder is ~/.local/share/devdiary/cli/{}/connection.",
                vendor.slug()
            ),
            None => "The folder is ~/.local/share/devdiary/cli/claude/connection, or codex/connection for Codex.".into(),
        }
    }
}

/// What a person reads when a command fails.
pub struct Guidance {
    pub what: String,
    pub why: Option<String>,
    pub next: Option<String>,
}
impl Guidance {
    fn new(what: &str, why: &str, next: Option<String>) -> Self {
        Self {
            what: what.into(),
            why: (!why.is_empty()).then(|| why.into()),
            next,
        }
    }
    /// The failure as printed: `✗` and what happened, why (dimmed), the lines
    /// in `between`, then `Next:`. `harness` names the harness that stopped.
    pub fn render(&self, mode: Mode, harness: Option<&str>, between: &[String]) -> String {
        let what = match harness {
            Some(name) => format!("{name}: {}", self.what),
            None => self.what.clone(),
        };
        let mut rows = vec![ui::line(mode, "", Some(Mark::Failed), None, &what)];
        if let Some(why) = &self.why {
            rows.push(ui::line(mode, "  ", None, Some(Style::Dim), why));
        }
        for line in between {
            rows.push(ui::line(mode, "  ", None, Some(Style::Dim), line));
        }
        if let Some(next) = &self.next {
            rows.push(format!("{} {next}", ui::paint(mode, Style::Bold, "Next:")));
        }
        rows.join("\n")
    }
}

/// Prints the failure of a command to stderr and returns its exit status, 2.
/// The last line is always `error_code: <code>`, unstyled.
pub fn fail(error: &(dyn std::error::Error + 'static), context: &Context) -> i32 {
    let code = code(error);
    if !error.is::<Shown>() {
        let out = ui::Out::stderr();
        out.raw(&explain(error, context).render(out.mode(), None, &[]));
    }
    eprintln!("error_code: {code}");
    2
}

/// The guidance for `error`: its code's, naming the file when it is a refusal
/// about one.
pub fn explain(error: &(dyn std::error::Error + 'static), c: &Context) -> Guidance {
    let code = code(error);
    let mut g = guidance(&code, c);
    let about = match (
        error.downcast_ref::<Refusal>(),
        error.downcast_ref::<crate::claude::UnreadableSettings>(),
    ) {
        (Some(r), _) => Some((ui::path(&r.file), r.detail.clone(), r.next.clone())),
        (_, Some(u)) => Some((ui::path(u.settings()), Some(u.position()), None)),
        _ => None,
    };
    if let Some((file, detail, next)) = about {
        name_the_file(&mut g, &code, &file, detail.as_deref(), c);
        g.next = next.or(g.next);
    }
    g
}
fn name_the_file(g: &mut Guidance, code: &str, f: &str, detail: Option<&str>, c: &Context) {
    let detail = detail.map(|d| format!(" {d}")).unwrap_or_default();
    let (why, next) = match code {
        "invalid_settings" => (
            format!("{f} is not valid JSON:{detail}. Fix it by hand; nothing changed."),
            c.again(&format!(", after you fix the JSON in {f}")),
        ),
        "settings_missing" => (
            format!(
                "There is no file at {f}. Start the harness once so it creates the file, or pass --settings with the file it uses."
            ),
            c.again(""),
        ),
        "python_observer_registered" => (
            format!(
                "{f} registers the Python observer hook (a command with observer_hook). Remove that hook entry from it."
            ),
            c.again(&format!(
                ", after you remove the Python observer hook from {f}"
            )),
        ),
        "old_collector_registered" => (
            format!(
                "{f} holds hook entries of the older devdiary-collector (commands with claude-hook or codex-hook). Its own uninstall removes them; setup then pairs this machine again."
            ),
            g.next.clone(),
        ),
        "already_registered" => (
            format!(
                "{f} holds {}. Setup adds no second set, so nothing changed.",
                match detail.trim_start() {
                    "" => "DevDiary hook entries",
                    held => held,
                }
            ),
            c.again(&format!(", after you remove DevDiary's entries from {f}")),
        ),
        "unsafe_settings" => (
            format!(
                "{f}{detail}. A settings file and its folder must be owned by you and not writable by group or others, with no symbolic or hard links."
            ),
            c.again(&format!(", after you fix {f}")),
        ),
        "stale_plan" => (
            format!("{f} changed after you agreed to the hook entries, so it was left as it is."),
            c.again(""),
        ),
        "owned_entry_changed" | "owned_entry_missing" => (
            format!(
                "In {f},{detail}. Restore DevDiary's entries as setup wrote them, or delete them."
            ),
            c.again(&format!(
                ", after you restore or delete DevDiary's entries in {f}"
            )),
        ),
        "codex_hooks_json_required" => (
            format!("{f} is not a Codex hooks.json file. Codex hooks go in hooks.json."),
            g.next.clone(),
        ),
        "unsupported_path" => (format!("{f}{detail}."), c.again("")),
        "backup_kept" => (
            format!(
                "Both {f} and{detail} are kept: the copy is the file as it was before an interrupted setup. Remove the one you do not want."
            ),
            g.next.clone(),
        ),
        _ => return,
    };
    g.why = Some(why);
    g.next = next;
}

pub fn guidance(code: &str, c: &Context) -> Guidance {
    let g = Guidance::new;
    let setup = || c.again("");
    let fix_settings = || c.again(", after you fix the settings file");
    match code {
        "invalid_command" => g(
            "devdiary does not know that command or option.",
            "",
            Some("devdiary --help".into()),
        ),
        "no_terminal" => g(
            "This command needs a terminal.",
            "It asks before it changes anything. Nothing changed and nothing was sent.",
            c.again(", in a terminal"),
        ),
        "no_harness_found" => g(
            "Neither Claude Code nor Codex settings were found at the paths listed above.",
            "Nothing changed and nothing was sent.",
            c.again(", after you start Claude Code or Codex once"),
        ),
        "platform_unsupported" => Guidance {
            what: format!(
                "This build is for {} on {}.",
                std::env::consts::OS,
                std::env::consts::ARCH
            ),
            why: Some(
                "DevDiary runs on macOS on Apple silicon and on Linux on x86_64. Nothing was sent."
                    .into(),
            ),
            next: None,
        },
        "scheduler_unreachable" if MACOS => g(
            "No GUI login session for this user, so launchd cannot run the sync agent.",
            "For example, this is an SSH-only session. Nothing was sent.",
            Some("devdiary setup, in Terminal after you log in at this Mac".into()),
        ),
        "scheduler_unreachable" => g(
            "No systemd user session: `systemctl --user` cannot reach your user manager.",
            "So no timer can run the sync. Nothing was sent.",
            c.again(", in a login session that has one"),
        ),
        "executable_format_unsupported" => g(
            "This devdiary binary is not built for this machine.",
            "Nothing was sent. Install it again.",
            Some("curl -fsSL https://devdiary.me/cli/install.sh | sh".into()),
        ),
        "unsupported_vendor" => g(
            "That harness is not one devdiary knows.",
            "Use --vendor claude or --vendor codex.",
            Some(format!("devdiary {} --help", c.command)),
        ),
        "codex_hooks_json_required" => g(
            "That is not a Codex hooks.json file.",
            "Use a Codex hooks.json file, not config.toml.",
            c.again(", with --settings naming a hooks.json file"),
        ),
        "approval_required" => g(
            "Approving orchestrator names needs a person at a terminal.",
            "No flag replaces this.",
            c.again(", in a terminal"),
        ),
        "offered_list_unreadable" => g(
            "The saved orchestrator offer cannot be read.",
            "The next sync replaces it.",
            c.again(", after the next sync"),
        ),
        "consent_declined" => g(
            "Nothing was registered.",
            "Settings and timers are unchanged.",
            setup(),
        ),
        "stale_plan" => g(
            "Settings changed after you agreed to the hook entries.",
            "They were left as they are.",
            setup(),
        ),
        "backup_kept" => g(
            "The settings file and the copy setup made before an interrupted run are both kept.",
            "Remove one of the two files.",
            c.again(", after you remove one of the two files"),
        ),
        "invalid_settings" => g(
            "The selected harness settings file is not valid JSON.",
            "Nothing changed.",
            fix_settings(),
        ),
        "not_installed" => g("Not set up.", "", Some("devdiary setup".into())),
        "old_collector_registered" => g(
            "An older devdiary-collector install registered these hooks.",
            "Remove them, then run `devdiary setup` again. Setup pairs this machine again.",
            Some("devdiary-collector uninstall".into()),
        ),
        "settings_missing" => g(
            "No settings file at that path.",
            "Start the harness once, or pass --settings with an absolute path.",
            setup(),
        ),
        "timer_unavailable" if MACOS => g(
            "Hooks are registered but launchctl could not load the launch agent.",
            "",
            c.doctor(),
        ),
        "timer_unavailable" => g(
            "Hooks are registered but systemctl --user could not enable the timer.",
            "",
            c.doctor(),
        ),
        "timer_unconfirmed" if MACOS => g(
            "Hooks and the launch agent's plist are removed, but launchctl could not confirm the agent stopped.",
            "Log out and back in, or stop the agent with launchctl bootout.",
            Some(format!(
                "launchctl bootout gui/$(id -u)/{}",
                c.vendor.unwrap_or_default().label()
            )),
        ),
        "timer_unconfirmed" => g(
            "Hooks and timer unit files are removed, but systemctl --user could not confirm the timer stopped.",
            "In a user session, disable the timer.",
            Some(format!(
                "systemctl --user disable --now {}",
                c.vendor.unwrap_or_default().timer()
            )),
        ),
        "unsupported_path" if MACOS => g(
            "That path is not one setup can use.",
            "Use an absolute path without control characters.",
            setup(),
        ),
        "unsupported_path" => g(
            "That path is not one setup can use.",
            "Use an absolute path without the characters \" \\ % or $, which systemd would reinterpret.",
            setup(),
        ),
        "home_required" => g("HOME is not set to an absolute path.", "", setup()),
        "connection_revoked" => Guidance {
            what: "The server revoked the saved connection, so the hooks would record nothing."
                .into(),
            why: Some(format!(
                "Run `devdiary uninstall` if it is set up, then move the connection folder aside. {} It keeps any pending rows.",
                c.folder()
            )),
            next: Some(MOVE_ASIDE.into()),
        },
        "self_test_failed" => g(
            "Hooks and the timer are in place, but the installed hook did not record the local test event.",
            "doctor shows the drop reason. To remove the hooks, run `devdiary uninstall`.",
            c.doctor(),
        ),
        "setup_incomplete" => Guidance {
            what: "Setup incomplete.".into(),
            why: Some(format!(
                "No secret is recoverable from a consumed exchange. Revoke unused connections in the browser, then move the connection folder aside. {}",
                c.folder()
            )),
            next: Some(MOVE_ASIDE.into()),
        },
        "not_paired" => g(
            "This harness has no saved connection, so nothing was sent.",
            "An earlier `devdiary login` stopped before the approval, or the connection folder was moved aside. The hooks stay in place and record nothing until it is paired.",
            Some("devdiary login".into()),
        ),
        "revoke_unreachable" => g(
            "DevDiary could not be reached to revoke this machine's old connection.",
            "Nothing changed on this machine. The hooks still record to the old account.",
            Some("devdiary login, once DevDiary can be reached".into()),
        ),
        "revoke_refused" => g(
            "DevDiary did not revoke this machine's old connection.",
            "Nothing changed on this machine. To retire it by hand, revoke it under Settings › Collectors in the old account, then run login again.",
            Some("devdiary login".into()),
        ),
        "login_incomplete" => g(
            "Login stopped before this machine received its new connection.",
            "DevDiary may hold a connection this machine never received: revoke it under Settings › Collectors in the account you approved. The hooks stay in place, and the next login finishes.",
            Some("devdiary login".into()),
        ),
        "owned_entry_changed" | "owned_entry_missing" => g(
            "The collector's hook entries in the settings were edited, so the settings were left as they are.",
            "Restore or delete those entries.",
            c.again(", after you restore or delete those entries"),
        ),
        "already_registered" => g(
            "These settings already hold DevDiary hook entries.",
            "Setup adds no second set. Remove DevDiary's entries from the settings file.",
            fix_settings(),
        ),
        "trust_origin_required" | "invalid_origin" => g(
            "That origin is not one setup trusts.",
            "Use the default HTTPS origin or an explicit --origin ORIGIN --trust-origin (HTTP only for numeric loopback).",
            setup(),
        ),
        "frozen_scope_conflict" => g(
            "The saved connection was made for a different scope and cannot be changed.",
            "To pair again, move the connection folder aside; it keeps its outbox.",
            Some(MOVE_ASIDE.into()),
        ),
        "start_rate_limited" => g(
            "Pairing starts are rate limited.",
            "The saved Retry-After cooldown must expire before setup tries again.",
            c.again(", once the wait has passed"),
        ),
        "pairing_unavailable" => g(
            "DevDiary did not accept the pairing request.",
            "Nothing changed on this machine.",
            c.again(", later"),
        ),
        "pairing_denied" => g(
            "This setup was denied in DevDiary, or a wrong code was typed five times.",
            "Nothing changed on this machine.",
            setup(),
        ),
        "scope_refresh_network_failure" | "scope_refresh_read_failure" => g(
            "Scope refresh failed.",
            "Check the connection and retry sync. Stored scope remains unchanged.",
            Some("devdiary sync".into()),
        ),
        "scope_refresh_http_failure" => g(
            "The server refused scope refresh.",
            "Check server access and retry sync. Stored scope remains unchanged.",
            Some("devdiary sync".into()),
        ),
        "scope_refresh_invalid_response" | "scope_refresh_response_too_large" => g(
            "The scope response is invalid.",
            "Check server compatibility and retry sync. Stored scope remains unchanged.",
            Some("devdiary sync".into()),
        ),
        "scope_refresh_offer_refused" => g(
            "The server offered an orchestrator entry that is not allowed.",
            "Check server compatibility and retry sync. The refused entry was not saved.",
            Some("devdiary sync".into()),
        ),
        "revoked" => g(
            "The server revoked this connection.",
            "Capture and sync have stopped. Run `devdiary uninstall`, then move the connection folder aside.",
            Some(MOVE_ASIDE.into()),
        ),
        "sync_rate_limited" => g(
            "The server asked the collector to wait.",
            "Sync resumes after the saved Retry-After time.",
            None,
        ),
        "repair_required" => g(
            "This outbox uses an older storage format that this version does not read.",
            "Move the connection folder aside.",
            Some(MOVE_ASIDE.into()),
        ),
        "python_observer_registered" => g(
            "These settings already register the Python observer hook.",
            "Remove it before registering this collector.",
            fix_settings(),
        ),
        "unsafe_settings" => g(
            "The settings file or its folder is not safe to change.",
            "Settings and their directory must be owned by you and not writable by group or others, with no symlinks or hardlinks.",
            fix_settings(),
        ),
        "approved_scope_mismatch" => g(
            "The approved scope does not match this installation.",
            "Revoke the unused browser connection, then move the connection folder aside. Hooks are unchanged.",
            Some(MOVE_ASIDE.into()),
        ),
        _ => g(
            "This command stopped on an unexpected error.",
            "",
            c.doctor(),
        ),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::Path;

    #[test]
    fn only_known_codes_are_printed_as_themselves() {
        for known in ["consent_declined", "start_rate_limited", "pairing_denied"] {
            assert_eq!(code(&*Box::<dyn std::error::Error>::from(known)), known);
        }
        let carries_a_path: Box<dyn std::error::Error> = "/home/u/.claude/settings.json".into();
        assert_eq!(code(&*carries_a_path), "unexpected_error");
        assert_eq!(code(&Shown("stale_plan".into())), "stale_plan");
    }

    const SETUP: Context = Context {
        command: "setup",
        vendor: None,
    };

    #[test]
    fn every_known_code_says_what_happened_in_one_line_and_names_a_next_step() {
        for known in REPORTED.iter().chain(&PAIRING).chain(&[UNEXPECTED]) {
            let g = guidance(known, &SETUP);
            assert!(!g.what.is_empty() && !g.what.contains('\n'), "{known}");
            assert!(g.what.ends_with('.'), "{known}: {}", g.what);
            if *known != "platform_unsupported" && *known != "sync_rate_limited" {
                assert!(g.next.is_some(), "{known}");
            }
            assert!(known_code(known), "{known}");
        }
        assert!(!known_code("/home/u/.claude/settings.json"));
    }

    #[test]
    fn a_failure_prints_what_then_why_then_next_and_leaves_the_code_for_last() {
        let g = guidance("no_terminal", &SETUP);
        let text = g.render(Mode::Plain, None, &[]);
        let rows: Vec<_> = text.lines().collect();
        assert_eq!(rows[0], "✗ This command needs a terminal.");
        assert!(rows[1].starts_with("  ") && rows[1].contains("Nothing changed"));
        assert_eq!(*rows.last().unwrap(), "Next: devdiary setup, in a terminal");
        assert!(!text.contains("error_code"));
    }

    #[test]
    fn a_harness_failure_names_the_harness_and_keeps_what_changed_above_next() {
        let codex = Context {
            command: "setup",
            vendor: Some(Vendor::Codex),
        };
        let text = guidance("self_test_failed", &codex).render(
            Mode::Plain,
            Some("Codex"),
            &["Changed on this machine: nothing.".into()],
        );
        let rows: Vec<_> = text.lines().collect();
        assert!(rows[0].starts_with("✗ Codex: "), "{text}");
        assert_eq!(rows[rows.len() - 2], "  Changed on this machine: nothing.");
        assert_eq!(rows[rows.len() - 1], "Next: devdiary doctor --vendor codex");
    }

    #[test]
    fn next_reruns_the_failed_command_for_its_harness() {
        let upgrade = Context {
            command: "upgrade",
            vendor: Some(Vendor::Codex),
        };
        assert_eq!(
            guidance("invalid_settings", &upgrade).next.as_deref(),
            Some("devdiary upgrade --vendor codex, after you fix the settings file")
        );
        let claude = Context {
            command: "uninstall",
            vendor: Some(Vendor::Claude),
        };
        assert_eq!(
            guidance("unexpected_error", &claude).next.as_deref(),
            Some("devdiary doctor")
        );
    }

    #[test]
    fn a_refusal_about_a_file_names_the_file_and_what_to_fix() {
        let file = Path::new("/nowhere/settings.json");
        let cases = [
            (
                Refusal::with("invalid_settings", file, "the error is at line 3, column 7"),
                "/nowhere/settings.json is not valid JSON: the error is at line 3, column 7.",
                "devdiary setup, after you fix the JSON in /nowhere/settings.json",
            ),
            (
                Refusal::about("python_observer_registered", file),
                "/nowhere/settings.json registers the Python observer hook",
                "devdiary setup, after you remove the Python observer hook from /nowhere/settings.json",
            ),
            (
                Refusal::with(
                    "already_registered",
                    file,
                    "2 DevDiary hook entries that no install on this machine recorded",
                ),
                "/nowhere/settings.json holds 2 DevDiary hook entries that no install on this machine recorded. Setup adds no second set, so nothing changed.",
                "devdiary setup, after you remove DevDiary's entries from /nowhere/settings.json",
            ),
            (
                Refusal::with(
                    "unsafe_settings",
                    file,
                    "can be written by group or others (mode 664)",
                ),
                "/nowhere/settings.json can be written by group or others (mode 664).",
                "devdiary setup, after you fix /nowhere/settings.json",
            ),
            (
                Refusal::about("settings_missing", file),
                "There is no file at /nowhere/settings.json.",
                "devdiary setup",
            ),
            (
                Refusal::with(
                    "owned_entry_changed",
                    file,
                    "the DevDiary entry for Stop was edited",
                ),
                "In /nowhere/settings.json, the DevDiary entry for Stop was edited.",
                "devdiary setup, after you restore or delete DevDiary's entries in /nowhere/settings.json",
            ),
        ];
        for (refusal, why, next) in cases {
            let code = code(refusal.as_ref());
            let g = explain(refusal.as_ref(), &SETUP);
            let shown = g.why.unwrap_or_default();
            assert!(shown.starts_with(why), "{code}: {shown}");
            assert_eq!(g.next.as_deref(), Some(next), "{code}");
        }
    }

    #[test]
    fn a_refusal_can_name_the_command_that_fixes_it() {
        let file = Path::new("/nowhere/hooks.json");
        let refusal = Refusal::with_next(
            "already_registered",
            file,
            "1 DevDiary hook entry from an earlier setup",
            "devdiary uninstall --vendor codex",
        );
        let g = explain(refusal.as_ref(), &SETUP);
        assert_eq!(
            g.why.as_deref(),
            Some(
                "/nowhere/hooks.json holds 1 DevDiary hook entry from an earlier setup. Setup adds no second set, so nothing changed."
            )
        );
        assert_eq!(g.next.as_deref(), Some("devdiary uninstall --vendor codex"));
        assert_eq!(code(refusal.as_ref()), "already_registered");
    }

    #[test]
    fn a_refusal_prints_its_code_and_never_its_text() {
        let refusal = Refusal::about("stale_plan", Path::new("/secret/settings.json"));
        assert_eq!(code(refusal.as_ref()), "stale_plan");
        assert_eq!(refusal.to_string(), "stale_plan");
    }

    #[test]
    fn a_connection_refusal_names_the_harness_folder() {
        let codex = Context {
            command: "setup",
            vendor: Some(Vendor::Codex),
        };
        let why = guidance("connection_revoked", &codex).why.unwrap();
        assert!(
            why.contains("~/.local/share/devdiary/cli/codex/connection"),
            "{why}"
        );
        assert!(!why.contains("claude"), "{why}");
    }

    #[test]
    fn styled_failures_carry_the_same_words() {
        let styled = Mode::Styled {
            truecolor: false,
            width: 100,
        };
        for known in REPORTED.iter().chain(&PAIRING) {
            let g = guidance(known, &SETUP);
            assert_eq!(
                crate::ui::strip(&g.render(styled, None, &[])),
                g.render(Mode::Plain, None, &[]),
                "{known}"
            );
        }
    }
}
