//! What the pairing request says about this machine, so the person approving
//! can recognise it. No security decision rests on these details, and none of
//! them names the local user or the hardware.
use crate::scheduler::Os;
use std::process::{Command, Stdio};

pub(crate) struct Machine {
    pub hostname: String,
    pub os_name: &'static str,
    pub os_version: String,
    pub arch: &'static str,
    pub cli_version: &'static str,
}

pub(crate) fn this() -> Machine {
    let uname = rustix::system::uname();
    let os_version = match Os::THIS {
        Os::Macos => Command::new("/usr/bin/sw_vers")
            .arg("-productVersion")
            .stdin(Stdio::null())
            .stderr(Stdio::null())
            .output()
            .ok()
            .filter(|out| out.status.success())
            .map(|out| String::from_utf8_lossy(&out.stdout).trim().to_owned())
            .unwrap_or_default(),
        Os::Linux => uname.release().to_string_lossy().into_owned(),
    };
    Machine {
        hostname: field(&uname.nodename().to_string_lossy(), 253),
        os_name: match Os::THIS {
            Os::Macos => "macOS",
            Os::Linux => "Linux",
        },
        os_version: field(&os_version, 64),
        arch: std::env::consts::ARCH,
        cli_version: env!("CARGO_PKG_VERSION"),
    }
}

// The server refuses control and format characters, so they are dropped
// rather than lose the whole request over one detail.
fn field(raw: &str, limit: usize) -> String {
    let mut out = String::new();
    for c in raw.chars().filter(|c| printable(*c)) {
        if out.len() + c.len_utf8() > limit {
            break;
        }
        out.push(c);
    }
    if out.is_empty() {
        "unknown".into()
    } else {
        out
    }
}

/// Text the server sends back is shown with any character that could move
/// the cursor, recolour the terminal or reorder the line replaced.
pub(crate) fn shown(raw: &str) -> String {
    raw.chars()
        .take(200)
        .map(|c| if printable(c) { c } else { '\u{fffd}' })
        .collect()
}

// Unicode general categories Cc and Cf.
fn printable(c: char) -> bool {
    !c.is_control()
        && !matches!(c,
            '\u{ad}'
            | '\u{600}'..='\u{605}'
            | '\u{61c}'
            | '\u{6dd}'
            | '\u{70f}'
            | '\u{890}'..='\u{891}'
            | '\u{8e2}'
            | '\u{180e}'
            | '\u{200b}'..='\u{200f}'
            | '\u{202a}'..='\u{202e}'
            | '\u{2060}'..='\u{2064}'
            | '\u{2066}'..='\u{206f}'
            | '\u{feff}'
            | '\u{fff9}'..='\u{fffb}'
            | '\u{110bd}'
            | '\u{110cd}'
            | '\u{13430}'..='\u{1343f}'
            | '\u{1bca0}'..='\u{1bca3}'
            | '\u{1d173}'..='\u{1d17a}'
            | '\u{e0001}'
            | '\u{e0020}'..='\u{e007f}')
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_field_drops_what_the_server_refuses_and_fits_its_limit() {
        assert_eq!(field("build\u{202e}box\n", 253), "buildbox");
        assert_eq!(field("ééé", 5), "éé");
        assert_eq!(field("\u{7}\u{200b}", 64), "unknown");
    }

    #[test]
    fn shown_text_cannot_steer_the_terminal() {
        assert_eq!(shown("acme\u{1b}[2J\u{202e}x"), "acme\u{fffd}[2J\u{fffd}x");
        assert_eq!(shown(&"w".repeat(300)).len(), 200);
    }
}
