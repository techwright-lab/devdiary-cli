//! Browser approval establishes a reporting credential, not host trust or hook consent.
//! Secrets live only in memory until one atomic private connection commit. Pairing
//! bearers cannot be resumed: a lost exchange requires revocation and a new pair.
use crate::{
    Object, Result, object, scope,
    store::{Store, private},
    text,
};
use serde_json::json;
use std::{
    fs::{self, DirBuilder, File, OpenOptions},
    io::{Read, Write},
    os::unix::fs::{DirBuilderExt, OpenOptionsExt},
    path::Path,
    process::{Command, Stdio},
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};
use uuid::Uuid;
// The server names its approval page, but only from this fixed set.
const VERIFY: [&str; 2] = ["/settings/collector", "/phoenix/settings/collector"];
const START: &str = "/ingest/v1/collector_pairings";
const EXCHANGE: &str = "/ingest/v1/collector_pairings/exchange";
pub const DEFAULT_ORIGIN: &str = "https://devdiary.me";

fn verification_path(value: &serde_json::Value) -> Result<&'static str> {
    VERIFY
        .into_iter()
        .find(|p| value.as_str() == Some(*p))
        .ok_or_else(|| "invalid_pairing_response".into())
}
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
fn git(repo: &Path, args: &[&str]) -> Result<String> {
    // Native Linux slice: absolute executable, no shell, inherited Git overrides,
    // credential helpers, pager, global config, or external command execution.
    let mut child = Command::new("/usr/bin/git")
        .env_clear()
        .env("PATH", "/usr/bin:/bin")
        .env("GIT_CONFIG_NOSYSTEM", "1")
        .env("GIT_CONFIG_GLOBAL", "/dev/null")
        .env("GIT_OPTIONAL_LOCKS", "0")
        .arg("-C")
        .arg(repo)
        .args(args)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()?;
    let stdout = child.stdout.take().ok_or("git_discovery_failed")?;
    let (tx, rx) = std::sync::mpsc::sync_channel(1);
    std::thread::spawn(move || {
        let mut bytes = Vec::new();
        let result = stdout.take(65537).read_to_end(&mut bytes).map(|_| bytes);
        let _ = tx.send(result);
    });
    let deadline = Instant::now() + Duration::from_secs(3);
    let output = rx.recv_timeout(Duration::from_secs(3));
    // Git config/rev-parse cannot execute helpers. Reap the one owned process
    // even if a filesystem hangs or the bounded reader hits its output ceiling.
    let oversized = output
        .as_ref()
        .is_ok_and(|r| r.as_ref().is_ok_and(|v| v.len() > 65536));
    while !oversized && child.try_wait()?.is_none() && Instant::now() < deadline {
        std::thread::sleep(Duration::from_millis(5));
    }
    if child.try_wait()?.is_none() {
        let _ = child.kill();
    }
    let status = child.wait()?;
    let output = output??;
    if !status.success() || output.len() > 65536 {
        return Err("git_discovery_failed".into());
    }
    Ok(String::from_utf8(output)?)
}
fn repository(repo: &Path, remote: Option<&str>) -> Result<(String, String)> {
    let repo = fs::canonicalize(repo)?;
    let top = git(&repo, &["rev-parse", "--show-toplevel"])?;
    if Path::new(top.trim_end_matches('\n')) != repo {
        return Err("repository_root_required".into());
    }
    let raw = git(
        &repo,
        &[
            "config",
            "--local",
            "--no-includes",
            "--null",
            "--get-regexp",
            "^remote\\..*\\.url$",
        ],
    )?;
    let mut candidates = std::collections::BTreeSet::new();
    for record in raw.split('\0').filter(|s| !s.is_empty()) {
        let (key, value) = record.split_once('\n').ok_or("invalid_remote")?;
        let name = key
            .strip_prefix("remote.")
            .and_then(|s| s.strip_suffix(".url"))
            .ok_or("invalid_remote")?;
        if remote.is_none_or(|r| r == name) {
            candidates.insert(github_remote(value)?);
        }
    }
    if candidates.len() != 1 {
        return Err("choose_remote".into());
    }
    Ok((
        repo.to_str().ok_or("invalid_repository")?.to_string(),
        candidates.into_iter().next().ok_or("choose_remote")?,
    ))
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
    if frozen.iter().any(|key| config.get(*key) != saved.get(*key))
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

pub struct Options<'a> {
    pub root: &'a Path,
    pub repository: Option<&'a Path>,
    pub remote: Option<&'a str>,
    pub origin: &'a str,
    pub trust_origin: bool,
    pub browser: bool,
    pub new_pair: bool,
    pub record_orchestrated_outside_repos: bool,
    pub next_steps: bool,
}
enum Target {
    Repository { path: String, reference: String },
    Installation { record_outside: bool },
}
impl Target {
    fn new(o: &Options<'_>) -> Result<Self> {
        match o.repository {
            Some(_) if o.record_orchestrated_outside_repos => Err("invalid_command".into()),
            Some(path) => {
                let (path, reference) = repository(path, o.remote)?;
                Ok(Self::Repository { path, reference })
            }
            None if o.remote.is_some() => Err("invalid_command".into()),
            None => Ok(Self::Installation {
                record_outside: o.record_orchestrated_outside_repos,
            }),
        }
    }
    fn installation_record(&self, origin: &str, remote: Option<&str>) -> serde_json::Value {
        let id = Uuid::new_v4().to_string();
        match self {
            Self::Repository { path, reference } => {
                json!({"installation_id":id, "origin":origin, "repository":path, "repository_ref":reference, "remote":remote})
            }
            Self::Installation { record_outside } => {
                json!({"installation_id":id, "origin":origin, "scope_kind":"installation", "record_orchestrated_outside_repos":record_outside})
            }
        }
    }
    fn matches(&self, saved: &Object, origin: &str) -> bool {
        let same = match self {
            Self::Repository { path, reference } => {
                saved.len() == 5
                    && saved["repository"] == *path
                    && saved["repository_ref"] == *reference
            }
            Self::Installation { record_outside } => {
                saved.len() == 4
                    && saved.get("scope_kind") == Some(&json!("installation"))
                    && saved.get("record_orchestrated_outside_repos")
                        == Some(&json!(record_outside))
            }
        };
        same && saved.get("origin") == Some(&json!(origin))
    }
    fn start(&self, installation_id: &serde_json::Value) -> serde_json::Value {
        let mut body =
            json!({"schema_version":1,"installation_id":installation_id,"runtime":"claude-code"});
        if let Self::Installation { .. } = self {
            body["scope_kind"] = json!("installation");
        }
        body
    }
    fn selection(&self) -> String {
        match self {
            Self::Repository { reference, .. } => format!("Select repository: {reference}"),
            Self::Installation { .. } => {
                "Select: All active repositories (this machine's installation)".into()
            }
        }
    }
    fn approved(
        &self,
        c: &Object,
        installation: &Object,
        origin: &str,
    ) -> Result<serde_json::Value> {
        let keys = match self {
            Self::Repository { .. } => 6,
            Self::Installation { .. } => 9,
        };
        if c.len() != keys
            || c.get("installation_id") != installation.get("installation_id")
            || c.get("runtime") != Some(&json!("claude-code"))
            || c.get("endpoint_path") != Some(&json!("/ingest/v1/observations"))
            || !secret(text(c, "token")?, "dc_live_")
            || !text(c, "collector_ref")?
                .strip_prefix("urn:devdiary:collector:")
                .is_some_and(crate::uuid)
        {
            return Err("approved_scope_mismatch".into());
        }
        let endpoint = format!("{origin}/ingest/v1/observations");
        let common = json!({"endpoint":endpoint,"collector_ref":c["collector_ref"],"installation_id":installation["installation_id"]});
        let (config, refs) = match self {
            Self::Repository { path, reference } => {
                if c.get("repository_ref") != Some(&json!(reference)) {
                    return Err("approved_scope_mismatch".into());
                }
                let mut config = common;
                config["repository"] = json!(path);
                config["repository_ref"] = json!(reference);
                (config, None)
            }
            Self::Installation { record_outside } => {
                let versions =
                    crate::store::accepted_versions(c.get("observation_schema_versions"))
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
                if *record_outside && !v2 {
                    eprintln!(
                        "Server does not accept observation schema v2: --record-orchestrated-outside-repos is not saved."
                    );
                }
                config["observation_schema_versions"] = if v2 { json!([1, 2]) } else { json!([1]) };
                config["record_orchestrated_outside_repos"] = json!(*record_outside && v2);
                (config, Some(refs))
            }
        };
        scope(&serde_json::to_vec(&config)?)?;
        let mut saved = json!({"scope":config,"token":c["token"]});
        if let Some(refs) = refs {
            saved["repository_refs"] = json!(refs);
        }
        Ok(saved)
    }
}
pub fn setup(o: Options<'_>) -> Result<()> {
    let origin = origin(o.origin, o.trust_origin)?;
    let target = Target::new(&o)?;
    directory(o.root)?;
    let lock_path = o.root.join("setup.lock");
    if !lock_path.exists() {
        save_new(&lock_path, b"")?;
    }
    private(&lock_path, false)?;
    let lock = OpenOptions::new().read(true).write(true).open(lock_path)?;
    lock.try_lock()?;
    let installation_path = o.root.join("installation.json");
    if !installation_path.exists() {
        // Never adopt a preexisting outbox/connection into a new installation.
        if fs::read_dir(o.root)?.count() != 1 {
            return Err("new_private_directory_required".into());
        }
        save_new(
            &installation_path,
            &serde_json::to_vec(&target.installation_record(&origin, o.remote))?,
        )?;
    }
    let installation = object(&read(&installation_path)?)?;
    if !target.matches(&installation, &origin)
        || !crate::uuid(text(&installation, "installation_id")?)
    {
        return Err("frozen_scope_conflict".into());
    }
    if o.root.join("connection.json").exists() {
        if o.new_pair {
            return Err("existing_connection_use_new_directory".into());
        }
        initialize(o.root, &connection(o.root)?)?;
        println!("Connection already saved; no new pairing or hook changes.");
        return Ok(());
    }
    let attempt = o.root.join("pairing-attempted");
    let cooldown = o.root.join("start-retry-after");
    if cooldown.exists() {
        let until: u64 = std::str::from_utf8(&read(&cooldown)?)?.parse()?;
        let now = SystemTime::now().duration_since(UNIX_EPOCH)?.as_secs();
        if now < until {
            return Err("start_rate_limited".into());
        }
        fs::remove_file(&cooldown)?;
        File::open(o.root)?.sync_all()?;
    }
    if attempt.exists() {
        private(&attempt, false)?;
        if !o.new_pair {
            return Err("repair_new_pair".into());
        }
    } else {
        save_new(&attempt, b"A pairing may have been consumed. Revoke unused connections in the browser before --new-pair.\n")?;
    }
    let client = reqwest::blocking::Client::builder()
        .no_proxy()
        .retry(reqwest::retry::never())
        .redirect(reqwest::redirect::Policy::none())
        .connect_timeout(Duration::from_secs(4))
        .timeout(Duration::from_secs(5))
        .build()?;
    let r = client
        .post(format!("{origin}{START}"))
        .header("Content-Type", "application/json")
        .body(serde_json::to_vec(
            &target.start(&installation["installation_id"]),
        )?)
        .send()?;
    if r.status().as_u16() == 429 {
        let seconds = r
            .headers()
            .get("Retry-After")
            .and_then(|v| v.to_str().ok())
            .and_then(|v| v.parse::<u64>().ok())
            .filter(|n| *n <= 86400)
            .ok_or("invalid_retry_after")?;
        let until = SystemTime::now().duration_since(UNIX_EPOCH)?.as_secs() + seconds.max(5);
        save_new(&cooldown, until.to_string().as_bytes())?;
        eprintln!(
            "Pairing rate limited: wait at least {} seconds before --new-pair.",
            seconds.max(5)
        );
        return Err("start_rate_limited".into());
    }
    if r.status().as_u16() != 201 {
        return Err("pairing_unavailable".into());
    }
    let start = response(r)?;
    let bearer = text(&start, "pairing_token")?;
    let code = text(&start, "user_code")?;
    let interval = start
        .get("interval")
        .and_then(|v| v.as_u64())
        .filter(|n| (5..=600).contains(n))
        .ok_or("invalid_pairing_response")?;
    let expiry = chrono::DateTime::parse_from_rfc3339(text(&start, "expires_at")?)?.timestamp();
    let now = SystemTime::now().duration_since(UNIX_EPOCH)?.as_secs() as i64;
    if start.len() != 6
        || start["schema_version"] != json!(1)
        || !secret(bearer, "dp_pair_")
        || code.len() != 32
        || !code
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
        || expiry <= now
        || expiry > now + 600
    {
        return Err("invalid_pairing_response".into());
    }
    let verify = verification_path(&start["verification_path"])?;
    let deadline = Instant::now() + Duration::from_secs((expiry - now) as u64);
    println!(
        "Approve at {origin}{verify}\nOne-time code: {code}\n{}\nBrowser approval does not install hooks or establish host trust.\nIf interrupted or response is lost: revoke the unused connection in the browser, then repeat with --new-pair. Never reuse a different scope's outbox.",
        target.selection()
    );
    std::io::stdout().flush()?;
    if o.browser {
        // The constant URL is the only argument. No bearer/code in child argv/env.
        let _ = Command::new("/usr/bin/xdg-open")
            .arg(format!("{origin}{verify}"))
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn();
    }
    let mut delay = interval;
    loop {
        if Instant::now() + Duration::from_secs(delay) >= deadline {
            return Err("pairing_expired".into());
        }
        std::thread::sleep(Duration::from_secs(delay));
        let r = client
            .post(format!("{origin}{EXCHANGE}"))
            .bearer_auth(bearer)
            .header("Content-Type", "application/json")
            .body("{}")
            .send()
            .map_err(|_| "repair_new_pair")?;
        match r.status().as_u16() {
            429 => {
                delay = r
                    .headers()
                    .get("Retry-After")
                    .and_then(|v| v.to_str().ok())
                    .and_then(|v| v.parse::<u64>().ok())
                    .filter(|n| *n <= 3600)
                    .ok_or("invalid_retry_after")?
                    .max(interval);
                continue;
            }
            410 | 401 => return Err("pairing_terminal_new_pair".into()),
            200 => {}
            _ => return Err("repair_new_pair".into()),
        }
        delay = interval;
        let doc = response(r).map_err(|_| "repair_new_pair")?;
        if doc.get("schema_version") != Some(&json!(1)) {
            return Err("invalid_pairing_response".into());
        }
        match text(&doc, "status")? {
            "pending" if doc.len() == 2 => continue,
            "consumed" if doc.len() == 3 => {}
            _ => return Err("repair_new_pair".into()),
        }
        let c = doc
            .get("connection")
            .and_then(|v| v.as_object())
            .ok_or("repair_new_pair")?;
        let saved = target.approved(c, &installation, &origin)?;
        save_new(
            &o.root.join("connection.json"),
            &serde_json::to_vec(&saved)?,
        )
        .map_err(|_| "repair_new_pair")?;
        // If interrupted here, setup reuses the committed connection, never exchanges again.
        initialize(o.root, saved.as_object().ok_or("invalid_connection")?)?;
        if !o.next_steps {
            println!("Connection saved privately.");
            return Ok(());
        }
        println!(
            "Connection saved privately. Hooks remain unchanged. Next: setup-plan CONNECTION_DIR SETTINGS NEW_PLAN, review the plan, then claude-apply NEW_PLAN --consent. If a control plane starts agents here, after the first connection-sync run orchestrators approve CONNECTION_DIR in a terminal. Host trust and managed policy remain controlled by Claude; runtime coverage is unverified."
        );
        return Ok(());
    }
}
// Only a paired outbox has a revocation marker. A plain state's parent is an
// arbitrary user directory whose entries say nothing about this connection.
pub(crate) fn revoked(state: &Path) -> bool {
    state.file_name().is_some_and(|name| name == "outbox")
        && state.parent().is_some_and(|root| {
            root.join("connection.json").is_file() && root.join("revoked").exists()
        })
}
pub fn plan(root: &Path, settings: &Path, plan: &Path) -> Result<()> {
    plan_with(root, settings, plan, &std::env::current_exe()?)?;
    println!(
        "Plan saved, settings unchanged. Metadata-only Claude lifecycle/tool IDs; no prompts, transcripts or tool contents; unknown actor. Review NEW_PLAN before claude-apply NEW_PLAN --consent. Browser approval is not local hook consent or host trust. Managed policy may block hooks; no policy-absence claim is made."
    );
    Ok(())
}
pub fn plan_with(root: &Path, settings: &Path, plan: &Path, exe: &Path) -> Result<()> {
    let doc = connection(root)?;
    let config = doc["scope"].as_object().ok_or("invalid_connection")?;
    // Revalidate current local remotes at registration, not only at approval.
    if !crate::installation(config) {
        let installation = object(&read(&root.join("installation.json"))?)?;
        let choice = installation.get("remote").and_then(|v| v.as_str());
        let (_, remote) = repository(Path::new(text(config, "repository")?), choice)?;
        if remote != text(config, "repository_ref")? {
            return Err("approved_scope_mismatch".into());
        }
    }
    initialize(root, &doc)?;
    crate::claude::plan_for(&root.join("outbox"), settings, plan, exe)
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
pub fn health(root: &Path, plan: Option<&Path>) -> Result<()> {
    let doc = connection(root)?;
    initialize(root, &doc)?;
    let store = Store::open(&root.join("outbox"), None)?;
    let (pending, delivered) = store.counts()?;
    let drops = crate::drops::counts(&store, &root.join("outbox"))?;
    let registration = match plan {
        Some(path) => json!(crate::claude::registration_status(
            path,
            &root.join("outbox")
        )?),
        None => json!({"registration":"unknown; provide PLAN for local inspection"}),
    };
    let refs = crate::store::read_refs(&root.join("outbox"))
        .map(|r| r.len())
        .ok();
    let orchestrators = crate::orchestrators::summary(&root.join("outbox"));
    println!(
        "{}",
        json!({"connection":"saved", "local_repository_refs":refs, "local_orchestrators":orchestrators, "local_observed":pending+delivered,"local_pending":pending,"local_delivered_with_validated_receipt":delivered,"local_drops":drops,"local_revoked":root.join("revoked").exists(),"local_scope_refresh_error":store.scope_refresh_failure()?,"local_registration":registration, "host_trust":"unknown", "managed_policy":"unknown", "runtime_qualification":"unverified", "server_current_status":"unknown; inspect browser connection page"})
    );
    Ok(())
}
pub const SETUP_INCOMPLETE: &str = "Setup incomplete. No secret is recoverable from a consumed exchange. Inspect/revoke unused browser connections before --new-pair. Existing saved credentials/outboxes are never retargeted.";
pub fn diagnostic(error: &dyn std::error::Error) -> &'static str {
    known_diagnostic(error).unwrap_or(SETUP_INCOMPLETE)
}
pub fn known_diagnostic(error: &dyn std::error::Error) -> Option<&'static str> {
    guidance(&error.to_string())
}
pub fn guidance(code: &str) -> Option<&'static str> {
    Some(match code {
        "choose_remote" => {
            "Multiple or missing GitHub remotes: choose explicitly with --remote NAME."
        }
        "trust_origin_required" | "invalid_origin" => {
            "Use the default HTTPS origin or an explicit --origin ORIGIN --trust-origin (HTTP only for numeric loopback)."
        }
        "frozen_scope_conflict" | "existing_connection_use_new_directory" => {
            "Existing installation/scope cannot be retargeted. Preserve its outbox; use a new private connection directory."
        }
        "start_rate_limited" => {
            "Pairing starts are rate limited. The saved Retry-After cooldown must expire before an explicit --new-pair retry."
        }
        "pairing_unavailable" => {
            "Pairing endpoint unavailable. Requires server PR #571 deployed; do not assume this backend is enabled."
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
            "The server revoked this connection. Capture and sync have stopped; pair a new connection directory."
        }
        "sync_rate_limited" => {
            "The server asked the collector to wait. Sync resumes after the saved Retry-After time."
        }
        "repair_required" => {
            "This outbox uses an older storage format. Run: repair CONNECTION_DIR/outbox"
        }
        "python_observer_registered" => {
            "These settings already register the Python observer hook. Remove it before registering this collector."
        }
        "unsafe_settings" => {
            "Settings and their directory must be owned by you and not writable by group or others, with no symlinks or hardlinks."
        }
        "approved_scope_mismatch" => {
            "Approved scope does not match this installation and local repository. Revoke the unused browser connection, then --new-pair; hooks unchanged."
        }
        _ => return None,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
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
    fn verification_page_is_one_of_the_known_paths() {
        for path in ["/settings/collector", "/phoenix/settings/collector"] {
            assert_eq!(verification_path(&json!(path)).unwrap(), path);
        }
        for path in [
            json!("/settings/collector?x=1"),
            json!("https://evil.test/settings/collector"),
            json!("/settings/collectors"),
            json!(null),
        ] {
            assert!(verification_path(&path).is_err(), "{path}");
        }
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
