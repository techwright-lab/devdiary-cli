//! Browser approval establishes a reporting credential, not host trust or hook consent.
//! Secrets live only in memory until one atomic private connection commit. The
//! server issues connections only to the verifier this terminal presents, and a
//! lost answer to that cannot be resumed: it needs revocation and a new pair.
use crate::{
    Object, Result,
    callback::{self, Listener},
    machine::{self, Machine},
    object,
    scheduler::Os,
    scope,
    store::{Store, private},
    text,
    vendor::Vendor,
};
use rustix::event::{PollFd, PollFlags, Timespec};
use serde_json::{Value, json};
use std::{
    cell::Cell,
    fs::{self, DirBuilder, File, OpenOptions},
    io::{Read, Write},
    os::unix::fs::{DirBuilderExt, OpenOptionsExt},
    path::Path,
    process::{Command, Stdio},
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};
use uuid::Uuid;
const START: &str = "/ingest/v1/collector_pairings";
const EXCHANGE: &str = "/ingest/v1/collector_pairings/exchange";
const APPROVE: &str = "/collector/approve?code=";
pub const DEFAULT_ORIGIN: &str = "https://devdiary.me";
const ALPHABET: &[u8] = b"BCDFGHJKLMNPQRSTVWXZ";
// A request lives 10 minutes on the server, which then answers `expired`.
const LONGEST_WAIT: Duration = Duration::from_secs(660);
const ATTEMPT: &str = "pairing-attempted";
const COOLDOWN: &str = "start-retry-after";
const CONNECTED: &str = "Connected. Return to the terminal.";
const NOT_CONNECTED: &str = "The terminal did not connect. Return to the terminal to see why.";

pub fn origin(s: &str, trusted: bool) -> Result<String> {
    if !trusted && s != DEFAULT_ORIGIN {
        return Err("trust_origin_required".into());
    }
    let u = reqwest::Url::parse(s)?;
    if u.path() != "/"
        || u.query().is_some()
        || u.fragment().is_some()
        || !u.username().is_empty()
        || u.password().is_some()
        || u.host_str().is_none()
        || u.as_str() != format!("{s}/")
        || !(u.scheme() == "https"
            || (trusted
                && u.scheme() == "http"
                && matches!(u.host_str(), Some("127.0.0.1" | "[::1]"))))
    {
        return Err("invalid_origin".into());
    }
    Ok(s.to_owned())
}

/// An approval code or a verifier as the server compares it: upper case, no
/// hyphens or white space, 8 letters of its alphabet.
pub(crate) fn normalised(input: &str) -> Option<String> {
    let code: String = input
        .chars()
        .filter(|c| *c != '-' && !c.is_whitespace())
        .collect::<String>()
        .to_ascii_uppercase();
    (code.len() == 8 && code.bytes().all(|b| ALPHABET.contains(&b))).then_some(code)
}
fn grouped(code: &str) -> String {
    format!("{}-{}", &code[..4], &code[4..])
}

/// Parse only documented GitHub URL shapes; do not repair arbitrary identities,
/// resolve aliases, apply insteadOf, contact remotes or modify Git configuration.
pub fn github_remote(raw: &str) -> Result<String> {
    let rest = raw
        .strip_prefix("git@github.com:")
        .or_else(|| raw.strip_prefix("ssh://git@github.com/"))
        .or_else(|| raw.strip_prefix("https://github.com/"))
        .ok_or("unsupported_remote")?;
    let rest = rest.strip_suffix(".git").unwrap_or(rest);
    let canonical = format!("https://github.com/{rest}");
    if !crate::repository(&canonical) {
        return Err("unsupported_remote".into());
    }
    Ok(canonical)
}
fn read(path: &Path) -> Result<Vec<u8>> {
    private(path, false)?;
    let mut raw = Vec::new();
    File::open(path)?.take(16385).read_to_end(&mut raw)?;
    if raw.len() > 16384 {
        return Err("invalid_connection".into());
    }
    Ok(raw)
}
fn directory(path: &Path) -> Result<()> {
    if !path.exists() {
        private(path.parent().ok_or("invalid_path")?, true)?;
        DirBuilder::new().mode(0o700).create(path)?;
        File::open(path.parent().ok_or("invalid_path")?)?.sync_all()?;
    }
    private(path, true)
}
fn save_new(path: &Path, raw: &[u8]) -> Result<()> {
    let parent = path.parent().ok_or("invalid_path")?;
    private(parent, true)?;
    let temporary = parent.join(format!(".pairing-{}.tmp", Uuid::new_v4()));
    let result = (|| -> Result<()> {
        let mut f = OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(&temporary)?;
        f.write_all(raw)?;
        f.sync_all()?;
        rustix::fs::renameat_with(
            rustix::fs::CWD,
            &temporary,
            rustix::fs::CWD,
            path,
            rustix::fs::RenameFlags::NOREPLACE,
        )?;
        File::open(parent)?.sync_all()?;
        Ok(())
    })();
    let _ = fs::remove_file(temporary);
    result
}
fn secret(s: &str, prefix: &str) -> bool {
    s.strip_prefix(prefix).is_some_and(|v| {
        v.len() == 43
            && v.bytes()
                .all(|b| b.is_ascii_alphanumeric() || b"_-".contains(&b))
    })
}
fn response(r: reqwest::blocking::Response) -> Result<Object> {
    let mut raw = Vec::new();
    r.take(65537).read_to_end(&mut raw)?;
    if raw.len() > 65536 {
        return Err("invalid_pairing_response".into());
    }
    serde_json::from_slice::<crate::claude::UniqueKeys>(&raw)?;
    object(&raw)
}
fn retry_after(r: &reqwest::blocking::Response, most: u64) -> Result<u64> {
    r.headers()
        .get("Retry-After")
        .and_then(|v| v.to_str().ok())
        .and_then(|v| v.parse::<u64>().ok())
        .filter(|n| *n <= most)
        .ok_or_else(|| "invalid_retry_after".into())
}
fn failure(doc: &Object) -> Option<&str> {
    (doc.len() == 1).then(|| doc.get("error")?.as_str())?
}
fn now() -> Result<u64> {
    Ok(SystemTime::now().duration_since(UNIX_EPOCH)?.as_secs())
}
fn connection(root: &Path) -> Result<Object> {
    private(root, true)?;
    let doc = object(&read(&root.join("connection.json"))?)?;
    let config = scope(&serde_json::to_vec(
        doc.get("scope").ok_or("invalid_connection")?,
    )?)?;
    let installation = crate::installation(&config);
    if doc.len() != 2 + usize::from(installation) || !secret(text(&doc, "token")?, "dc_live_") {
        return Err("invalid_connection".into());
    }
    if installation {
        crate::store::repository_refs(doc.get("repository_refs").ok_or("invalid_connection")?)?;
    }
    let saved = object(&read(&root.join("installation.json"))?)?;
    let frozen: &[&str] = if installation {
        &["installation_id", "scope_kind"]
    } else {
        &["installation_id", "repository", "repository_ref"]
    };
    if Vendor::from_scope(&config)? != Vendor::from_scope(&saved)?
        || frozen.iter().any(|key| config.get(*key) != saved.get(*key))
        || (installation && config["record_orchestrated_outside_repos"] != outside(&saved, &config))
    {
        return Err("frozen_scope_conflict".into());
    }
    if text(&config, "endpoint")? != format!("{}/ingest/v1/observations", text(&saved, "origin")?) {
        return Err("frozen_scope_conflict".into());
    }
    Ok(doc)
}
// The saved request only takes effect on a server that accepts schema v2.
fn outside(installation: &Object, config: &Object) -> serde_json::Value {
    json!(
        installation.get("record_orchestrated_outside_repos") == Some(&json!(true))
            && crate::wire_version(config) == 2
    )
}
fn initialize(root: &Path, doc: &Object) -> Result<()> {
    let outbox = root.join("outbox");
    directory(&outbox)?;
    Store::open(&outbox, Some(&serde_json::to_vec(&doc["scope"])?))?;
    if let Some(refs) = doc.get("repository_refs")
        && !crate::store::has_refs(&outbox)
    {
        crate::store::seed_refs(&outbox, &crate::store::repository_refs(refs)?)?;
    }
    Ok(())
}

/// One harness the person accepted, and the private folder for its connection.
pub struct Harness<'a> {
    pub vendor: Vendor,
    pub root: &'a Path,
}
pub struct Options<'a> {
    pub harnesses: &'a [Harness<'a>],
    pub origin: &'a str,
    pub trust_origin: bool,
    pub browser: bool,
    pub record_orchestrated_outside_repos: bool,
    /// Set once a pairing request has been sent, so setup can say what left this machine.
    pub sent: &'a Cell<bool>,
}
/// What this machine asks to pair: its installation, covering every tracked repository.
struct Target {
    record_outside: bool,
}
impl Target {
    fn installation_record(&self, origin: &str, id: &str, vendor: Vendor) -> Value {
        let mut saved = json!({"installation_id":id, "origin":origin, "scope_kind":"installation", "record_orchestrated_outside_repos":self.record_outside});
        if vendor == Vendor::Codex {
            saved["runtime"] = json!(vendor.runtime());
        }
        saved
    }
    fn matches(&self, saved: &Object, origin: &str) -> bool {
        saved.len() == 4 + usize::from(saved.contains_key("runtime"))
            && saved.get("scope_kind") == Some(&json!("installation"))
            && saved.get("record_orchestrated_outside_repos") == Some(&json!(self.record_outside))
            && saved.get("origin") == Some(&json!(origin))
    }
    fn approved(
        &self,
        c: &Object,
        installation_id: &str,
        origin: &str,
        vendor: Vendor,
    ) -> Result<Value> {
        if c.len() != 9
            || c.get("installation_id") != Some(&json!(installation_id))
            || c.get("runtime") != Some(&json!(vendor.runtime()))
            || c.get("endpoint_path") != Some(&json!("/ingest/v1/observations"))
            || !secret(text(c, "token")?, "dc_live_")
            || !text(c, "collector_ref")?
                .strip_prefix("urn:devdiary:collector:")
                .is_some_and(crate::uuid)
        {
            return Err("approved_scope_mismatch".into());
        }
        let endpoint = format!("{origin}/ingest/v1/observations");
        let mut common = json!({"endpoint":endpoint,"collector_ref":c["collector_ref"],"installation_id":installation_id});
        if vendor == Vendor::Codex {
            common["runtime"] = json!(vendor.runtime());
        }
        let versions = crate::store::accepted_versions(c.get("observation_schema_versions"))
            .ok_or("approved_scope_mismatch")?;
        if c.get("scope_kind") != Some(&json!("installation"))
            || c.get("scope_path") != Some(&json!("/ingest/v1/collector/scope"))
        {
            return Err("approved_scope_mismatch".into());
        }
        let refs = crate::store::repository_refs(&c["repository_refs"])
            .map_err(|_| "approved_scope_mismatch")?;
        let mut config = common;
        config["scope_kind"] = json!("installation");
        let v2 = versions.contains(&json!(2));
        if self.record_outside && !v2 {
            eprintln!(
                "Server does not accept observation schema v2: --record-orchestrated-outside-repos is not saved."
            );
        }
        config["observation_schema_versions"] = if v2 { json!([1, 2]) } else { json!([1]) };
        config["record_orchestrated_outside_repos"] = json!(self.record_outside && v2);
        scope(&serde_json::to_vec(&config)?)?;
        Ok(json!({"scope":config,"token":c["token"],"repository_refs":refs}))
    }
    /// The exchange answer as one saved connection per harness, in the
    /// request's order, and the workspace they joined.
    fn connections(
        &self,
        doc: &Object,
        request: &Request,
        origin: &str,
        harnesses: &[Vendor],
    ) -> Result<(String, Vec<Value>)> {
        let list = doc
            .get("connections")
            .and_then(Value::as_array)
            .filter(|list| list.len() == harnesses.len())
            .ok_or("approved_scope_mismatch")?;
        let workspace = doc.get("workspace_name").and_then(Value::as_str);
        let (true, Some(workspace)) = (
            doc.len() == 4
                && doc["schema_version"] == json!(2)
                && doc["status"] == json!("consumed"),
            workspace,
        ) else {
            return Err("invalid_pairing_response".into());
        };
        let saved = list
            .iter()
            .zip(harnesses)
            .map(|(c, vendor)| {
                let c = c.as_object().ok_or("approved_scope_mismatch")?;
                self.approved(c, &request.installation_id, origin, *vendor)
            })
            .collect::<Result<Vec<_>>>()?;
        Ok((workspace.to_owned(), saved))
    }
}

fn start_body(
    harnesses: &[Vendor],
    installation_id: &str,
    machine: &Machine,
    port: Option<u16>,
) -> Value {
    let mut body = json!({
        "schema_version": 2,
        "installation_id": installation_id,
        "harnesses": harnesses.iter().map(|v| v.runtime()).collect::<Vec<_>>(),
        "scope_kind": "installation",
        "hostname": machine.hostname,
        "os_name": machine.os_name,
        "os_version": machine.os_version,
        "arch": machine.arch,
        "cli_version": machine.cli_version,
    });
    if let Some(port) = port {
        body["callback_port"] = json!(port);
    }
    body
}

pub fn setup(o: Options<'_>) -> Result<()> {
    let origin = origin(o.origin, o.trust_origin)?;
    let target = Target {
        record_outside: o.record_orchestrated_outside_repos,
    };
    let mut locks = Vec::new();
    let mut unpaired = Vec::new();
    for harness in o.harnesses {
        locks.push(lock(harness.root)?);
        if harness.root.join("connection.json").exists() {
            let installation = object(&read(&harness.root.join("installation.json"))?)?;
            if Vendor::from_scope(&installation)? != harness.vendor
                || !target.matches(&installation, &origin)
                || !crate::uuid(text(&installation, "installation_id")?)
            {
                return Err("frozen_scope_conflict".into());
            }
            initialize(harness.root, &connection(harness.root)?)?;
            println!(
                "{}: connection already saved, so it is not paired again.",
                harness.vendor.name()
            );
        } else {
            unused(harness.root)?;
            unpaired.push(harness);
        }
    }
    if unpaired.is_empty() {
        return Ok(());
    }
    let client = reqwest::blocking::Client::builder()
        .no_proxy()
        .retry(reqwest::retry::never())
        .redirect(reqwest::redirect::Policy::none())
        .connect_timeout(Duration::from_secs(4))
        .timeout(Duration::from_secs(5))
        .build()?;
    Pairing {
        client,
        origin: &origin,
        target: &target,
        harnesses: &unpaired,
        listener: Listener::bind(),
        typed: Typed::default(),
        sent: o.sent,
    }
    .run(o.browser)
}
fn lock(root: &Path) -> Result<File> {
    directory(root)?;
    let path = root.join("setup.lock");
    if !path.exists() {
        save_new(&path, b"")?;
    }
    private(&path, false)?;
    let lock = OpenOptions::new().read(true).write(true).open(path)?;
    lock.try_lock()?;
    Ok(lock)
}
// Never adopt a preexisting outbox or connection into a new installation. A
// leftover attempt marker means a verifier was presented and its answer lost.
fn unused(root: &Path) -> Result<()> {
    let cooldown = root.join(COOLDOWN);
    if cooldown.exists() {
        let until: u64 = std::str::from_utf8(&read(&cooldown)?)?.parse()?;
        if now()? < until {
            return Err("start_rate_limited".into());
        }
        fs::remove_file(&cooldown)?;
        File::open(root)?.sync_all()?;
    }
    let attempt = root.join(ATTEMPT);
    if attempt.exists() {
        private(&attempt, false)?;
        return Err("repair_new_pair".into());
    }
    for entry in fs::read_dir(root)? {
        let name = entry?.file_name();
        let name = name.to_string_lossy();
        if name != "setup.lock" && !(name.starts_with(".pairing-") && name.ends_with(".tmp")) {
            return Err("new_private_directory_required".into());
        }
    }
    Ok(())
}

struct Request {
    installation_id: String,
    token: String,
    code: String,
    interval: u64,
}
enum Waited {
    Connected(Object),
    Expired,
}
enum Polled {
    Waiting,
    Approved,
    Later(u64),
    Expired,
}
/// The server's answer to one presented verifier.
enum Tried {
    Connected(Object),
    Wrong(u64),
    Refused,
    Denied,
    Expired,
}
/// Lines the person types while setup waits, read from the terminal itself so
/// that a line typed early is never held in a buffer the wait cannot see.
struct Typed {
    pending: Vec<u8>,
    open: bool,
}
impl Default for Typed {
    fn default() -> Self {
        Self {
            pending: Vec::new(),
            open: true,
        }
    }
}
impl Typed {
    fn lines(&mut self) -> Vec<String> {
        let mut chunk = [0; 256];
        match rustix::io::read(std::io::stdin(), &mut chunk[..]) {
            Ok(0) => self.open = false,
            Ok(n) => self.pending.extend_from_slice(&chunk[..n]),
            Err(rustix::io::Errno::INTR | rustix::io::Errno::AGAIN) => {}
            Err(_) => self.open = false,
        }
        if self.pending.len() > 4096 {
            self.pending.clear();
        }
        let mut lines = Vec::new();
        while let Some(end) = self.pending.iter().position(|b| *b == b'\n') {
            let line: Vec<u8> = self.pending.drain(..=end).collect();
            lines.push(String::from_utf8_lossy(&line).into_owned());
        }
        lines
    }
}

struct Pairing<'a> {
    client: reqwest::blocking::Client,
    origin: &'a str,
    target: &'a Target,
    harnesses: &'a [&'a Harness<'a>],
    listener: Option<Listener>,
    typed: Typed,
    sent: &'a Cell<bool>,
}
impl Pairing<'_> {
    fn run(mut self, browser: bool) -> Result<()> {
        let mut first = true;
        loop {
            let request = self.start()?;
            let code = grouped(&request.code);
            let link = format!("{}{APPROVE}{code}", self.origin);
            println!("Approval code: {code}\nApprove at {link}");
            println!(
                "{}\nBrowser approval does not install hooks or establish host trust.",
                if self.listener.is_some() {
                    "When you approve, the browser returns here. If it can't, type the verifier the approval page shows, then press Enter."
                } else {
                    "After you approve, type the verifier the approval page shows, then press Enter."
                }
            );
            std::io::stdout().flush()?;
            // The server's own copy tells the person to open a fresh request's
            // link themselves, so only the first one opens on its own.
            if browser && first {
                open(&link);
            }
            first = false;
            match self.wait(&request)? {
                Waited::Connected(doc) => return self.save(&request, &doc),
                Waited::Expired => {
                    println!("This setup expired before it was approved. A new one starts now.")
                }
            }
        }
    }
    fn vendors(&self) -> Vec<Vendor> {
        self.harnesses.iter().map(|h| h.vendor).collect()
    }
    fn start(&self) -> Result<Request> {
        let installation_id = Uuid::new_v4().to_string();
        let body = start_body(
            &self.vendors(),
            &installation_id,
            &machine::this(),
            self.listener.as_ref().map(Listener::port),
        );
        self.sent.set(true);
        let r = self
            .client
            .post(format!("{}{START}", self.origin))
            .header("Content-Type", "application/json")
            .body(serde_json::to_vec(&body)?)
            .send()
            .map_err(|_| "pairing_unavailable")?;
        if r.status().as_u16() == 429 {
            let seconds = retry_after(&r, 86400)?.max(5);
            for harness in self.harnesses {
                save_new(
                    &harness.root.join(COOLDOWN),
                    (now()? + seconds).to_string().as_bytes(),
                )?;
            }
            eprintln!(
                "Pairing rate limited: wait at least {seconds} seconds before running setup again."
            );
            return Err("start_rate_limited".into());
        }
        if r.status().as_u16() != 201 {
            return Err("pairing_unavailable".into());
        }
        let start = response(r)?;
        let token = text(&start, "pairing_token")?;
        let shown = text(&start, "approval_code")?;
        let code = normalised(shown).filter(|code| grouped(code) == shown);
        let interval = start
            .get("interval")
            .and_then(Value::as_u64)
            .filter(|n| (5..=600).contains(n));
        let expiry = chrono::DateTime::parse_from_rfc3339(text(&start, "expires_at")?)?.timestamp();
        let now = now()? as i64;
        match (code, interval) {
            (Some(code), Some(interval))
                if start.len() == 6
                    && start["schema_version"] == json!(2)
                    && secret(token, "dp_pair_")
                    && start["approval_path"] == json!(format!("{APPROVE}{shown}"))
                    && expiry > now
                    && expiry <= now + 600 =>
            {
                Ok(Request {
                    installation_id,
                    token: token.to_owned(),
                    code,
                    interval,
                })
            }
            _ => Err("invalid_pairing_response".into()),
        }
    }
    fn wait(&mut self, request: &Request) -> Result<Waited> {
        let began = Instant::now();
        let mut next_poll = began + Duration::from_secs(request.interval);
        let mut approved = false;
        loop {
            if began.elapsed() > LONGEST_WAIT {
                return Err("pairing_unavailable".into());
            }
            let (called, typed) =
                self.ready(next_poll.saturating_duration_since(Instant::now()))?;
            if called
                && let Some((stream, verifier)) = self
                    .listener
                    .as_ref()
                    .and_then(|l| l.receive(&request.code))
            {
                let tried = self.present(request, &verifier);
                let page = match tried {
                    Ok(Tried::Connected(_)) => CONNECTED,
                    _ => NOT_CONNECTED,
                };
                callback::answer(stream, 200, page);
                if let Some(done) = settle(tried?)? {
                    return Ok(done);
                }
            }
            if typed {
                for line in self.typed.lines() {
                    if line.trim().is_empty() {
                        continue;
                    }
                    let Some(verifier) = normalised(&line) else {
                        println!("Verifiers are 8 letters, such as BCDF-GHJK. Type it again.");
                        continue;
                    };
                    if let Some(done) = settle(self.present(request, &verifier)?)? {
                        return Ok(done);
                    }
                }
            }
            if Instant::now() >= next_poll {
                let delay = match self.poll(request)? {
                    Polled::Waiting => request.interval,
                    Polled::Approved => {
                        if !approved {
                            approved = true;
                            println!(
                                "Approved in DevDiary. If the browser didn't return here, type the verifier the approval page shows."
                            );
                        }
                        request.interval
                    }
                    Polled::Later(seconds) => seconds.max(request.interval),
                    Polled::Expired => return Ok(Waited::Expired),
                };
                next_poll = Instant::now() + Duration::from_secs(delay);
            }
        }
    }
    /// Whether the callback listener and the terminal have input, waiting at
    /// most `timeout`.
    fn ready(&self, timeout: Duration) -> Result<(bool, bool)> {
        let stdin = std::io::stdin();
        let mut fds = Vec::with_capacity(2);
        let typed = self.typed.open.then(|| {
            fds.push(PollFd::new(&stdin, PollFlags::IN));
            fds.len() - 1
        });
        let called = self.listener.as_ref().map(|l| {
            fds.push(PollFd::new(l.socket(), PollFlags::IN));
            fds.len() - 1
        });
        let timeout = Timespec {
            tv_sec: timeout.as_secs() as _,
            tv_nsec: timeout.subsec_nanos() as _,
        };
        match rustix::event::poll(&mut fds, Some(&timeout)) {
            Err(rustix::io::Errno::INTR) => return Ok((false, false)),
            result => result?,
        };
        let has = |index: Option<usize>| {
            index.is_some_and(|i| {
                fds[i]
                    .revents()
                    .intersects(PollFlags::IN | PollFlags::HUP | PollFlags::ERR)
            })
        };
        Ok((has(called), has(typed)))
    }
    fn poll(&self, request: &Request) -> Result<Polled> {
        // A poll never issues connections, so one that gets no answer is only retried.
        let Ok(r) = self
            .client
            .post(format!("{}{EXCHANGE}", self.origin))
            .bearer_auth(&request.token)
            .header("Content-Type", "application/json")
            .body(r#"{"schema_version":2}"#)
            .send()
        else {
            return Ok(Polled::Waiting);
        };
        match r.status().as_u16() {
            429 => Ok(Polled::Later(retry_after(&r, 3600)?)),
            500..=599 => Ok(Polled::Waiting),
            200 => {
                let doc = response(r)?;
                match doc.get("status").and_then(Value::as_str) {
                    Some("pending") if doc.len() == 2 && doc["schema_version"] == json!(2) => {
                        Ok(Polled::Waiting)
                    }
                    Some("approved") if doc.len() == 2 && doc["schema_version"] == json!(2) => {
                        Ok(Polled::Approved)
                    }
                    _ => Err("invalid_pairing_response".into()),
                }
            }
            410 => match failure(&response(r)?) {
                Some("denied") => Err("pairing_denied".into()),
                Some("expired") => Ok(Polled::Expired),
                _ => Err("repair_new_pair".into()),
            },
            _ => Err("pairing_unavailable".into()),
        }
    }
    // Only a right verifier makes the server issue connections, so only this
    // call can leave credentials behind that never reached this machine.
    fn present(&self, request: &Request, verifier: &str) -> Result<Tried> {
        for harness in self.harnesses {
            save_new(
                &harness.root.join(ATTEMPT),
                b"A pairing may have been consumed. Revoke unused connections in the browser before pairing again.\n",
            )?;
        }
        let tried = self.exchange(request, verifier);
        if let Ok(Tried::Wrong(_) | Tried::Refused | Tried::Denied | Tried::Expired) = tried {
            for harness in self.harnesses {
                fs::remove_file(harness.root.join(ATTEMPT))?;
                File::open(harness.root)?.sync_all()?;
            }
        }
        tried
    }
    fn exchange(&self, request: &Request, verifier: &str) -> Result<Tried> {
        let r = self
            .client
            .post(format!("{}{EXCHANGE}", self.origin))
            .bearer_auth(&request.token)
            .header("Content-Type", "application/json")
            .body(serde_json::to_vec(
                &json!({"schema_version": 2, "verifier": verifier}),
            )?)
            .send()
            .map_err(|_| "repair_new_pair")?;
        let status = r.status().as_u16();
        if status == 429 {
            return Ok(Tried::Refused);
        }
        let doc = response(r).map_err(|_| "repair_new_pair")?;
        let left = doc.get("attempts_left").and_then(Value::as_u64);
        Ok(match (status, failure(&doc)) {
            (200, _) => Tried::Connected(doc),
            (403, _) if doc.len() == 2 && doc["error"] == json!("wrong_verifier") => Tried::Wrong(
                left.filter(|n| (1..=4).contains(n))
                    .ok_or("repair_new_pair")?,
            ),
            (422, Some("invalid_verifier")) => Tried::Refused,
            (410, Some("denied")) => Tried::Denied,
            (410, Some("expired")) => Tried::Expired,
            _ => return Err("repair_new_pair".into()),
        })
    }
    fn save(&self, request: &Request, doc: &Object) -> Result<()> {
        let (workspace, saved) =
            self.target
                .connections(doc, request, self.origin, &self.vendors())?;
        let records = self
            .harnesses
            .iter()
            .map(|h| {
                self.target
                    .installation_record(self.origin, &request.installation_id, h.vendor)
            })
            .collect::<Vec<_>>();
        commit(self.harnesses, &records, &saved)?;
        println!(
            "Connected to the DevDiary workspace {}.\nConnection saved privately.",
            machine::shown(&workspace)
        );
        Ok(())
    }
}
fn settle(tried: Tried) -> Result<Option<Waited>> {
    match tried {
        Tried::Connected(doc) => Ok(Some(Waited::Connected(doc))),
        Tried::Wrong(left) => {
            let attempts = if left == 1 { "attempt" } else { "attempts" };
            println!("That verifier is wrong. {left} {attempts} left.");
            Ok(None)
        }
        Tried::Refused => {
            println!("DevDiary did not take that verifier. Type the one the approval page shows.");
            Ok(None)
        }
        Tried::Denied => Err("pairing_denied".into()),
        Tried::Expired => Ok(Some(Waited::Expired)),
    }
}
// If interrupted between harnesses, the next setup reuses each committed
// connection and refuses the rest until their folders are moved aside.
fn commit(harnesses: &[&Harness<'_>], records: &[Value], saved: &[Value]) -> Result<()> {
    for ((harness, record), saved) in harnesses.iter().zip(records).zip(saved) {
        save_new(
            &harness.root.join("installation.json"),
            &serde_json::to_vec(record)?,
        )
        .map_err(|_| "repair_new_pair")?;
        save_new(
            &harness.root.join("connection.json"),
            &serde_json::to_vec(saved)?,
        )
        .map_err(|_| "repair_new_pair")?;
        initialize(harness.root, saved.as_object().ok_or("invalid_connection")?)?;
    }
    Ok(())
}
// The approval code is not a secret, so its link may sit in the opener's arguments.
fn open(link: &str) {
    let fake = cfg!(debug_assertions)
        .then(|| std::env::var_os("DEVDIARY_DEBUG_OPENER"))
        .flatten();
    let opener = fake.unwrap_or_else(|| opener(Os::THIS).into());
    let _ = Command::new(opener)
        .arg(link)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn();
}
// Only a paired outbox has a revocation marker. A plain state's parent is an
// arbitrary user directory whose entries say nothing about this connection.
pub(crate) fn revoked(state: &Path) -> bool {
    state.file_name().is_some_and(|name| name == "outbox")
        && state.parent().is_some_and(|root| {
            root.join("connection.json").is_file() && root.join("revoked").exists()
        })
}
pub fn plan_with(
    root: &Path,
    settings: &Path,
    plan: &Path,
    exe: &Path,
    agreed: Option<&str>,
) -> Result<()> {
    let doc = connection(root)?;
    let config = doc["scope"].as_object().ok_or("invalid_connection")?;
    if !crate::installation(config) {
        return Err("invalid_connection".into());
    }
    initialize(root, &doc)?;
    crate::claude::plan_for(&root.join("outbox"), settings, plan, exe, agreed)
}
pub fn sync(root: &Path, limit: usize) -> Result<crate::store::Report> {
    let doc = connection(root)?;
    initialize(root, &doc)?;
    crate::store::sync_report(
        &root.join("outbox"),
        text(&doc, "token")?,
        limit,
        Some(&root.join("revoked")),
    )
}
pub(crate) fn refresh_scope(root: &Path) -> Result<()> {
    let doc = connection(root)?;
    crate::store::scope_now(
        &root.join("outbox"),
        text(&doc, "token")?,
        Some(&root.join("revoked")),
    )
}
fn opener(os: Os) -> &'static str {
    match os {
        Os::Linux => "/usr/bin/xdg-open",
        Os::Macos => "/usr/bin/open",
    }
}
pub const SETUP_INCOMPLETE: &str = "Setup incomplete. No secret is recoverable from a consumed exchange. Revoke unused connections in the browser, then move the connection folder aside and run setup again. The folder is ~/.local/share/devdiary/cli/claude/connection, or codex/connection for Codex.";
pub fn guidance(code: &str) -> Option<&'static str> {
    Some(match code {
        "trust_origin_required" | "invalid_origin" => {
            "Use the default HTTPS origin or an explicit --origin ORIGIN --trust-origin (HTTP only for numeric loopback)."
        }
        "frozen_scope_conflict" => {
            "The saved connection was made for a different scope and cannot be changed. To pair again, move the connection folder aside; it keeps its outbox."
        }
        "start_rate_limited" => {
            "Pairing starts are rate limited. The saved Retry-After cooldown must expire before setup tries again."
        }
        "pairing_unavailable" => {
            "DevDiary did not accept the pairing request. Nothing changed on this machine. Run `devdiary setup` again later."
        }
        "pairing_denied" => {
            "This setup was denied in DevDiary, or a wrong verifier was typed five times. Nothing changed on this machine. Run `devdiary setup` to start again."
        }
        "scope_refresh_network_failure" | "scope_refresh_read_failure" => {
            "Scope refresh failed. Check the connection and retry sync. Stored scope remains unchanged."
        }
        "scope_refresh_http_failure" => {
            "The server refused scope refresh. Check server access and retry sync. Stored scope remains unchanged."
        }
        "scope_refresh_invalid_response" | "scope_refresh_response_too_large" => {
            "The scope response is invalid. Check server compatibility and retry sync. Stored scope remains unchanged."
        }
        "scope_refresh_offer_refused" => {
            "The server offered an orchestrator entry that is not allowed. Check server compatibility and retry sync. The refused entry was not saved."
        }
        "revoked" => {
            "The server revoked this connection. Capture and sync have stopped; run uninstall, move the connection folder aside, then run setup again."
        }
        "sync_rate_limited" => {
            "The server asked the collector to wait. Sync resumes after the saved Retry-After time."
        }
        "repair_required" => {
            "This outbox uses an older storage format that this version does not read. Move the connection folder aside and run setup again."
        }
        "python_observer_registered" => {
            "These settings already register the Python observer hook. Remove it before registering this collector."
        }
        "unsafe_settings" => {
            "Settings and their directory must be owned by you and not writable by group or others, with no symlinks or hardlinks."
        }
        "approved_scope_mismatch" => {
            "The approved scope does not match this installation. Revoke the unused browser connection, move the connection folder aside, then run setup again. Hooks are unchanged."
        }
        _ => return None,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn each_platform_opens_the_browser_with_its_own_system_opener() {
        assert_eq!(opener(Os::Macos), "/usr/bin/open");
        assert_eq!(opener(Os::Linux), "/usr/bin/xdg-open");
    }
    #[test]
    fn origins_are_exact_and_explicit() {
        assert!(origin(DEFAULT_ORIGIN, false).is_ok());
        assert!(origin("http://127.0.0.1:1234", true).is_ok());
        for s in [
            "https://evil.test",
            "https://devdiary.me/",
            "https://devdiary.me?x",
            "https://u:p@devdiary.me",
            "http://devdiary.me",
            "https://devdiary.me/a/..",
            "https://DEVDIARY.me",
            "http://localhost:1234",
        ] {
            assert!(origin(s, false).is_err(), "{s}");
        }
        for s in [
            "http://evil.test",
            "https://devdiary.me/",
            "https://devdiary.me/a/..",
            "https://u:p@devdiary.me",
        ] {
            assert!(origin(s, true).is_err());
        }
    }
    #[test]
    fn codes_and_verifiers_are_compared_as_eight_letters_of_the_alphabet() {
        for typed in ["KBRT-WMPH", "kbrtwmph", " kbrt - wmph\n"] {
            assert_eq!(normalised(typed).as_deref(), Some("KBRTWMPH"), "{typed}");
        }
        for typed in [
            "KBRT-WMP",
            "KBRT-WMPHX",
            "KBRT-WMPA",
            "KBRT-WMP1",
            "KBRT_WMPH",
        ] {
            assert_eq!(normalised(typed), None, "{typed}");
        }
    }
    fn machine() -> Machine {
        Machine {
            hostname: "build-box".into(),
            os_name: "Linux",
            os_version: "6.8.0".into(),
            arch: "x86_64",
            cli_version: "0.4.0",
        }
    }
    #[test]
    fn one_start_names_every_harness_and_the_port_only_when_listening() {
        let id = "11111111-1111-4111-8111-111111111111";
        let both = start_body(&[Vendor::Claude, Vendor::Codex], id, &machine(), Some(4100));
        assert_eq!(
            both,
            json!({"schema_version": 2, "installation_id": id, "harnesses": ["claude-code", "codex"],
                "scope_kind": "installation", "hostname": "build-box", "os_name": "Linux",
                "os_version": "6.8.0", "arch": "x86_64", "cli_version": "0.4.0", "callback_port": 4100})
        );
        let deaf = start_body(&[Vendor::Codex], id, &machine(), None);
        assert!(!deaf.as_object().unwrap().contains_key("callback_port"));
    }
    #[test]
    fn one_exchange_saves_each_harness_its_own_credential_and_outbox() {
        let dir = crate::tempdir();
        let cli = dir.path().join("cli");
        DirBuilder::new().mode(0o700).create(&cli).unwrap();
        let roots = [cli.join("claude"), cli.join("codex")];
        let harnesses = [
            Harness {
                vendor: Vendor::Claude,
                root: &roots[0],
            },
            Harness {
                vendor: Vendor::Codex,
                root: &roots[1],
            },
        ];
        for root in &roots {
            directory(root).unwrap();
        }
        let request = Request {
            installation_id: "11111111-1111-4111-8111-111111111111".into(),
            token: String::new(),
            code: "KBRTWMPH".into(),
            interval: 5,
        };
        let issued = |runtime: &str, token: char| {
            json!({"collector_ref": "urn:devdiary:collector:22222222-2222-4222-8222-222222222222",
                "token": format!("dc_live_{}", token.to_string().repeat(43)),
                "installation_id": request.installation_id, "runtime": runtime,
                "endpoint_path": "/ingest/v1/observations", "scope_kind": "installation",
                "repository_refs": ["https://github.com/acme/demo"],
                "scope_path": "/ingest/v1/collector/scope", "observation_schema_versions": [1, 2]})
        };
        let doc = json!({"schema_version": 2, "status": "consumed", "workspace_name": "acme-diary",
            "connections": [issued("claude-code", 'a'), issued("codex", 'b')]});
        let doc = doc.as_object().unwrap();
        let origin = "http://127.0.0.1:4000";
        let target = Target {
            record_outside: false,
        };
        let vendors = [Vendor::Claude, Vendor::Codex];
        let (workspace, saved) = target.connections(doc, &request, origin, &vendors).unwrap();
        assert_eq!(workspace, "acme-diary");
        let records: Vec<_> = vendors
            .iter()
            .map(|v| target.installation_record(origin, &request.installation_id, *v))
            .collect();
        commit(&harnesses.iter().collect::<Vec<_>>(), &records, &saved).unwrap();
        let claude = connection(&roots[0]).unwrap();
        let codex = connection(&roots[1]).unwrap();
        assert_eq!(
            claude["token"],
            json!(format!("dc_live_{}", "a".repeat(43)))
        );
        assert_eq!(codex["token"], json!(format!("dc_live_{}", "b".repeat(43))));
        assert_eq!(
            Vendor::from_scope(claude["scope"].as_object().unwrap()).unwrap(),
            Vendor::Claude
        );
        assert_eq!(
            Vendor::from_scope(codex["scope"].as_object().unwrap()).unwrap(),
            Vendor::Codex
        );
        for root in &roots {
            assert!(root.join("outbox").is_dir());
        }
        let swapped = json!({"schema_version": 2, "status": "consumed", "workspace_name": "acme-diary",
            "connections": [issued("codex", 'b'), issued("claude-code", 'a')]});
        let swapped = swapped.as_object().unwrap();
        assert!(
            target
                .connections(swapped, &request, origin, &vendors)
                .is_err()
        );
    }
    #[test]
    fn remote_parser_does_not_repair_identity() {
        for s in [
            "git@github.com:Owner/Repo.git",
            "ssh://git@github.com/Owner/Repo.git",
            "https://github.com/Owner/Repo",
        ] {
            assert_eq!(github_remote(s).unwrap(), "https://github.com/Owner/Repo");
        }
        for s in [
            "https://github.com/owner/repo/",
            "https://github.com/owner/repo?x",
            "ssh://git@github.com:22/owner/repo",
            "https://github.com/owner/../repo",
            "git@evil:owner/repo",
            "https://user@github.com/owner/repo",
            "https://github.com/owner/%72epo",
        ] {
            assert!(github_remote(s).is_err(), "{s}");
        }
    }
}
