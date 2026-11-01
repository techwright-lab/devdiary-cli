use crate::Result;
use std::{fs, path::Path};

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
    let system = std::env::temp_dir();
    let cwd = fs::canonicalize(cwd).unwrap_or_else(|_| cwd.to_path_buf());
    TEMPORARY.iter().any(|dir| cwd.starts_with(dir)) || cwd.starts_with(system)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::PermissionsExt;

    #[test]
    fn consent_is_a_file_that_comes_and_goes() {
        let dir = tempfile::tempdir().unwrap();
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
}
