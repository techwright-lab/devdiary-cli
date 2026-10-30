//! Maps a hook cwd to the GitHub repository of its checkout with plain reads:
//! no git process, no include or insteadOf handling, no symlinked git files.
use crate::pairing::github_remote;
use std::{
    fs::{self, File, OpenOptions},
    io::{BufRead, BufReader, Read},
    os::unix::fs::OpenOptionsExt,
    path::{Path, PathBuf},
};

const MAX_DEPTH: usize = 64;
const MAX_POINTER: u64 = 4096;
const MAX_CONFIG: u64 = 16 << 20;

#[derive(Debug, PartialEq, Eq)]
pub enum Checkout {
    Repository(String),
    Unresolved,
    Absent,
}

pub fn resolve(cwd: &Path) -> Checkout {
    let Ok(cwd) = fs::canonicalize(cwd) else {
        return Checkout::Absent;
    };
    for dir in cwd.ancestors().take(MAX_DEPTH) {
        match fs::symlink_metadata(dir.join(".git")) {
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => continue,
            Err(_) => return Checkout::Unresolved,
            Ok(_) => {
                return repository(dir)
                    .map(Checkout::Repository)
                    .unwrap_or(Checkout::Unresolved);
            }
        }
    }
    Checkout::Absent
}

fn repository(dir: &Path) -> Option<String> {
    let common = common_dir(&git_dir(dir)?)?;
    remote(regular(&common.join("config"))?)
}

fn git_dir(dir: &Path) -> Option<PathBuf> {
    let dotgit = dir.join(".git");
    let kind = fs::symlink_metadata(&dotgit).ok()?.file_type();
    if kind.is_dir() {
        return Some(dotgit);
    }
    let pointer = String::from_utf8(read(&dotgit, MAX_POINTER)?).ok()?;
    let target = pointer
        .strip_prefix("gitdir: ")?
        .trim_end_matches(['\n', '\r']);
    real_dir(&dir.join(target))
}

fn common_dir(gitdir: &Path) -> Option<PathBuf> {
    let file = gitdir.join("commondir");
    match fs::symlink_metadata(&file) {
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Some(gitdir.to_path_buf()),
        Err(_) => None,
        Ok(_) => {
            let raw = String::from_utf8(read(&file, MAX_POINTER)?).ok()?;
            real_dir(&gitdir.join(raw.trim_end_matches(['\n', '\r'])))
        }
    }
}

fn real_dir(path: &Path) -> Option<PathBuf> {
    let m = fs::symlink_metadata(path).ok()?;
    m.is_dir().then(|| path.to_path_buf())
}

fn regular(path: &Path) -> Option<File> {
    let file: File = OpenOptions::new()
        .read(true)
        .custom_flags((rustix::fs::OFlags::NOFOLLOW | rustix::fs::OFlags::NONBLOCK).bits() as i32)
        .open(path)
        .ok()?;
    file.metadata().ok()?.is_file().then_some(file)
}

fn read(path: &Path, limit: u64) -> Option<Vec<u8>> {
    let mut raw = Vec::new();
    regular(path)?.take(limit + 1).read_to_end(&mut raw).ok()?;
    (raw.len() as u64 <= limit).then_some(raw)
}

// Origin wins; otherwise exactly one remote may name a GitHub repository.
fn remote(config: File) -> Option<String> {
    let mut reader = BufReader::new(config.take(MAX_CONFIG + 1));
    let (mut section, mut urls, mut line, mut total) = (None, Vec::new(), Vec::new(), 0);
    loop {
        line.clear();
        match reader.read_until(b'\n', &mut line).ok()? {
            0 => break,
            n => total += n as u64,
        }
        if total > MAX_CONFIG {
            return None;
        }
        let text = String::from_utf8_lossy(&line);
        let Some((name, url)) = remote_url(text.trim(), &mut section) else {
            continue;
        };
        if urls.iter().any(|(n, _)| *n == name) {
            continue;
        }
        if name == "origin"
            && let Ok(found) = github_remote(&url)
        {
            return Some(found);
        }
        urls.push((name, url));
    }
    let github: Vec<_> = urls
        .iter()
        .filter_map(|(_, url)| github_remote(url).ok())
        .collect();
    (github.len() == 1).then(|| github[0].clone())
}

fn remote_url(line: &str, section: &mut Option<String>) -> Option<(String, String)> {
    if let Some(header) = line.strip_prefix('[') {
        *section = remote_name(header);
        return None;
    }
    let name = section.as_ref()?;
    let (key, value) = line.split_once('=')?;
    key.trim()
        .eq_ignore_ascii_case("url")
        .then(|| (name.clone(), config_value(value)))
}

fn config_value(raw: &str) -> String {
    let (mut value, mut quoted, mut chars) = (String::new(), false, raw.chars());
    while let Some(c) = chars.next() {
        match c {
            '"' => quoted = !quoted,
            '\\' => value.extend(chars.next()),
            ';' | '#' if !quoted => break,
            _ => value.push(c),
        }
    }
    value.trim().to_owned()
}

fn remote_name(header: &str) -> Option<String> {
    let header = header.split(']').next()?.trim();
    let (kind, rest) = header.split_once(char::is_whitespace)?;
    if !kind.eq_ignore_ascii_case("remote") {
        return None;
    }
    let name = rest.trim().strip_prefix('"')?.strip_suffix('"')?;
    (!name.is_empty()).then(|| name.to_owned())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{fs, os::unix::fs::symlink, path::PathBuf};

    fn config(gitdir: &Path, body: &str) {
        fs::create_dir_all(gitdir).unwrap();
        fs::write(gitdir.join("config"), body).unwrap();
    }
    fn origin(url: &str) -> String {
        format!(
            "[core]\n\tbare = false\n[remote \"origin\"]\n\turl = {url}\n\tfetch = +refs/heads/*:refs/remotes/origin/*\n"
        )
    }
    fn repo(root: &Path, url: &str) -> PathBuf {
        let repo = root.join("main");
        config(&repo.join(".git"), &origin(url));
        repo
    }
    fn found(slug: &str) -> Checkout {
        Checkout::Repository(format!("https://github.com/{slug}"))
    }

    #[test]
    fn ssh_https_and_git_suffix_remotes_canonicalize() {
        for url in [
            "git@github.com:acme/project.git",
            "ssh://git@github.com/acme/project.git",
            "https://github.com/acme/project.git",
            "https://github.com/acme/project",
        ] {
            let dir = tempfile::tempdir().unwrap();
            let repo = repo(dir.path(), url);
            fs::create_dir_all(repo.join("src/deep")).unwrap();
            assert_eq!(
                resolve(&repo.join("src/deep")),
                found("acme/project"),
                "{url}"
            );
        }
    }

    #[test]
    fn linked_worktree_git_file_resolves_through_commondir() {
        let dir = tempfile::tempdir().unwrap();
        let main = repo(dir.path(), "git@github.com:acme/project.git");
        let admin = main.join(".git/worktrees/feature");
        fs::create_dir_all(&admin).unwrap();
        fs::write(admin.join("commondir"), "../..\n").unwrap();
        for tree in [
            main.join(".worktrees/feature"),
            dir.path().join("home/.t3/worktrees/project/feature"),
        ] {
            fs::create_dir_all(&tree).unwrap();
            fs::write(tree.join(".git"), format!("gitdir: {}\n", admin.display())).unwrap();
            assert_eq!(resolve(&tree), found("acme/project"), "{}", tree.display());
        }
    }

    #[test]
    fn relative_gitdir_is_read_from_the_git_file_directory() {
        let dir = tempfile::tempdir().unwrap();
        let main = repo(dir.path(), "https://github.com/acme/project");
        let admin = main.join(".git/worktrees/rel");
        fs::create_dir_all(&admin).unwrap();
        fs::write(admin.join("commondir"), "../..").unwrap();
        let tree = main.join(".worktrees/rel");
        fs::create_dir_all(&tree).unwrap();
        fs::write(tree.join(".git"), "gitdir: ../../.git/worktrees/rel\n").unwrap();
        assert_eq!(resolve(&tree), found("acme/project"));
    }

    #[test]
    fn nested_submodule_resolves_to_its_own_remote() {
        let dir = tempfile::tempdir().unwrap();
        let main = repo(dir.path(), "git@github.com:acme/parent.git");
        config(
            &main.join(".git/modules/vendor/lib"),
            &origin("https://github.com/other/lib.git"),
        );
        let sub = main.join("vendor/lib");
        fs::create_dir_all(sub.join("src")).unwrap();
        fs::write(sub.join(".git"), "gitdir: ../../.git/modules/vendor/lib\n").unwrap();
        assert_eq!(resolve(&sub.join("src")), found("other/lib"));
        assert_eq!(resolve(&main.join("vendor")), found("acme/parent"));
    }

    #[test]
    fn single_github_remote_is_used_without_origin() {
        let dir = tempfile::tempdir().unwrap();
        let repo = dir.path().join("r");
        config(
            &repo.join(".git"),
            "[remote \"upstream\"]\n  url = git@github.com:acme/up.git\n[remote \"mirror\"]\n  url = https://gitlab.com/acme/up.git\n",
        );
        assert_eq!(resolve(&repo), found("acme/up"));
        config(
            &repo.join(".git"),
            "[remote \"a\"]\n url = git@github.com:acme/a.git\n[remote \"b\"]\n url = git@github.com:acme/b.git\n",
        );
        assert_eq!(resolve(&repo), Checkout::Unresolved);
    }

    #[test]
    fn non_github_or_missing_remote_is_unresolved() {
        let dir = tempfile::tempdir().unwrap();
        let repo = dir.path().join("r");
        config(
            &repo.join(".git"),
            &origin("https://gitlab.com/acme/project.git"),
        );
        assert_eq!(resolve(&repo), Checkout::Unresolved);
        config(&repo.join(".git"), "[core]\n\tbare = false\n");
        assert_eq!(resolve(&repo), Checkout::Unresolved);
    }

    #[test]
    fn directory_without_checkout_is_absent() {
        let dir = tempfile::tempdir().unwrap();
        let plain = dir.path().join("plain/child");
        fs::create_dir_all(&plain).unwrap();
        assert_eq!(resolve(&plain), Checkout::Absent);
    }

    #[test]
    fn symlink_escape_is_rejected() {
        let dir = tempfile::tempdir().unwrap();
        let other = repo(dir.path(), "git@github.com:acme/secret.git");
        let linked = dir.path().join("linked");
        fs::create_dir_all(&linked).unwrap();
        symlink(other.join(".git"), linked.join(".git")).unwrap();
        assert_eq!(resolve(&linked), Checkout::Unresolved);

        let config_link = dir.path().join("config-link");
        fs::create_dir_all(config_link.join(".git")).unwrap();
        symlink(other.join(".git/config"), config_link.join(".git/config")).unwrap();
        assert_eq!(resolve(&config_link), Checkout::Unresolved);

        let pointed = dir.path().join("pointed");
        fs::create_dir_all(&pointed).unwrap();
        symlink(other.join(".git"), dir.path().join("gitdir-link")).unwrap();
        fs::write(
            pointed.join(".git"),
            format!("gitdir: {}\n", dir.path().join("gitdir-link").display()),
        )
        .unwrap();
        assert_eq!(resolve(&pointed), Checkout::Unresolved);
    }

    #[test]
    fn large_config_with_many_branches_still_resolves() {
        let dir = tempfile::tempdir().unwrap();
        let repo = dir.path().join("r");
        let branches: String = (0..3000)
            .map(|i| format!("[branch \"feature/{i}\"]\n\tremote = origin\n\tmerge = refs/heads/feature/{i}\n"))
            .collect();
        let body = format!("{branches}{}", origin("git@github.com:acme/big.git"));
        assert!(body.len() > 150_000);
        config(&repo.join(".git"), &body);
        assert_eq!(resolve(&repo), found("acme/big"));
    }

    #[test]
    fn non_utf8_bytes_elsewhere_in_config_do_not_matter() {
        let dir = tempfile::tempdir().unwrap();
        let repo = repo(dir.path(), "git@github.com:acme/demo.git");
        let mut raw = fs::read(repo.join(".git/config")).unwrap();
        raw.extend_from_slice(b"[user]\n\tname = Ren\xe9\n");
        fs::write(repo.join(".git/config"), raw).unwrap();
        assert_eq!(resolve(&repo), found("acme/demo"));
    }

    #[test]
    fn inline_comments_are_stripped_from_the_url() {
        for url in [
            "git@github.com:acme/demo.git ; main",
            "git@github.com:acme/demo.git # main",
            "\"git@github.com:acme/demo.git\" ; main",
        ] {
            let dir = tempfile::tempdir().unwrap();
            let repo = repo(dir.path(), url);
            assert_eq!(resolve(&repo), found("acme/demo"), "{url}");
        }
    }

    #[test]
    fn symlinked_commondir_target_is_rejected() {
        let dir = tempfile::tempdir().unwrap();
        let other = repo(dir.path(), "git@github.com:acme/secret.git");
        let admin = dir.path().join("admin");
        fs::create_dir_all(&admin).unwrap();
        symlink(other.join(".git"), dir.path().join("common-link")).unwrap();
        fs::write(
            admin.join("commondir"),
            format!("{}\n", dir.path().join("common-link").display()),
        )
        .unwrap();
        let tree = dir.path().join("tree");
        fs::create_dir_all(&tree).unwrap();
        fs::write(tree.join(".git"), format!("gitdir: {}\n", admin.display())).unwrap();
        assert_eq!(resolve(&tree), Checkout::Unresolved);
    }

    #[test]
    fn oversized_or_malformed_git_file_is_unresolved() {
        let dir = tempfile::tempdir().unwrap();
        let tree = dir.path().join("t");
        fs::create_dir_all(&tree).unwrap();
        fs::write(tree.join(".git"), "not a pointer\n").unwrap();
        assert_eq!(resolve(&tree), Checkout::Unresolved);
        fs::write(
            tree.join(".git"),
            format!("gitdir: {}\n", "x".repeat(70_000)),
        )
        .unwrap();
        assert_eq!(resolve(&tree), Checkout::Unresolved);
    }
}
