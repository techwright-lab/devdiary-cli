//! The per-user job that runs `sync` every 2 minutes: a systemd user timer on
//! Linux, a launchd agent on macOS.
use crate::{Result, launchd, systemd, vendor::Vendor};
use serde_json::Value;
use std::path::Path;

pub trait Runner {
    fn run(&self, args: &[&str]) -> Result<(bool, String)>;
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Os {
    Linux,
    Macos,
}
impl Os {
    // Other targets fail the platform preflight before any scheduler call.
    pub const THIS: Self = if cfg!(target_os = "macos") {
        Self::Macos
    } else {
        Self::Linux
    };
}

pub struct Scheduler<'a> {
    pub os: Os,
    pub runner: &'a dyn Runner,
}
impl Scheduler<'static> {
    /// The real scheduler of this build's operating system.
    pub fn system() -> Self {
        match Os::THIS {
            Os::Linux => Self {
                os: Os::Linux,
                runner: &systemd::Systemctl,
            },
            Os::Macos => Self {
                os: Os::Macos,
                runner: &launchd::Launchctl,
            },
        }
    }
}
impl Scheduler<'_> {
    pub fn reachable(&self) -> Result<()> {
        match self.os {
            Os::Linux => systemd::reachable(self.runner),
            Os::Macos => launchd::reachable(self.runner, launchd::uid()),
        }
    }
    pub fn enable(&self, exe: &Path, connection: &Path, vendor: Vendor) -> Result<()> {
        match self.os {
            Os::Linux => {
                systemd::enable_for(self.runner, &systemd::unit_dir()?, exe, connection, vendor)
            }
            Os::Macos => launchd::enable(
                self.runner,
                &launchd::agent_dir()?,
                launchd::uid(),
                exe,
                connection,
                vendor,
            ),
        }
    }
    /// Returns false when no job definition was installed.
    pub fn disable(&self, vendor: Vendor) -> Result<bool> {
        match self.os {
            Os::Linux => systemd::disable_for(self.runner, &systemd::unit_dir()?, vendor),
            Os::Macos => {
                launchd::disable(self.runner, &launchd::agent_dir()?, launchd::uid(), vendor)
            }
        }
    }
    pub fn state(&self, vendor: Vendor) -> Value {
        match self.os {
            Os::Linux => systemd::state_for(self.runner, vendor),
            Os::Macos => launchd::state(self.runner, launchd::uid(), vendor),
        }
    }
}
