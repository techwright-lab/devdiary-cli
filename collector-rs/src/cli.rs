//! The commands a person types, parsed with clap, and the help they read.
//! The hook commands never come here: `main` runs them first, before any
//! parsing, to stay inside their deadline.
use crate::{
    Result, exit, install,
    scheduler::Scheduler,
    ui::{self, Out},
    vendor::Vendor,
};
use clap::{
    Arg, ArgAction, Args, CommandFactory, FromArgMatches, Parser, Subcommand,
    error::{ContextKind, ContextValue, ErrorKind},
};
use std::{ffi::OsString, path::PathBuf};

const OVERVIEW: &str = "\
DevDiary {version} · records your coding-agent sessions in DevDiary

Usage:  {usage}

{all-args}

Run \"devdiary <command> --help\" for details.
";
const COMMAND: &str = "{about}\n\n{before-help}Usage:  {usage}\n\n{all-args}{after-help}\n";
const VENDOR: &str = "The harness: claude (the default) or codex";

#[derive(Parser, Debug)]
#[command(
    name = "devdiary",
    version,
    disable_help_flag = true,
    disable_version_flag = true,
    disable_help_subcommand = true,
    override_usage = "devdiary <command> [options]",
    help_template = OVERVIEW
)]
struct Cli {
    #[arg(short = 'h', long = "help", action = ArgAction::Help, help = "Show help")]
    help: Option<bool>,
    #[arg(short = 'V', long = "version", action = ArgAction::Version, help = "Show the version")]
    version: Option<bool>,
    #[command(subcommand)]
    run: Option<Command>,
}

#[derive(Subcommand, Debug)]
enum Command {
    #[command(flatten)]
    Run(Run),
    /// Shows the overview, or one command's help.
    #[command(hide = true)]
    Help { command: Option<String> },
    /// Shows the version, as --version does.
    #[command(hide = true)]
    Version,
}

/// A command to run, as parsed. A harness name is checked when the command runs.
#[derive(Subcommand, Debug, PartialEq, Eq)]
pub enum Run {
    #[command(
        about = "Connect Claude Code and Codex on this machine",
        before_help = concat!(
            "Setup finds Claude Code and Codex, shows what DevDiary would add to each and\n",
            "asks before it changes anything. One approval in the browser then connects every\n",
            "harness you accept. Setup ends by waiting up to 15 minutes for your first\n",
            "session.",
        ),
        override_usage = "devdiary setup [options]",
        after_help = "Examples:\n  devdiary setup\n  devdiary setup --vendor codex"
    )]
    Setup(SetupArgs),
    #[command(
        about = "Check that recording works",
        before_help = concat!(
            "Doctor checks the hooks, the sync job and the last sync of each harness, prints\n",
            "one line for each, and names the command that fixes the first problem. It\n",
            "changes nothing. It exits 1 when a settings file is not valid JSON.",
        ),
        override_usage = "devdiary doctor [options]",
        after_help = "Examples:\n  devdiary doctor\n  devdiary doctor --json --vendor codex"
    )]
    Doctor {
        #[arg(long, value_name = "claude|codex", help = "Check one harness only")]
        vendor: Option<String>,
        #[arg(long, help = "Print one harness's full report as JSON, for scripts")]
        json: bool,
    },
    #[command(
        about = "Send recorded sessions now",
        before_help = concat!(
            "Sync sends what every harness set up on this machine has recorded, without\n",
            "waiting for the sync job that runs every 2 minutes.",
        ),
        override_usage = "devdiary sync",
        after_help = "Examples:\n  devdiary sync"
    )]
    Sync {
        /// The sync job's own entry point: one connection folder.
        #[arg(hide = true, value_parser = absolute)]
        connection: Option<PathBuf>,
    },
    #[command(
        about = "Move hooks to this version",
        before_help = concat!(
            "Upgrade shows how this version's hook entries replace the ones an earlier\n",
            "version wrote, and asks before it replaces them.",
        ),
        override_usage = "devdiary upgrade [options]",
        after_help = "Examples:\n  devdiary upgrade\n  devdiary upgrade --vendor codex --yes"
    )]
    Upgrade {
        #[arg(
            long,
            help = "Replace without asking. It never approves orchestrator names"
        )]
        yes: bool,
        #[arg(long, value_name = "claude|codex", help = VENDOR)]
        vendor: Option<String>,
    },
    #[command(
        about = "Remove hooks and stop syncing",
        before_help = concat!(
            "Uninstall removes the hook entries and the sync job. The connection and anything\n",
            "not yet sent stay on this machine; revoke the connection in DevDiary to retire\n",
            "it.",
        ),
        override_usage = "devdiary uninstall [options]",
        after_help = "Examples:\n  devdiary uninstall\n  devdiary uninstall --vendor codex"
    )]
    Uninstall {
        #[arg(long, value_name = "claude|codex", help = VENDOR)]
        vendor: Option<String>,
    },
    #[command(
        about = "Record sessions outside git repositories (on|off)",
        before_help = concat!(
            "When on, DevDiary also records your sessions outside git repositories: event,\n",
            "time and tool name, never a path. Temp folders stay out. Turning it on needs a\n",
            "terminal.",
        ),
        override_usage = "devdiary outside-repos <on|off> [options]",
        after_help = "Examples:\n  devdiary outside-repos on\n  devdiary outside-repos off --vendor codex"
    )]
    OutsideRepos {
        #[arg(
            value_parser = ["on", "off"],
            value_name = "on|off",
            hide_possible_values = true,
            help = "on to record them, off to stop"
        )]
        choice: String,
        #[arg(long, value_name = "claude|codex", help = VENDOR)]
        vendor: Option<String>,
    },
    #[command(
        about = "Approve orchestrator names",
        before_help = concat!(
            "Shows the environment names DevDiary offers for recognising orchestrated runs,\n",
            "and asks before the hook reads them. It needs a terminal.",
        ),
        override_usage = "devdiary orchestrators approve [options]",
        after_help = "Examples:\n  devdiary orchestrators approve\n  devdiary orchestrators approve --vendor codex",
        subcommand_required = true
    )]
    #[command(subcommand)]
    Orchestrators(Orchestrators),
}

#[derive(Args, Debug, PartialEq, Eq)]
pub struct SetupArgs {
    #[arg(
        long,
        value_name = "claude|codex",
        help = "Set up one harness without asking which"
    )]
    pub vendor: Option<String>,
    #[arg(long, hide = true)]
    pub origin: Option<String>,
    #[arg(long, hide = true)]
    pub trust_origin: bool,
    #[arg(
        long,
        value_name = "file",
        help = "The Claude Code settings file, as an absolute path"
    )]
    pub settings: Option<PathBuf>,
    #[arg(long, help = "Also record orchestrated runs outside git repositories")]
    pub record_orchestrated_outside_repos: bool,
    #[arg(long, help = "Print the approval link without opening a browser")]
    pub no_browser: bool,
}

#[derive(Subcommand, Debug, PartialEq, Eq)]
pub enum Orchestrators {
    #[command(
        about = "Show the offered names and ask",
        before_help = concat!(
            "Shows the environment names DevDiary offers for recognising orchestrated runs,\n",
            "and asks before the hook reads them. It needs a terminal.",
        ),
        override_usage = "devdiary orchestrators approve [options]",
        after_help = "Examples:\n  devdiary orchestrators approve"
    )]
    Approve {
        #[arg(hide = true, value_parser = absolute)]
        connection: Option<PathBuf>,
        #[arg(long, value_name = "claude|codex", help = VENDOR)]
        vendor: Option<String>,
    },
}

fn absolute(value: &str) -> std::result::Result<PathBuf, String> {
    if value.starts_with('/') {
        Ok(value.into())
    } else {
        Err("an absolute path".into())
    }
}

/// The command line as clap reads it, with every command's help laid out alike.
fn command() -> clap::Command {
    let help = || {
        Arg::new("help")
            .short('h')
            .long("help")
            .action(ArgAction::Help)
            .help("Show help")
    };
    let laid_out = move |c: clap::Command| {
        c.disable_help_flag(true)
            .disable_help_subcommand(true)
            .arg(help())
            .help_template(COMMAND)
    };
    Cli::command()
        .mut_subcommands(laid_out)
        .mut_subcommand("orchestrators", move |c| c.mut_subcommands(laid_out))
}
/// `devdiary --help`, `devdiary help` and a bare `devdiary`.
pub fn overview() -> String {
    command().render_help().to_string()
}

/// What the command line asks for.
#[derive(Debug, PartialEq, Eq)]
pub enum Parsed {
    Overview,
    /// Text clap prints: one command's help, or the version.
    Help(String),
    Run(Run),
}
/// A command line that is not one devdiary takes.
pub struct Failure {
    pub code: String,
    pub guidance: exit::Guidance,
}

pub fn parsed<I, T>(args: I) -> std::result::Result<Parsed, Failure>
where
    I: IntoIterator<Item = T>,
    T: Into<OsString> + Clone,
{
    let args: Vec<OsString> = args.into_iter().map(Into::into).collect();
    let matches = match command().try_get_matches_from(&args) {
        Ok(matches) => matches,
        Err(error)
            if matches!(
                error.kind(),
                ErrorKind::DisplayHelp | ErrorKind::DisplayVersion
            ) =>
        {
            let text = error.render().to_string();
            return Ok(if text == overview() {
                Parsed::Overview
            } else {
                Parsed::Help(text)
            });
        }
        Err(error) => return Err(invalid(&error, &args)),
    };
    let cli = Cli::from_arg_matches(&matches).map_err(|e| invalid(&e, &args))?;
    match cli.run {
        None | Some(Command::Help { command: None }) => Ok(Parsed::Overview),
        Some(Command::Help {
            command: Some(name),
        }) => {
            let mut command = command();
            match command.find_subcommand_mut(&name) {
                Some(sub) if !sub.is_hide_set() => {
                    let mut sub = sub.clone().bin_name(format!("devdiary {name}"));
                    Ok(Parsed::Help(sub.render_help().to_string()))
                }
                _ => Err(failure(
                    "devdiary has no such command.".into(),
                    None,
                    "devdiary --help".into(),
                )),
            }
        }
        Some(Command::Version) => Ok(Parsed::Help(command().render_version().to_string())),
        Some(Command::Run(run)) => Ok(Parsed::Run(run)),
    }
}
fn failure(what: String, why: Option<String>, next: String) -> Failure {
    let mut guidance = exit::guidance(
        "invalid_command",
        &exit::Context {
            command: "",
            vendor: None,
        },
    );
    guidance.what = what;
    guidance.why = why;
    guidance.next = Some(next);
    Failure {
        code: "invalid_command".into(),
        guidance,
    }
}
// What was typed is never echoed: only names devdiary itself defines.
fn invalid(error: &clap::Error, args: &[OsString]) -> Failure {
    let named = |kind| match error.get(kind) {
        Some(ContextValue::String(s)) => Some(s.clone()),
        Some(ContextValue::Strings(list)) => list.first().cloned(),
        _ => None,
    };
    let valid = match error.get(ContextKind::ValidValue) {
        Some(ContextValue::Strings(list)) => Some(list.join(" or ")),
        _ => None,
    };
    let what = match error.kind() {
        ErrorKind::InvalidSubcommand => "devdiary has no such command.",
        ErrorKind::UnknownArgument => "That command does not take that argument or option.",
        ErrorKind::InvalidValue | ErrorKind::ValueValidation => {
            "That value is not one this command takes."
        }
        ErrorKind::ArgumentConflict => "An option is given twice, or with one it cannot go with.",
        _ => "The command is not complete.",
    };
    let why = named(ContextKind::SuggestedSubcommand)
        .or_else(|| named(ContextKind::SuggestedArg))
        .map(|name| format!("Did you mean `{}`?", name.trim()))
        .or_else(|| valid.map(|v| format!("It takes {v}.")));
    let visible = args
        .get(1)
        .and_then(|a| a.to_str())
        .filter(|_| error.kind() != ErrorKind::InvalidSubcommand)
        .filter(|name| {
            command()
                .find_subcommand(name)
                .is_some_and(|c| !c.is_hide_set())
        });
    let next = match visible {
        Some(name) => format!("devdiary {name} --help"),
        None => "devdiary --help".into(),
    };
    failure(what.into(), why, next)
}

/// Runs the command line and returns the exit status: 0, 1 when doctor
/// printed its report and a check failed, or 2 after a failure.
pub fn run(args: Vec<OsString>) -> i32 {
    let bare = args.len() <= 1;
    match parsed(args) {
        Ok(Parsed::Overview) => {
            let out = Out::stdout();
            if bare && out.mode() != ui::Mode::Plain {
                out.header();
                out.raw("");
            }
            print!("{}", overview());
            0
        }
        Ok(Parsed::Help(text)) => {
            print!("{text}");
            0
        }
        Ok(Parsed::Run(run)) => dispatch(run),
        Err(failure) => {
            let out = Out::stderr();
            out.raw(&failure.guidance.render(out.mode(), None, &[]));
            eprintln!("error_code: {}", failure.code);
            2
        }
    }
}

fn dispatch(run: Run) -> i32 {
    let (command, vendor) = match &run {
        Run::Setup(a) => ("setup", &a.vendor),
        Run::Doctor { vendor, .. } => ("doctor", vendor),
        Run::Sync { .. } => ("sync", &None),
        Run::Upgrade { vendor, .. } => ("upgrade", vendor),
        Run::Uninstall { vendor } => ("uninstall", vendor),
        Run::OutsideRepos { vendor, .. } => ("outside-repos", vendor),
        Run::Orchestrators(Orchestrators::Approve { vendor, .. }) => {
            ("orchestrators approve", vendor)
        }
    };
    let named = match vendor.as_deref().map(Vendor::parse).transpose() {
        Ok(named) => named,
        Err(error) => {
            return exit::fail(
                error.as_ref(),
                &exit::Context {
                    command,
                    vendor: None,
                },
            );
        }
    };
    let context = exit::Context {
        command,
        vendor: named,
    };
    match execute(run, named) {
        Ok(status) => status,
        Err(error) => exit::fail(error.as_ref(), &context),
    }
}
fn execute(run: Run, named: Option<Vendor>) -> Result<i32> {
    let scheduler = Scheduler::system();
    let vendor = named.unwrap_or_default();
    match run {
        Run::Setup(a) => install::setup(
            install::Options {
                vendor: named,
                origin: a.origin.as_deref(),
                trust_origin: a.trust_origin,
                settings: a.settings.as_deref(),
                record_orchestrated_outside_repos: a.record_orchestrated_outside_repos,
                browser: !a.no_browser,
            },
            &scheduler,
        ),
        Run::Doctor { json, .. } => {
            return install::doctor(&scheduler, named, json).map(|outcome| match outcome {
                install::Doctor::Reported => 0,
                install::Doctor::CheckFailed => 1,
            });
        }
        Run::Sync { connection: None } => install::sync_all(),
        Run::Sync {
            connection: Some(dir),
        } => install::timer_sync(&dir),
        Run::Upgrade { yes, .. } => install::upgrade(yes, &scheduler, vendor),
        Run::Uninstall { .. } => install::uninstall(&scheduler, vendor),
        Run::OutsideRepos { choice, .. } => install::outside_repos(choice == "on", vendor),
        Run::Orchestrators(Orchestrators::Approve { connection, .. }) => {
            install::approve_orchestrators(connection.as_deref(), vendor)
        }
    }
    .map(|()| 0)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn parse(args: &[&str]) -> std::result::Result<Parsed, String> {
        parsed(std::iter::once("devdiary").chain(args.iter().copied())).map_err(|f| f.code)
    }

    #[test]
    fn the_overview_lists_the_commands_and_options_in_the_spec_layout() {
        let overview = overview();
        let version = env!("CARGO_PKG_VERSION");
        assert!(overview.starts_with(&format!(
            "DevDiary {version} · records your coding-agent sessions in DevDiary\n\nUsage:  devdiary <command> [options]\n\nCommands:\n"
        )), "{overview}");
        for (name, about) in [
            ("setup", "Connect Claude Code and Codex on this machine"),
            ("doctor", "Check that recording works"),
            ("sync", "Send recorded sessions now"),
            ("upgrade", "Move hooks to this version"),
            ("uninstall", "Remove hooks and stop syncing"),
            (
                "outside-repos",
                "Record sessions outside git repositories (on|off)",
            ),
            ("orchestrators", "Approve orchestrator names"),
        ] {
            let row = overview
                .lines()
                .find(|l| l.trim_start().starts_with(&format!("{name} ")))
                .unwrap_or_else(|| panic!("{name} missing from\n{overview}"));
            assert!(row.ends_with(about), "{row}");
        }
        assert!(overview.contains("\nOptions:\n"), "{overview}");
        assert!(overview.contains("-h, --help"), "{overview}");
        assert!(overview.contains("Show help"), "{overview}");
        assert!(overview.contains("-V, --version"), "{overview}");
        assert!(overview.contains("Show the version"), "{overview}");
        assert!(
            overview.ends_with("Run \"devdiary <command> --help\" for details.\n"),
            "{overview}"
        );
        for hidden in [
            "\n  hook ",
            "\n  help ",
            "CONNECTION_DIR",
            "qualification",
            "Exits 1",
        ] {
            assert!(!overview.contains(hidden), "{hidden} in\n{overview}");
        }
    }

    #[test]
    fn the_version_answers_to_the_flag_and_the_command() {
        let version = format!("devdiary {}\n", env!("CARGO_PKG_VERSION"));
        for args in [&["--version"][..], &["-V"], &["version"]] {
            assert_eq!(parse(args), Ok(Parsed::Help(version.clone())), "{args:?}");
        }
        assert!(!overview().contains("\n  version "));
    }

    #[test]
    fn help_and_a_bare_devdiary_show_the_overview() {
        for args in [&[][..], &["--help"], &["-h"], &["help"]] {
            assert!(matches!(parse(args), Ok(Parsed::Overview)), "{args:?}");
        }
    }

    #[test]
    fn each_command_has_help_with_a_paragraph_its_options_and_examples() {
        for name in [
            "setup",
            "doctor",
            "sync",
            "upgrade",
            "uninstall",
            "outside-repos",
            "orchestrators",
        ] {
            let Ok(Parsed::Help(help)) = parse(&[name, "--help"]) else {
                panic!("{name}");
            };
            assert!(help.contains(&format!("Usage:  devdiary {name}")), "{help}");
            assert!(help.contains("\nExamples:\n  devdiary "), "{help}");
            assert!(help.contains("-h, --help"), "{help}");
            assert_eq!(parse(&["help", name]).ok(), Some(Parsed::Help(help)));
        }
        let Ok(Parsed::Help(setup)) = parse(&["setup", "--help"]) else {
            panic!()
        };
        for option in [
            "--vendor",
            "--settings",
            "--no-browser",
            "--record-orchestrated-outside-repos",
        ] {
            assert!(setup.contains(option), "{option}");
        }
        for hidden in ["--origin", "--trust-origin", "pins", "VERSION"] {
            assert!(!setup.contains(hidden), "{hidden}");
        }
    }

    #[test]
    fn commands_parse_with_their_harness() {
        assert_eq!(
            parse(&["setup", "--vendor", "codex", "--no-browser"]),
            Ok(Parsed::Run(Run::Setup(SetupArgs {
                vendor: Some("codex".into()),
                origin: None,
                trust_origin: false,
                settings: None,
                record_orchestrated_outside_repos: false,
                no_browser: true,
            })))
        );
        assert_eq!(
            parse(&["upgrade", "--yes", "--vendor", "claude"]),
            Ok(Parsed::Run(Run::Upgrade {
                yes: true,
                vendor: Some("claude".into())
            }))
        );
        assert_eq!(
            parse(&["sync", "/state"]),
            Ok(Parsed::Run(Run::Sync {
                connection: Some("/state".into())
            }))
        );
        assert_eq!(
            parse(&["orchestrators", "approve", "--vendor", "codex"]),
            Ok(Parsed::Run(Run::Orchestrators(Orchestrators::Approve {
                connection: None,
                vendor: Some("codex".into())
            })))
        );
    }

    #[test]
    fn anything_else_is_an_invalid_command() {
        for args in [
            &["init", "/state", "--consent"][..],
            &["setup", "/state"],
            &["setup", "--yes"],
            &["setup", "--new-pair"],
            &["setup", "--no-browser", "--no-browser"],
            &["setup", "--vendor", "claude", "--vendor", "codex"],
            &["setup", "--vendor"],
            &["sync", "relative"],
            &["sync", "--vendor", "claude"],
            &["outside-repos", "maybe"],
            &["orchestrators"],
            &["orchestrators", "approve", "relative"],
            &["hook", "claude"],
            &["help", "nothing"],
        ] {
            assert_eq!(
                parse(args).err().as_deref(),
                Some("invalid_command"),
                "{args:?}"
            );
        }
    }

    #[test]
    fn a_mistyped_command_gets_a_suggestion_without_echoing_what_was_typed() {
        let failure = parsed(["devdiary", "setpu"]).err().unwrap();
        let text = failure.guidance.render(crate::ui::Mode::Plain, None, &[]);
        assert!(text.contains("Did you mean `setup`?"), "{text}");
        assert!(!text.contains("setpu"), "{text}");
        assert!(text.ends_with("Next: devdiary --help"), "{text}");
        let failure = parsed(["devdiary", "setup", "--no-browsr"]).err().unwrap();
        let text = failure.guidance.render(crate::ui::Mode::Plain, None, &[]);
        assert!(text.contains("Did you mean `--no-browser`?"), "{text}");
        assert!(text.ends_with("Next: devdiary setup --help"), "{text}");
    }
}
