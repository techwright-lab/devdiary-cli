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
const VERIFY: &str = "/phoenix/settings/collector";
const START: &str = "/ingest/v1/collector_pairings";
const EXCHANGE: &str = "/ingest/v1/collector_pairings/exchange";
pub const DEFAULT_ORIGIN: &str = "https://devdiary.me";

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
    r.take(16385).read_to_end(&mut raw)?;
    if raw.len() > 16384 {
        return Err("invalid_pairing_response".into());
    }
    serde_json::from_slice::<crate::claude::UniqueKeys>(&raw)?;
    object(&raw)
}
fn connection(root: &Path) -> Result<Object> {
    private(root, true)?;
    let doc = object(&read(&root.join("connection.json"))?)?;
    if doc.len() != 2 || !secret(text(&doc, "token")?, "dc_live_") {
        return Err("invalid_connection".into());
    }
    let config = scope(&serde_json::to_vec(
        doc.get("scope").ok_or("invalid_connection")?,
    )?)?;
    let installation = object(&read(&root.join("installation.json"))?)?;
    for key in ["installation_id", "repository", "repository_ref"] {
        if config.get(key) != installation.get(key) {
            return Err("frozen_scope_conflict".into());
        }
    }
    if text(&config, "endpoint")?
        != format!("{}/ingest/v1/observations", text(&installation, "origin")?)
    {
        return Err("frozen_scope_conflict".into());
    }
    Ok(doc)
}
fn initialize(root: &Path, doc: &Object) -> Result<()> {
    directory(&root.join("outbox"))?;
    Store::open(
        &root.join("outbox"),
        Some(&serde_json::to_vec(&doc["scope"])?),
    )?;
    Ok(())
}

pub struct Options<'a> {
    pub root: &'a Path,
    pub repository: &'a Path,
    pub remote: Option<&'a str>,
    pub origin: &'a str,
    pub trust_origin: bool,
    pub browser: bool,
    pub new_pair: bool,
}
pub fn setup(o: Options<'_>) -> Result<()> {
    let origin = origin(o.origin, o.trust_origin)?;
    let (repo, repo_ref) = repository(o.repository, o.remote)?;
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
            &serde_json::to_vec(
                &json!({"installation_id":Uuid::new_v4().to_string(), "origin":origin, "repository":repo, "repository_ref":repo_ref, "remote":o.remote}),
            )?,
        )?;
    }
    let installation = object(&read(&installation_path)?)?;
    if installation.len() != 5
        || installation["origin"] != origin
        || installation["repository"] != repo
        || installation["repository_ref"] != repo_ref
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
    let r = client.post(format!("{origin}{START}")).header("Content-Type", "application/json").body(serde_json::to_vec(&json!({"schema_version":1,"installation_id":installation["installation_id"],"runtime":"claude-code"}))?).send()?;
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
        || start["verification_path"] != VERIFY
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
    let deadline = Instant::now() + Duration::from_secs((expiry - now) as u64);
    println!(
        "Approve at {origin}{VERIFY}\nOne-time code: {code}\nSelect repository: {repo_ref}\nBrowser approval does not install hooks or establish host trust.\nIf interrupted or response is lost: revoke the unused connection in the browser, then repeat with --new-pair. Never reuse a different scope's outbox."
    );
    std::io::stdout().flush()?;
    if o.browser {
        // The constant URL is the only argument. No bearer/code in child argv/env.
        let _ = Command::new("/usr/bin/xdg-open")
            .arg(format!("{origin}{VERIFY}"))
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
        if c.len() != 6
            || c.get("installation_id") != installation.get("installation_id")
            || c.get("repository_ref") != Some(&json!(repo_ref))
            || c.get("runtime") != Some(&json!("claude-code"))
            || c.get("endpoint_path") != Some(&json!("/ingest/v1/observations"))
            || !secret(text(c, "token")?, "dc_live_")
            || !text(c, "collector_ref")?
                .strip_prefix("urn:devdiary:collector:")
                .is_some_and(crate::uuid)
        {
            return Err("approved_scope_mismatch".into());
        }
        let config = json!({"endpoint":format!("{origin}/ingest/v1/observations"),"collector_ref":c["collector_ref"],"installation_id":installation["installation_id"],"repository":repo,"repository_ref":repo_ref});
        scope(&serde_json::to_vec(&config)?)?;
        let saved = json!({"scope":config,"token":c["token"]});
        save_new(
            &o.root.join("connection.json"),
            &serde_json::to_vec(&saved)?,
        )
        .map_err(|_| "repair_new_pair")?;
        // If interrupted here, setup reuses the committed connection, never exchanges again.
        initialize(o.root, saved.as_object().ok_or("invalid_connection")?)?;
        println!(
            "Connection saved privately. Hooks remain unchanged. Next: setup-plan CONNECTION_DIR SETTINGS NEW_PLAN, review the plan, then claude-apply NEW_PLAN --consent. Host trust and managed policy remain controlled by Claude; runtime coverage is unverified."
        );
        return Ok(());
    }
}
pub fn plan(root: &Path, settings: &Path, plan: &Path) -> Result<()> {
    let doc = connection(root)?;
    let config = doc["scope"].as_object().ok_or("invalid_connection")?;
    // Revalidate current local remotes at registration, not only at approval.
    let installation = object(&read(&root.join("installation.json"))?)?;
    let choice = installation.get("remote").and_then(|v| v.as_str());
    let (_, remote) = repository(Path::new(text(config, "repository")?), choice)?;
    if remote != text(config, "repository_ref")? {
        return Err("approved_scope_mismatch".into());
    }
    initialize(root, &doc)?;
    crate::claude::plan(&root.join("outbox"), settings, plan)?;
    println!(
        "Plan saved, settings unchanged. Metadata-only Claude lifecycle/tool IDs; no prompts, transcripts or tool contents; unknown actor. Review NEW_PLAN before claude-apply NEW_PLAN --consent. Browser approval is not local hook consent or host trust. Managed policy may block hooks; no policy-absence claim is made."
    );
    Ok(())
}
pub fn sync(root: &Path, limit: usize) -> Result<(i64, i64)> {
    let doc = connection(root)?;
    initialize(root, &doc)?;
    crate::store::sync_key(&root.join("outbox"), text(&doc, "token")?, limit)
}
pub fn health(root: &Path, plan: Option<&Path>) -> Result<()> {
    let doc = connection(root)?;
    initialize(root, &doc)?;
    let (pending, delivered) = Store::open(&root.join("outbox"), None)?.counts()?;
    let registration = match plan {
        Some(path) => json!(crate::claude::registration_status(
            path,
            &root.join("outbox")
        )?),
        None => json!({"registration":"unknown; provide PLAN for local inspection"}),
    };
    println!(
        "{}",
        json!({"connection":"saved", "local_observed":pending+delivered,"local_pending":pending,"local_delivered_with_validated_receipt":delivered,"local_registration":registration, "host_trust":"unknown", "managed_policy":"unknown", "runtime_qualification":"unverified", "server_current_status":"unknown; inspect browser connection page"})
    );
    Ok(())
}
pub fn diagnostic(error: &dyn std::error::Error) -> &'static str {
    match error.to_string().as_str() {
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
        "approved_scope_mismatch" => {
            "Approved scope does not match this installation and local repository. Revoke the unused browser connection, then --new-pair; hooks unchanged."
        }
        _ => {
            "Setup incomplete. No secret is recoverable from a consumed exchange. Inspect/revoke unused browser connections before --new-pair. Existing saved credentials/outboxes are never retargeted."
        }
    }
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
