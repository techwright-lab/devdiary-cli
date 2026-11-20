use crate::Result;
use std::{
    fs,
    path::{Path, PathBuf},
};

pub(crate) const CONSENT: &str = "record-outside-repos";
const TEMPORARY: [&str; 2] = ["/tmp", "/var/tmp"];

pub fn allowed(state: &Path) -> bool {
    state.join(CONSENT).is_file()
}

pub fn set(state: &Path, on: bool) -> Result<()> {
    let path = state.join(CONSENT);
    if on {
        crate::store::create_private(&path)?;
    } else if allowed(state) {
        fs::remove_file(path)?;
    }
    Ok(())
}

// Background tools run throwaway sessions here; they are nobody's work.
pub fn temporary(cwd: &Path) -> bool {
    under(
        cwd,
        TEMPORARY
            .iter()
            .map(PathBuf::from)
            .chain([std::env::temp_dir()]),
    )
}
// macOS resolves /tmp, /var/tmp and TMPDIR under /private, so both sides are
// compared in canonical form. A path that cannot be resolved stays as given.
fn under(cwd: &Path, roots: impl IntoIterator<Item = PathBuf>) -> bool {
    let canonical = |p: &Path| fs::canonicalize(p).unwrap_or_else(|_| p.to_path_buf());
    let cwd = canonical(cwd);
    roots
        .into_iter()
        .any(|root| cwd.starts_with(&root) || cwd.starts_with(canonical(&root)))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::PermissionsExt;

    #[test]
    fn consent_is_a_file_that_comes_and_goes() {
        let dir = crate::tempdir();
        assert!(!allowed(dir.path()));
        set(dir.path(), true).unwrap();
        set(dir.path(), true).unwrap();
        assert!(allowed(dir.path()));
        let mode = fs::metadata(dir.path().join(CONSENT))
            .unwrap()
            .permissions();
        assert_eq!(
            mode.mode() & 0o077,
            0,
            "the state check refuses a shared file"
        );
        set(dir.path(), false).unwrap();
        set(dir.path(), false).unwrap();
        assert!(!allowed(dir.path()));
    }

    #[test]
    fn temp_folders_are_never_work() {
        assert!(temporary(Path::new("/tmp/summarizer")));
        assert!(temporary(Path::new("/var/tmp/x")));
        assert!(!temporary(Path::new("/home/user/notes")));
    }

    // On macOS TMPDIR is under /var/folders, which resolves to /private/var.
    #[test]
    fn a_folder_in_the_system_temp_folder_is_temporary_however_it_is_named() {
        let dir = tempfile::tempdir().unwrap();
        assert!(temporary(dir.path()));
        assert!(temporary(&fs::canonicalize(dir.path()).unwrap()));
    }

    #[test]
    fn both_sides_are_compared_in_canonical_form() {
        let dir = crate::tempdir();
        let real = dir.path().join("real");
        fs::create_dir_all(real.join("job")).unwrap();
        let alias = dir.path().join("alias");
        std::os::unix::fs::symlink(&real, &alias).unwrap();
        fs::create_dir(dir.path().join("work")).unwrap();
        assert!(under(&real.join("job"), [alias.clone()]));
        assert!(under(&alias.join("job"), [real.clone()]));
        assert!(!under(&dir.path().join("work"), [alias]));
        assert!(!under(&dir.path().join("realm"), [real]));
    }
}
