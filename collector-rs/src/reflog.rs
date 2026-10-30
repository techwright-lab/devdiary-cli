//! Reads only what a checkout's HEAD reflog gained inside one tool-call
//! bracket. Messages and identities are parsed in memory and never kept.
use sha2::{Digest, Sha256};
use std::{
    fs::{self, File, OpenOptions},
    io::{Read, Seek, SeekFrom},
    os::unix::fs::{MetadataExt, OpenOptionsExt},
    path::Path,
};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Mark {
    pub offset: u64,
    pub ino: u64,
}

#[derive(Debug, PartialEq, Eq)]
pub enum Appended {
    Commits(Vec<String>),
    Gap(&'static str),
}

const MAX_APPENDED: u64 = 64 * 1024;
const MAX_COMMITS: usize = 20;
const COMMIT_ACTIONS: [&str; 3] = ["commit", "cherry-pick", "revert"];
const REBASE_ACTIONS: [&str; 6] = [
    "rebase (pick)",
    "rebase (reword)",
    "rebase (edit)",
    "rebase (squash)",
    "rebase (fixup)",
    "rebase (continue)",
];

pub fn checkout_key(gitdir: &Path) -> Option<String> {
    let m = fs::symlink_metadata(gitdir).ok()?;
    let identity = format!("{}:{}", m.dev(), m.ino());
    Some(format!("{:x}", Sha256::digest(identity.as_bytes())))
}

/// A reflog that does not exist yet marks offset 0 of a future file.
pub fn mark(gitdir: &Path) -> Option<Mark> {
    match open(gitdir) {
        Ok(file) => {
            let m = file.metadata().ok()?;
            Some(Mark {
                offset: m.len(),
                ino: m.ino(),
            })
        }
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Some(Mark { offset: 0, ino: 0 }),
        Err(_) => None,
    }
}

pub fn appended(gitdir: &Path, mark: Mark) -> Appended {
    let file = match open(gitdir) {
        Ok(file) => file,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound && mark.ino == 0 => {
            return Appended::Commits(Vec::new());
        }
        Err(_) => return Appended::Gap("reflog_rewritten"),
    };
    let Ok(m) = file.metadata() else {
        return Appended::Gap("reflog_rewritten");
    };
    if (mark.ino != 0 && m.ino() != mark.ino) || m.len() < mark.offset {
        return Appended::Gap("reflog_rewritten");
    }
    if m.len() - mark.offset > MAX_APPENDED {
        return Appended::Gap("reflog_overflow");
    }
    let mut raw = Vec::new();
    let read = (&file)
        .seek(SeekFrom::Start(mark.offset))
        .and_then(|_| (&file).take(MAX_APPENDED + 1).read_to_end(&mut raw));
    if read.is_err() || raw.len() as u64 > MAX_APPENDED {
        return Appended::Gap("reflog_overflow");
    }
    commits(&raw)
}

// Kept equal to the Python observer's list. A fast-forward merge moves HEAD to
// a commit that already existed, so the bracket did not create it.
pub fn creates_commit(action: &str) -> bool {
    let action = action.to_lowercase();
    if COMMIT_ACTIONS.iter().any(|a| action.starts_with(a)) {
        return true;
    }
    if action.starts_with("merge") {
        return !action.contains("fast-forward");
    }
    REBASE_ACTIONS.iter().any(|a| action.starts_with(a))
}

fn open(gitdir: &Path) -> std::io::Result<File> {
    let file = OpenOptions::new()
        .read(true)
        .custom_flags((rustix::fs::OFlags::NOFOLLOW | rustix::fs::OFlags::NONBLOCK).bits() as i32)
        .open(gitdir.join("logs/HEAD"))?;
    if !file.metadata()?.is_file() {
        return Err(std::io::ErrorKind::InvalidInput.into());
    }
    Ok(file)
}

// A line still being written has no newline yet; it belongs to whoever is
// writing it, so it is skipped rather than guessed at.
fn commits(raw: &[u8]) -> Appended {
    let mut shas = Vec::<String>::new();
    let end = raw.iter().rposition(|b| *b == b'\n').unwrap_or(0);
    let complete = &raw[..end];
    for line in complete.split(|b| *b == b'\n') {
        let Some(sha) = entry(line) else {
            continue;
        };
        if !shas.contains(&sha) {
            shas.push(sha);
        }
        if shas.len() > MAX_COMMITS {
            return Appended::Gap("reflog_overflow");
        }
    }
    Appended::Commits(shas)
}

fn entry(line: &[u8]) -> Option<String> {
    let tab = line.iter().position(|b| *b == b'\t')?;
    let (header, action) = (&line[..tab], &line[tab + 1..]);
    if !creates_commit(&String::from_utf8_lossy(action)) {
        return None;
    }
    let header = std::str::from_utf8(header).ok()?;
    let mut fields = header.splitn(3, ' ');
    let (_, new, _) = (fields.next()?, fields.next()?, fields.next()?);
    let hex = new.len() == 40
        && new
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b));
    hex.then(|| new.to_owned())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{fs, io::Write};

    const A: &str = "1111111111111111111111111111111111111111";
    const B: &str = "2222222222222222222222222222222222222222";
    const C: &str = "3333333333333333333333333333333333333333";

    fn line(old: &str, new: &str, at: i64, message: &str) -> String {
        format!("{old} {new} Ann Example <ann@example.invalid> {at} +0100\t{message}\n")
    }
    fn gitdir() -> (tempfile::TempDir, std::path::PathBuf) {
        let dir = tempfile::tempdir().unwrap();
        let gitdir = dir.path().join(".git");
        fs::create_dir_all(gitdir.join("logs")).unwrap();
        (dir, gitdir)
    }
    fn append(gitdir: &Path, text: &str) {
        fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(gitdir.join("logs/HEAD"))
            .unwrap()
            .write_all(text.as_bytes())
            .unwrap();
    }

    #[test]
    fn commit_creating_actions_match_the_python_list() {
        for action in [
            "commit: add x",
            "commit (initial): first",
            "commit (amend): fix",
            "commit (merge): Merge branch",
            "Cherry-pick: x",
            "revert: Revert \"x\"",
            "merge feature: Merge made by the 'ort' strategy.",
            "rebase (pick): x",
            "rebase (reword): x",
            "rebase (edit): x",
            "rebase (squash): x",
            "rebase (fixup): x",
            "rebase (continue): x",
        ] {
            assert!(creates_commit(action), "{action}");
        }
        for action in [
            "checkout: moving from main to topic",
            "reset: moving to HEAD~1",
            "merge feature: Fast-forward",
            "rebase (start): checkout main",
            "rebase (finish): returning to refs/heads/topic",
            "pull: Fast-forward",
            "branch: Created from HEAD",
        ] {
            assert!(!creates_commit(action), "{action}");
        }
    }

    #[test]
    fn only_entries_appended_after_the_mark_are_read() {
        let (_dir, gitdir) = gitdir();
        append(&gitdir, &line(A, B, 100, "commit: before"));
        let mark = mark(&gitdir).unwrap();
        append(&gitdir, &line(B, C, 200, "commit: inside"));
        append(&gitdir, &line(C, A, 300, "checkout: moving from x to y"));
        assert_eq!(appended(&gitdir, mark), Appended::Commits(vec![C.into()]));
    }

    #[test]
    fn a_reflog_created_inside_the_bracket_is_read_from_the_start() {
        let (_dir, gitdir) = gitdir();
        let mark = mark(&gitdir).unwrap();
        assert_eq!(mark, Mark { offset: 0, ino: 0 });
        append(
            &gitdir,
            &line(&"0".repeat(40), A, 50, "commit (initial): x"),
        );
        assert_eq!(appended(&gitdir, mark), Appended::Commits(vec![A.into()]));
    }

    #[test]
    fn repeated_and_partial_or_malformed_lines_are_skipped() {
        let (_dir, gitdir) = gitdir();
        let mark = mark(&gitdir).unwrap();
        append(&gitdir, &line(A, B, 10, "commit: one"));
        append(&gitdir, &line(C, B, 11, "commit (amend): same sha again"));
        append(
            &gitdir,
            &format!("{A} {} x <y> 12 +0000\tcommit: short\n", "ab"),
        );
        append(&gitdir, &format!("{A} {C} x <y> 13 +0000 commit: no tab\n"));
        append(
            &gitdir,
            &line(A, &"ABCDEF0123".repeat(4), 14, "commit: upper"),
        );
        append(
            &gitdir,
            line(B, C, 15, "commit: torn").trim_end_matches('\n'),
        );
        assert_eq!(appended(&gitdir, mark), Appended::Commits(vec![B.into()]));
    }

    #[test]
    fn shrunk_or_replaced_reflog_is_a_rewrite_gap() {
        let (_dir, gitdir) = gitdir();
        append(&gitdir, &line(A, B, 1, "commit: one"));
        append(&gitdir, &line(B, C, 2, "commit: two"));
        let before = mark(&gitdir).unwrap();
        fs::write(gitdir.join("logs/HEAD.new"), line(A, B, 1, "commit: one")).unwrap();
        fs::rename(gitdir.join("logs/HEAD.new"), gitdir.join("logs/HEAD")).unwrap();
        append(&gitdir, &line(B, C, 3, "commit: three"));
        append(&gitdir, &line(C, A, 4, "commit: four"));
        assert_eq!(appended(&gitdir, before), Appended::Gap("reflog_rewritten"));

        let before = mark(&gitdir).unwrap();
        fs::OpenOptions::new()
            .write(true)
            .open(gitdir.join("logs/HEAD"))
            .unwrap()
            .set_len(10)
            .unwrap();
        assert_eq!(appended(&gitdir, before), Appended::Gap("reflog_rewritten"));

        let before = mark(&gitdir).unwrap();
        fs::remove_file(gitdir.join("logs/HEAD")).unwrap();
        assert_eq!(appended(&gitdir, before), Appended::Gap("reflog_rewritten"));
    }

    #[test]
    fn more_than_64_kib_or_20_commits_is_an_overflow_gap() {
        let (_dir, gitdir) = gitdir();
        let start = mark(&gitdir).unwrap();
        let filler = line(A, B, 1, &format!("checkout: {}", "x".repeat(1000)));
        for _ in 0..66 {
            append(&gitdir, &filler);
        }
        assert_eq!(appended(&gitdir, start), Appended::Gap("reflog_overflow"));

        let start = mark(&gitdir).unwrap();
        for i in 0..21 {
            append(&gitdir, &line(A, &format!("{i:040x}"), i, "commit: n"));
        }
        assert_eq!(appended(&gitdir, start), Appended::Gap("reflog_overflow"));
    }

    #[test]
    fn symlinked_reflog_is_never_followed() {
        let (dir, gitdir) = gitdir();
        let elsewhere = dir.path().join("elsewhere");
        fs::write(&elsewhere, line(A, B, 1, "commit: one")).unwrap();
        std::os::unix::fs::symlink(&elsewhere, gitdir.join("logs/HEAD")).unwrap();
        assert_eq!(mark(&gitdir), None);
    }

    #[test]
    fn checkout_key_is_a_hash_of_the_gitdir_identity_not_its_path() {
        let (dir, gitdir) = gitdir();
        let key = checkout_key(&gitdir).unwrap();
        assert_eq!(key.len(), 64);
        assert!(!key.contains(".git"));
        let other = dir.path().join("other");
        fs::create_dir(&other).unwrap();
        assert_ne!(checkout_key(&other).unwrap(), key);
        assert_eq!(checkout_key(&gitdir).unwrap(), key);
    }
}
