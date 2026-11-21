//! How the CLI looks to a person. Styled mode, for a terminal, adds colour,
//! marks, the boxed header and a live wait line. Plain mode, for a pipe, a file,
//! CI or `NO_COLOR`, prints the same words without them. The hook commands print
//! nothing and never come here.

use std::{
    io::{IsTerminal, Write},
    os::fd::AsFd,
    path::Path,
    sync::OnceLock,
    time::Duration,
};

/// The widest a line gets, in either mode.
const WIDEST: usize = 100;
/// Narrower than this, the boxed header collapses to one line.
const BOX_WIDTH: usize = 50;
/// The DevDiary notebook: two rings on the left, the spine, two lines of text.
pub const MARK: [&str; 4] = [" ██ █████", "▀██ █▀▀▀█", "▀██ █▀▀▀█", " ██ █████"];
const TAGLINE: &str = "Records your coding-agent sessions";
const SITE: &str = "devdiary.me";

/// How one output stream renders.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Mode {
    Plain,
    Styled { truecolor: bool, width: usize },
}
impl Mode {
    /// Styled when the stream is a terminal, `NO_COLOR` is unset or empty and
    /// `TERM` is not `dumb`. `width` is the terminal's, when known.
    pub fn detect(
        terminal: bool,
        env: impl Fn(&str) -> Option<String>,
        width: Option<usize>,
    ) -> Self {
        let set = |name: &str| env(name).is_some_and(|v| !v.is_empty());
        if !terminal || set("NO_COLOR") || env("TERM").as_deref() == Some("dumb") {
            return Mode::Plain;
        }
        let truecolor = matches!(env("COLORTERM").as_deref(), Some("truecolor" | "24bit"));
        let width = width.filter(|w| *w > 0).unwrap_or(80).min(WIDEST);
        Mode::Styled { truecolor, width }
    }
    fn width(self) -> usize {
        match self {
            Mode::Plain => WIDEST,
            Mode::Styled { width, .. } => width,
        }
    }
}
fn detect(stream: impl AsFd + IsTerminal) -> Mode {
    let terminal = stream.is_terminal();
    let width = rustix::termios::tcgetwinsize(&stream)
        .ok()
        .map(|size| usize::from(size.ws_col))
        .filter(|columns| *columns > 0)
        .or_else(|| std::env::var("COLUMNS").ok()?.parse().ok());
    Mode::detect(terminal, |name| std::env::var(name).ok(), width)
}

/// A colour or weight. Colour never carries meaning alone: results also have a mark.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Style {
    Accent,
    Green,
    Yellow,
    Red,
    Dim,
    Bold,
}
/// What a result line reports.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Mark {
    Done,
    Attention,
    Failed,
}
impl Mark {
    fn glyph(self) -> (&'static str, Style) {
        match self {
            Mark::Done => ("✓", Style::Green),
            Mark::Attention => ("!", Style::Yellow),
            Mark::Failed => ("✗", Style::Red),
        }
    }
}

pub fn paint(mode: Mode, style: Style, text: &str) -> String {
    let Mode::Styled { truecolor, .. } = mode else {
        return text.to_owned();
    };
    let code = match style {
        // Emerald #10b981; 36 is the nearest colour in the 256-colour cube.
        Style::Accent if truecolor => "38;2;16;185;129",
        Style::Accent => "38;5;36",
        Style::Green => "32",
        Style::Yellow => "33",
        Style::Red => "31",
        Style::Dim => "2",
        Style::Bold => "1",
    };
    format!("\x1b[{code}m{text}\x1b[0m")
}
/// The text without its styling.
pub fn strip(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    let mut chars = text.chars();
    while let Some(c) = chars.next() {
        if c == '\x1b' {
            for c in chars.by_ref() {
                if c.is_ascii_alphabetic() {
                    break;
                }
            }
        } else {
            out.push(c);
        }
    }
    out
}

/// Words broken into rows of at most `width` columns. A word longer than a row
/// stays whole, so a path or command is never split.
fn wrap(text: &str, width: usize) -> Vec<String> {
    let mut rows = Vec::new();
    let mut row = String::new();
    for word in text.split_whitespace() {
        let wanted = row.chars().count() + usize::from(!row.is_empty()) + word.chars().count();
        if !row.is_empty() && wanted > width {
            rows.push(std::mem::take(&mut row));
        }
        if !row.is_empty() {
            row.push(' ');
        }
        row.push_str(word);
    }
    rows.push(row);
    rows
}
/// One line of output: indent, an optional mark, then the text, wrapped with
/// its continuation rows aligned under the text.
pub fn line(
    mode: Mode,
    indent: &str,
    mark: Option<Mark>,
    style: Option<Style>,
    text: &str,
) -> String {
    let lead = indent.chars().count() + mark.map_or(0, |_| 2);
    let rows = wrap(text, mode.width().saturating_sub(lead).max(20));
    let mut out = String::new();
    for (i, row) in rows.iter().enumerate() {
        if i > 0 {
            out.push('\n');
        }
        out.push_str(indent);
        match (i, mark) {
            (0, Some(mark)) => {
                let (glyph, colour) = mark.glyph();
                out.push_str(&paint(mode, colour, glyph));
                out.push(' ');
            }
            (_, Some(_)) => out.push_str("  "),
            _ => {}
        }
        out.push_str(&match style {
            Some(style) => paint(mode, style, row),
            None => row.clone(),
        });
    }
    out
}
/// A question: bold, ending with its choices and a space for the answer.
pub fn question(mode: Mode, indent: &str, text: &str, choices: &str) -> String {
    format!(
        "{indent}{} ",
        paint(mode, Style::Bold, &format!("{text} [{choices}]"))
    )
}
/// `path` as a person reads it: from `~` when it is under the home folder.
pub fn shown(path: &Path, home: Option<&Path>) -> String {
    match home.and_then(|home| path.strip_prefix(home).ok()) {
        Some(rest) if rest.as_os_str().is_empty() => "~".into(),
        Some(rest) => format!("~/{}", rest.display()),
        None => path.display().to_string(),
    }
}
/// `path` from `~` when it is under `HOME`.
pub fn path(path: &Path) -> String {
    let home = std::env::var_os("HOME").map(std::path::PathBuf::from);
    shown(path, home.as_deref().filter(|h| h.is_absolute()))
}
pub fn waiting(elapsed: Duration, limit: Duration) -> String {
    let clock = |d: Duration| format!("{}:{:02}", d.as_secs() / 60, d.as_secs() % 60);
    format!(
        "Waiting for your first session · {} of {}",
        clock(elapsed),
        clock(limit)
    )
}

/// The brand header: one line in plain mode and on a narrow terminal, else the
/// notebook mark, name, version, tagline and site in a box.
pub fn header(mode: Mode, version: &str) -> String {
    let accent = |s: &str| paint(mode, Style::Accent, s);
    let name = paint(mode, Style::Bold, "DevDiary");
    let one_line = format!("{name} {version} · {SITE}");
    match mode {
        Mode::Plain => one_line,
        Mode::Styled { width, .. } if width < BOX_WIDTH => format!("{} {one_line}", accent("◆")),
        Mode::Styled { .. } => {
            let name_line = format!("DevDiary {version}");
            let texts = [name_line.as_str(), TAGLINE, SITE, ""];
            let text_width = texts.iter().map(|t| t.chars().count()).max().unwrap_or(0);
            let mark_width = MARK[0].chars().count();
            let inner = 1 + mark_width + 2 + text_width + 1;
            let rule = "─".repeat(inner);
            let mut rows = vec![accent(&format!("╭{rule}╮"))];
            for (mark, text) in MARK.iter().zip(texts) {
                let pad = " ".repeat(text_width - text.chars().count());
                let text = match text {
                    t if t == name_line => {
                        format!("{name} {}", paint(mode, Style::Dim, version))
                    }
                    SITE => paint(mode, Style::Dim, SITE),
                    t => t.to_owned(),
                };
                rows.push(format!(
                    "{} {}  {text}{pad} {}",
                    accent("│"),
                    accent(mark),
                    accent("│")
                ));
            }
            rows.push(accent(&format!("╰{rule}╯")));
            rows.join("\n")
        }
    }
}

/// Where a command's lines go, and how they render there.
#[derive(Clone, Copy, Debug)]
pub struct Out {
    mode: Mode,
    stderr: bool,
}
impl Out {
    pub fn stdout() -> Self {
        static MODE: OnceLock<Mode> = OnceLock::new();
        Self {
            mode: *MODE.get_or_init(|| detect(std::io::stdout())),
            stderr: false,
        }
    }
    pub fn stderr() -> Self {
        static MODE: OnceLock<Mode> = OnceLock::new();
        Self {
            mode: *MODE.get_or_init(|| detect(std::io::stderr())),
            stderr: true,
        }
    }
    pub fn mode(self) -> Mode {
        self.mode
    }
    pub fn raw(self, text: &str) {
        if self.stderr {
            eprintln!("{text}");
        } else {
            println!("{text}");
        }
    }
    pub fn header(self) {
        self.raw(&header(self.mode, env!("CARGO_PKG_VERSION")));
    }
    /// A numbered setup step, after a blank line.
    pub fn step(self, number: usize, title: &str) {
        let number = paint(self.mode, Style::Accent, &format!("[{number}/4]"));
        self.raw(&format!(
            "\n{number} {}",
            paint(self.mode, Style::Bold, title)
        ));
    }
    pub fn line(self, indent: &str, mark: Option<Mark>, style: Option<Style>, text: &str) {
        self.raw(&line(self.mode, indent, mark, style, text));
    }
    /// Text under a step heading.
    pub fn say(self, text: &str) {
        self.line("  ", None, None, text);
    }
    pub fn done(self, text: &str) {
        self.line("  ", Some(Mark::Done), None, text);
    }
    pub fn attention(self, text: &str) {
        self.line("  ", Some(Mark::Attention), None, text);
    }
    pub fn failed(self, text: &str) {
        self.line("  ", Some(Mark::Failed), None, text);
    }
    pub fn dim(self, text: &str) {
        self.line("  ", None, Some(Style::Dim), text);
    }
    /// The one command to run next.
    pub fn next(self, command: &str) {
        self.raw(&format!(
            "{} {command}",
            paint(self.mode, Style::Bold, "Next:")
        ));
    }
    /// Prints the question and waits on nothing: the caller reads the answer.
    pub fn ask(self, indent: &str, text: &str, choices: &str) -> std::io::Result<()> {
        let question = question(self.mode, indent, text, choices);
        if self.stderr {
            eprint!("{question}");
            std::io::stderr().flush()
        } else {
            print!("{question}");
            std::io::stdout().flush()
        }
    }
}

/// One line that redraws in place in styled mode and prints once in plain mode.
pub struct Live {
    out: Out,
    shown: bool,
}
impl Live {
    pub fn new(out: Out) -> Self {
        Self { out, shown: false }
    }
    pub fn show(&mut self, text: &str) -> std::io::Result<()> {
        match self.out.mode {
            Mode::Plain if self.shown => {}
            Mode::Plain => self.out.say(text),
            Mode::Styled { .. } => {
                print!("\r\x1b[2K  {}", paint(self.out.mode, Style::Dim, text));
                std::io::stdout().flush()?;
            }
        }
        self.shown = true;
        Ok(())
    }
    /// Clears the live line, so the next line takes its place.
    pub fn end(&mut self) -> std::io::Result<()> {
        if self.shown && matches!(self.out.mode, Mode::Styled { .. }) {
            print!("\r\x1b[2K");
            std::io::stdout().flush()?;
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::Path;

    fn env<'a>(pairs: &'a [(&'a str, &'a str)]) -> impl Fn(&str) -> Option<String> + 'a {
        move |name| {
            pairs
                .iter()
                .find(|(k, _)| *k == name)
                .map(|(_, v)| v.to_string())
        }
    }
    const STYLED: Mode = Mode::Styled {
        truecolor: false,
        width: 80,
    };

    #[test]
    fn a_terminal_is_styled_unless_no_color_is_set_or_term_is_dumb() {
        let term = [("TERM", "xterm-256color")];
        assert_eq!(
            Mode::detect(true, env(&term), Some(120)),
            Mode::Styled {
                truecolor: false,
                width: 100
            }
        );
        assert_eq!(Mode::detect(false, env(&term), Some(120)), Mode::Plain);
        let no_color = [("TERM", "xterm"), ("NO_COLOR", "1")];
        assert_eq!(Mode::detect(true, env(&no_color), Some(80)), Mode::Plain);
        let dumb = [("TERM", "dumb")];
        assert_eq!(Mode::detect(true, env(&dumb), Some(80)), Mode::Plain);
        let truecolor = [("TERM", "xterm"), ("COLORTERM", "24bit")];
        assert_eq!(
            Mode::detect(true, env(&truecolor), Some(60)),
            Mode::Styled {
                truecolor: true,
                width: 60
            }
        );
    }

    #[test]
    fn the_accent_is_emerald_in_truecolor_and_its_nearest_256_colour_otherwise() {
        let truecolor = Mode::Styled {
            truecolor: true,
            width: 80,
        };
        assert_eq!(
            paint(truecolor, Style::Accent, "x"),
            "\x1b[38;2;16;185;129mx\x1b[0m"
        );
        assert_eq!(paint(STYLED, Style::Accent, "x"), "\x1b[38;5;36mx\x1b[0m");
        assert_eq!(paint(Mode::Plain, Style::Accent, "x"), "x");
    }

    #[test]
    fn styled_and_plain_lines_carry_the_same_words_and_marks() {
        for mark in [
            None,
            Some(Mark::Done),
            Some(Mark::Attention),
            Some(Mark::Failed),
        ] {
            for style in [None, Some(Style::Dim), Some(Style::Bold)] {
                let text = "Claude Code records to the workspace acme";
                let styled = line(STYLED, "  ", mark, style, text);
                if mark.is_some() || style.is_some() {
                    assert_ne!(styled, line(Mode::Plain, "  ", mark, style, text));
                }
                assert_eq!(strip(&styled), line(Mode::Plain, "  ", mark, style, text));
            }
        }
        assert_eq!(
            line(Mode::Plain, "  ", Some(Mark::Done), None, "Done"),
            "  ✓ Done"
        );
        assert_eq!(
            line(Mode::Plain, "", Some(Mark::Attention), None, "Look"),
            "! Look"
        );
        assert_eq!(
            line(Mode::Plain, "", Some(Mark::Failed), None, "Broke"),
            "✗ Broke"
        );
    }

    #[test]
    fn long_lines_wrap_at_100_columns_in_plain_mode_and_at_the_terminal_in_styled_mode() {
        let words = "word ".repeat(40);
        let plain = line(Mode::Plain, "  ", Some(Mark::Done), None, words.trim());
        let rows: Vec<_> = plain.lines().collect();
        assert!(rows.len() > 1);
        assert!(rows.iter().all(|r| r.chars().count() <= 100), "{plain}");
        assert!(rows[1].starts_with("    word"), "{plain}");
        let narrow = Mode::Styled {
            truecolor: false,
            width: 40,
        };
        let styled = strip(&line(narrow, "  ", None, None, words.trim()));
        assert!(styled.lines().all(|r| r.chars().count() <= 40), "{styled}");
        assert_eq!(styled.split_whitespace().count(), 40);
    }

    #[test]
    fn a_word_longer_than_the_line_is_never_split() {
        let path = format!("/{}", "a".repeat(120));
        let out = line(Mode::Plain, "  ", None, None, &format!("see {path} now"));
        assert!(out.lines().any(|r| r.trim() == path), "{out}");
    }

    #[test]
    fn paths_under_home_print_from_the_tilde() {
        let home = Some(Path::new("/home/ada"));
        assert_eq!(
            shown(Path::new("/home/ada/.claude/settings.json"), home),
            "~/.claude/settings.json"
        );
        assert_eq!(shown(Path::new("/home/ada"), home), "~");
        assert_eq!(shown(Path::new("/home/adam/x"), home), "/home/adam/x");
        assert_eq!(shown(Path::new("/etc/x"), None), "/etc/x");
    }

    #[test]
    fn the_plain_header_is_one_line() {
        assert_eq!(header(Mode::Plain, "0.4.0"), "DevDiary 0.4.0 · devdiary.me");
    }

    #[test]
    fn the_styled_header_is_a_box_at_most_60_columns_with_a_mark_at_most_4_rows() {
        let boxed = strip(&header(STYLED, "0.4.0"));
        let rows: Vec<_> = boxed.lines().collect();
        assert!(rows.len() <= 6, "{boxed}");
        assert!(rows.iter().all(|r| r.chars().count() <= 60), "{boxed}");
        let width = rows[0].chars().count();
        assert!(rows.iter().all(|r| r.chars().count() == width), "{boxed}");
        assert!(rows[0].starts_with('╭') && rows[rows.len() - 1].starts_with('╰'));
        for words in [
            "DevDiary",
            "0.4.0",
            "Records your coding-agent sessions",
            "devdiary.me",
        ] {
            assert!(boxed.contains(words), "{words}");
        }
        assert_eq!(MARK.len(), 4);
        assert!(
            MARK.iter()
                .all(|r| r.chars().count() == MARK[0].chars().count())
        );
    }

    #[test]
    fn below_50_columns_the_styled_header_collapses_to_the_plain_words() {
        let narrow = Mode::Styled {
            truecolor: false,
            width: 49,
        };
        assert_eq!(
            strip(&header(narrow, "0.4.0")),
            "◆ DevDiary 0.4.0 · devdiary.me"
        );
        let wide = Mode::Styled {
            truecolor: false,
            width: 50,
        };
        assert!(strip(&header(wide, "0.4.0")).starts_with('╭'));
    }

    #[test]
    fn a_question_is_bold_and_ends_with_its_choices() {
        assert_eq!(
            question(Mode::Plain, "  ", "Set up Claude Code?", "y/N/d"),
            "  Set up Claude Code? [y/N/d] "
        );
        let styled = question(STYLED, "  ", "Set up Claude Code?", "y/N/d");
        assert!(styled.contains("\x1b[1m"));
        assert_eq!(strip(&styled), "  Set up Claude Code? [y/N/d] ");
    }

    #[test]
    fn the_wait_line_counts_minutes_and_seconds() {
        use std::time::Duration;
        assert_eq!(
            waiting(Duration::from_secs(151), Duration::from_secs(900)),
            "Waiting for your first session · 2:31 of 15:00"
        );
        assert_eq!(
            waiting(Duration::ZERO, Duration::from_secs(60)),
            "Waiting for your first session · 0:00 of 1:00"
        );
    }
}
