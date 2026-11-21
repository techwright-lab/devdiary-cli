//! What a failed command prints: the guidance line for its code, then
//! `error_code: <code>`, and exit status 2. Codes are stable.
use crate::{pairing, scheduler::Os};

// Static refusal codes only; other errors can carry input or paths.
const REPORTED: [&str; 31] = [
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
];
const UNEXPECTED: &str = "unexpected_error";
const MACOS: bool = matches!(Os::THIS, Os::Macos);

/// A failure whose guidance the command printed already, so only its code is left.
#[derive(Debug)]
pub struct Shown(pub String);
impl std::fmt::Display for Shown {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}
impl std::error::Error for Shown {}

/// The code to print for `error`: its own when it is a known code, and
/// `unexpected_error` for any other, whose text may hold input or a path.
pub fn code(error: &dyn std::error::Error) -> String {
    let text = error.to_string();
    if REPORTED.contains(&text.as_str()) || pairing::guidance(&text).is_some() {
        text
    } else {
        UNEXPECTED.into()
    }
}

pub fn guidance(code: &str) -> String {
    match code {
        "platform_unsupported" => format!(
            "This build is for {} on {}. DevDiary runs on macOS on Apple silicon and on Linux on x86_64. Nothing was sent.",
            std::env::consts::OS,
            std::env::consts::ARCH
        ),
        _ => fixed(code).into(),
    }
}
fn fixed(code: &str) -> &'static str {
    match code {
        "invalid_command" => "Run `devdiary --help` for the commands.",
        "no_terminal" => {
            "This command asks before it changes anything, so it needs a terminal. Nothing changed and nothing was sent."
        }
        "no_harness_found" => {
            "Neither Claude Code nor Codex settings were found at the paths listed above. Nothing changed and nothing was sent."
        }
        "scheduler_unreachable" if MACOS => {
            "No GUI login session for this user, for example an SSH-only session, so launchd cannot run the sync agent. Nothing was sent. Log in at this Mac, then run `devdiary setup` in Terminal."
        }
        "scheduler_unreachable" => {
            "No systemd user session: `systemctl --user` cannot reach your user manager, so no timer can run the sync. Nothing was sent. Run `devdiary setup` in a login session that has one."
        }
        "executable_format_unsupported" => {
            "This devdiary binary is not built for this machine. Nothing was sent. Install it again: curl -fsSL https://devdiary.me/cli/install.sh | sh"
        }
        "unsupported_vendor" => "Use --vendor claude or --vendor codex.",
        "codex_hooks_json_required" => "Use a Codex hooks.json file, not config.toml.",
        "approval_required" => {
            "Approving orchestrator names needs a person at a terminal. No flag replaces this. Run this command in a terminal."
        }
        "offered_list_unreadable" => {
            "The saved orchestrator offer cannot be read. The next sync replaces it; run this command again after that."
        }
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
        "not_installed" => "Not set up. Use setup.",
        "old_collector_registered" => {
            "An older devdiary-collector install registered these hooks. Run `devdiary-collector uninstall`, then run `devdiary setup` again. Setup pairs this machine again."
        }
        "settings_missing" => {
            "No settings file at that path. Start the harness once, or pass --settings with an absolute path, then run setup again."
        }
        "timer_unavailable" if MACOS => {
            "Hooks are registered but launchctl could not load the launch agent. Run doctor."
        }
        "timer_unavailable" => {
            "Hooks are registered but systemctl --user could not enable the timer. Run doctor."
        }
        "timer_unconfirmed" if MACOS => {
            "Hooks and the launch agent's plist are removed, but launchctl could not confirm the agent stopped. Log out and back in, or run: launchctl bootout gui/$(id -u)/me.devdiary.claude-sync (me.devdiary.codex-sync for Codex)"
        }
        "timer_unconfirmed" => {
            "Hooks and timer unit files are removed, but systemctl --user could not confirm the timer stopped. In a user session disable the selected harness timer: devdiary-claude-sync.timer for Claude or devdiary-codex-sync.timer for Codex"
        }
        "unsupported_path" if MACOS => "Use an absolute path without control characters.",
        "unsupported_path" => {
            "Use an absolute path without the characters \" \\ % or $, which systemd would reinterpret."
        }
        "connection_revoked" => {
            "The server revoked the saved connection, so the hooks would record nothing. Run uninstall if it is set up, then move the connection folder aside and run setup again. The folder is ~/.local/share/devdiary/cli/claude/connection, or codex/connection for Codex; it keeps any pending rows."
        }
        "self_test_failed" => {
            "Hooks and the timer are in place, but the installed hook did not record the local test event. Run doctor to see the drop reason, or uninstall."
        }
        "setup_incomplete" => pairing::SETUP_INCOMPLETE,
        "owned_entry_changed" | "owned_entry_missing" => {
            "The collector's hook entries in the settings were edited, so the settings were left as they are. Restore or delete those entries, then run uninstall again."
        }
        UNEXPECTED => "Run doctor to inspect the install.",
        _ => pairing::guidance(code).unwrap_or(""),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn only_known_codes_are_printed_as_themselves() {
        for known in ["consent_declined", "start_rate_limited", "pairing_denied"] {
            assert_eq!(code(&*Box::<dyn std::error::Error>::from(known)), known);
        }
        let carries_a_path: Box<dyn std::error::Error> = "/home/u/.claude/settings.json".into();
        assert_eq!(code(&*carries_a_path), "unexpected_error");
        assert_eq!(code(&Shown("stale_plan".into())), "stale_plan");
    }
}
