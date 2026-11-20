//! Release one runs on macOS on Apple silicon and on Linux on x86_64. Setup
//! checks the machine before it pairs, so a machine that fails approves nothing.
use crate::{Result, scheduler::Scheduler};
use std::{fs::File, io::Read, path::Path};

pub(crate) fn supported(os: &str, arch: &str) -> bool {
    matches!((os, arch), ("macos", "aarch64") | ("linux", "x86_64"))
}
// macOS: a 64-bit Mach-O, thin (MH_MAGIC_64, stored little-endian on arm64)
// or universal (FAT_MAGIC, stored big-endian).
pub(crate) fn native_format(os: &str, magic: [u8; 4]) -> bool {
    match os {
        "linux" => magic == *b"\x7fELF",
        "macos" => magic == [0xcf, 0xfa, 0xed, 0xfe] || magic == [0xca, 0xfe, 0xba, 0xbe],
        _ => false,
    }
}
pub fn preflight(scheduler: &Scheduler<'_>) -> Result<()> {
    check(
        std::env::consts::OS,
        std::env::consts::ARCH,
        &std::env::current_exe()?,
        scheduler,
    )
}
fn check(os: &str, arch: &str, exe: &Path, scheduler: &Scheduler<'_>) -> Result<()> {
    if !supported(os, arch) {
        return Err("platform_unsupported".into());
    }
    scheduler.reachable()?;
    let mut magic = [0; 4];
    let read = File::open(exe).and_then(|mut f| f.read_exact(&mut magic));
    if read.is_err() || !native_format(os, magic) {
        return Err("executable_format_unsupported".into());
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::scheduler::{Os, Runner};
    use std::cell::RefCell;

    const ELF: [u8; 4] = *b"\x7fELF";
    const MACH_O_64: [u8; 4] = [0xcf, 0xfa, 0xed, 0xfe];
    const UNIVERSAL: [u8; 4] = [0xca, 0xfe, 0xba, 0xbe];
    const MACH_O_32: [u8; 4] = [0xce, 0xfa, 0xed, 0xfe];

    struct Answer(bool, RefCell<Vec<String>>);
    impl Runner for Answer {
        fn run(&self, args: &[&str]) -> Result<(bool, String)> {
            self.1.borrow_mut().push(args.join(" "));
            Ok((self.0, String::new()))
        }
    }
    fn exe(dir: &Path, magic: &[u8]) -> std::path::PathBuf {
        let path = dir.join("devdiary");
        std::fs::write(&path, magic).unwrap();
        path
    }
    fn code(result: Result<()>) -> String {
        result.unwrap_err().to_string()
    }

    #[test]
    fn release_one_targets_are_apple_silicon_macs_and_x86_64_linux() {
        assert!(supported("macos", "aarch64"));
        assert!(supported("linux", "x86_64"));
        for (os, arch) in [
            ("macos", "x86_64"),
            ("linux", "aarch64"),
            ("freebsd", "x86_64"),
            ("windows", "x86_64"),
        ] {
            assert!(!supported(os, arch), "{os} {arch}");
        }
    }

    #[test]
    fn each_build_accepts_only_its_own_executable_format() {
        assert!(native_format("linux", ELF));
        assert!(!native_format("linux", MACH_O_64));
        assert!(!native_format("linux", UNIVERSAL));
        assert!(native_format("macos", MACH_O_64));
        assert!(native_format("macos", UNIVERSAL));
        assert!(!native_format("macos", ELF));
        assert!(!native_format("macos", MACH_O_32));
        assert!(!native_format("macos", *b"#!/b"));
    }

    #[test]
    fn a_machine_that_passes_checks_platform_scheduler_and_format() {
        let dir = crate::tempdir();
        let runner = Answer(true, RefCell::default());
        let scheduler = Scheduler {
            os: Os::Linux,
            runner: &runner,
        };
        check("linux", "x86_64", &exe(dir.path(), &ELF), &scheduler).unwrap();
        assert_eq!(*runner.1.borrow(), ["show-environment"]);
    }

    #[test]
    fn an_unsupported_platform_is_refused_before_the_scheduler_is_asked() {
        let dir = crate::tempdir();
        let runner = Answer(true, RefCell::default());
        let scheduler = Scheduler {
            os: Os::Linux,
            runner: &runner,
        };
        let refused = check("linux", "aarch64", &exe(dir.path(), &ELF), &scheduler);
        assert_eq!(code(refused), "platform_unsupported");
        assert!(runner.1.borrow().is_empty());
    }

    #[test]
    fn an_unreachable_scheduler_is_refused() {
        let dir = crate::tempdir();
        let runner = Answer(false, RefCell::default());
        let scheduler = Scheduler {
            os: Os::Macos,
            runner: &runner,
        };
        let refused = check("macos", "aarch64", &exe(dir.path(), &MACH_O_64), &scheduler);
        assert_eq!(code(refused), "scheduler_unreachable");
    }

    #[test]
    fn a_binary_in_another_format_is_refused() {
        let dir = crate::tempdir();
        let runner = Answer(true, RefCell::default());
        let scheduler = Scheduler {
            os: Os::Macos,
            runner: &runner,
        };
        let elf = check("macos", "aarch64", &exe(dir.path(), &ELF), &scheduler);
        assert_eq!(code(elf), "executable_format_unsupported");
        let short = check("macos", "aarch64", &exe(dir.path(), b"\xcf"), &scheduler);
        assert_eq!(code(short), "executable_format_unsupported");
    }
}
