//! Execution harness identity, independent of actor and control plane.
use crate::{Object, Result};

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum Vendor {
    #[default]
    Claude,
    Codex,
}

impl Vendor {
    pub fn parse(name: &str) -> Result<Self> {
        match name {
            "claude" => Ok(Self::Claude),
            "codex" => Ok(Self::Codex),
            _ => Err("unsupported_vendor".into()),
        }
    }

    pub fn runtime(self) -> &'static str {
        match self {
            Self::Claude => "claude-code",
            Self::Codex => "codex",
        }
    }

    pub fn from_scope(scope: &Object) -> Result<Self> {
        match scope.get("runtime") {
            None => Ok(Self::Claude),
            Some(serde_json::Value::String(s)) if s == "claude-code" => Ok(Self::Claude),
            Some(serde_json::Value::String(s)) if s == "codex" => Ok(Self::Codex),
            _ => Err("unsupported_vendor".into()),
        }
    }
    pub fn slug(self) -> &'static str {
        match self {
            Self::Claude => "claude",
            Self::Codex => "codex",
        }
    }
    pub fn hook(self) -> &'static str {
        match self {
            Self::Claude => "claude-hook",
            Self::Codex => "codex-hook",
        }
    }
    pub fn events(self) -> &'static [&'static str] {
        match self {
            Self::Claude => &crate::claude::EVENTS,
            Self::Codex => &[
                "SessionStart",
                "UserPromptSubmit",
                "PreToolUse",
                "PostToolUse",
                "PermissionRequest",
                "Stop",
                "SubagentStart",
                "SubagentStop",
                "SessionEnd",
                "Interrupt",
            ],
        }
    }
    // Only these events may be absent from older registrations of this vendor.
    // Keep original hooks strict, and extend this list when adding a new event.
    pub fn added_later(self) -> &'static [&'static str] {
        match self {
            Self::Claude => &["PermissionRequest"],
            Self::Codex => &[],
        }
    }
    pub fn service(self) -> &'static str {
        match self {
            Self::Claude => crate::systemd::SERVICE,
            Self::Codex => "devdiary-collector-codex-sync.service",
        }
    }
    pub fn timer(self) -> &'static str {
        match self {
            Self::Claude => crate::systemd::TIMER,
            Self::Codex => "devdiary-collector-codex-sync.timer",
        }
    }
}
