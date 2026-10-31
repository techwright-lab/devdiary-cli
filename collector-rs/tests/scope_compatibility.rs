use serde_json::{Value, json};
use std::{
    fs,
    io::{Read, Write},
    net::TcpListener,
    os::unix::fs::PermissionsExt,
    process::Command,
};

enum Want {
    Applied,
    Kept(&'static str),
    Refused(Value),
    Stopped,
}
use Want::{Applied, Kept, Refused, Stopped};
const INVALID: Want = Kept("scope_refresh_invalid_response");
const OFFER_REFUSED: &str = "scope_refresh_offer_refused";
const EMPTY_OFFER: &str = r#"{"orchestrators":[],"refused":0}"#;
const NEW_REFS: &str = "[\"https://github.com/acme/new\"]";

fn refresh(status: u16, body: Value, want: Want) {
    refresh_raw(status, body.to_string(), want);
}
fn refresh_raw(status: u16, raw: String, want: Want) {
    let length = raw.len();
    refresh_wire(status, raw, want, length);
}
fn refresh_wire(status: u16, raw: String, want: Want, length: usize) {
    refresh_over(EMPTY_OFFER, status, raw, want, length);
}
fn refresh_saved(saved: &str, body: Value, want: Want) {
    let raw = body.to_string();
    let length = raw.len();
    refresh_over(saved, 200, raw, want, length);
}
fn refresh_over(saved: &str, status: u16, raw: String, want: Want, length: usize) {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let endpoint = format!(
        "http://{}/ingest/v1/observations",
        listener.local_addr().unwrap()
    );
    listener.set_nonblocking(true).unwrap();
    let server = std::thread::spawn(move || {
        let deadline = std::time::Instant::now() + std::time::Duration::from_secs(10);
        let mut socket = loop {
            match listener.accept() {
                Ok((socket, _)) => break socket,
                Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                    assert!(
                        std::time::Instant::now() < deadline,
                        "scope request missing"
                    );
                    std::thread::sleep(std::time::Duration::from_millis(5));
                }
                Err(e) => panic!("{e}"),
            }
        };
        socket
            .set_read_timeout(Some(std::time::Duration::from_secs(5)))
            .unwrap();
        let mut request = [0; 8192];
        let request_bytes = socket.read(&mut request).unwrap();
        let sent = String::from_utf8_lossy(&request[..request_bytes]).to_ascii_lowercase();
        assert!(sent.contains("\r\ndevdiary-scope-keys: accepts_declared_run_ref\r\n"));
        if status == 0 {
            return;
        }
        write!(
            socket,
            "HTTP/1.1 {status} Test\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{raw}",
            length
        )
        .ok();
    });
    let dir = tempfile::tempdir().unwrap();
    fs::set_permissions(dir.path(), fs::Permissions::from_mode(0o700)).unwrap();
    let config = json!({"collector_ref":"collector-fixture", "endpoint":endpoint,
        "installation_id":"11111111-1111-4111-8111-111111111111", "observation_schema_versions":[1,2],
        "record_orchestrated_outside_repos":false,"scope_kind":"installation"});
    devdiary_collector::store::Store::open(dir.path(), Some(&serde_json::to_vec(&config).unwrap()))
        .unwrap();
    let refs = dir.path().join("repository-refs.json");
    fs::write(&refs, b"[\"https://github.com/acme/old\"]").unwrap();
    fs::set_permissions(&refs, fs::Permissions::from_mode(0o600)).unwrap();
    let offered = dir.path().join("orchestrators-offered.json");
    fs::write(&offered, saved).unwrap();
    fs::set_permissions(&offered, fs::Permissions::from_mode(0o600)).unwrap();
    let approved = dir.path().join("orchestrators-approved.json");
    fs::write(&approved, b"[]").unwrap();
    fs::set_permissions(&approved, fs::Permissions::from_mode(0o600)).unwrap();
    let credentials = tempfile::tempdir().unwrap();
    fs::set_permissions(credentials.path(), fs::Permissions::from_mode(0o700)).unwrap();
    let key = credentials.path().join("key");
    fs::write(&key, b"dc_live_secret_fixture").unwrap();
    fs::set_permissions(&key, fs::Permissions::from_mode(0o600)).unwrap();
    let output = Command::new(env!("CARGO_BIN_EXE_devdiary-collector"))
        .arg("sync")
        .arg(dir.path())
        .arg(&key)
        .arg("1")
        .output()
        .unwrap();
    server.join().unwrap();
    let error = String::from_utf8_lossy(&output.stderr);
    assert_eq!(output.status.success(), !matches!(want, Stopped), "{error}");
    assert!(!error.contains("dc_live_secret_fixture"));
    assert!(!error.contains("response-secret"));
    let stdout = String::from_utf8_lossy(&output.stdout);
    assert!(!stdout.contains("dc_live_secret_fixture"));
    assert!(!stdout.contains("response-secret"));
    assert_eq!(fs::read(approved).unwrap(), b"[]");
    let reported = match want {
        Kept(code) => Some(code),
        Refused(_) => Some(OFFER_REFUSED),
        Applied | Stopped => None,
    };
    if let Some(code) = reported {
        let line: Value = serde_json::from_str(&stdout).unwrap();
        assert_eq!(stdout.lines().count(), 1);
        assert_eq!(line["scope_refresh_error"], code);
    }
    let refs_now = fs::read_to_string(refs).unwrap();
    let offer_now = fs::read_to_string(offered).unwrap();
    match want {
        Applied => {
            assert!(!stdout.contains("scope_refresh_error"));
            assert_eq!(refs_now, NEW_REFS);
        }
        Refused(offer) => {
            assert!(!error.trim().is_empty());
            assert_eq!(refs_now, NEW_REFS);
            assert_eq!(serde_json::from_str::<Value>(&offer_now).unwrap(), offer);
        }
        Kept(_) | Stopped => {
            assert!(!error.trim().is_empty());
            assert_eq!(offer_now, saved);
            assert_eq!(refs_now, "[\"https://github.com/acme/old\"]");
        }
    }
}
fn payload() -> Value {
    json!({"schema_version":1,"collector_ref":"collector-fixture","scope_kind":"installation",
        "repository_refs":["https://github.com/acme/new"],"observation_schema_versions":[1,2]})
}
#[test]
fn scope_compatibility_legacy() {
    refresh(200, payload(), Applied);
}
#[test]
fn scope_compatibility_empty_orchestrators() {
    let mut p = payload();
    p["orchestrators"] = json!([]);
    refresh(200, p, Applied);
}
fn preset() -> Value {
    json!({"key":"paperclip","env":{"agent_ref":"PAPERCLIP_AGENT_ID", "run_ref":"PAPERCLIP_RUN_ID", "task_ref":"PAPERCLIP_TASK_ID", "checkout_path":["PAPERCLIP_WORKSPACE_CWD"]}})
}
#[test]
fn scope_compatibility_nonempty_orchestrators() {
    let mut p = payload();
    p["orchestrators"] = json!([preset()]);
    refresh(200, p, Applied);
}
#[test]
fn scope_compatibility_malformed_orchestrators() {
    for (invalid, clean) in [
        (json!(null), json!([])),
        (json!({}), json!([])),
        (json!([{}]), json!([])),
        (json!([preset(), preset()]), json!([preset()])),
        (
            json!([{"key":"paperclip","env":{"agent_ref":42}}]),
            json!([]),
        ),
        (
            json!([{"key":"paperclip","env":{"agent_ref":"invalid-name"}}]),
            json!([]),
        ),
        (
            json!([{"key":"paperclip","env":{"unexpected":"NAME"}}]),
            json!([]),
        ),
    ] {
        let mut p = payload();
        p["orchestrators"] = invalid;
        refresh(200, p, Refused(json!({"orchestrators":clean,"refused":1})));
    }
}
fn refused_entry() -> Value {
    json!({"key":"evil","env":{"agent_ref":"AWS_SECRET_ACCESS_KEY"}})
}
#[test]
fn scope_compatibility_refused_entry_replaces_an_offer_with_no_entries() {
    for saved in [EMPTY_OFFER, r#"{"orchestrators":[],"refused":2}"#] {
        let mut p = payload();
        p["orchestrators"] = json!([preset(), refused_entry()]);
        let clean = json!({"orchestrators":[preset()],"refused":1});
        refresh_saved(saved, p, Refused(clean));
    }
}
#[test]
fn scope_compatibility_refused_entry_keeps_saved_entries_but_applies_refs() {
    let saved = json!({"orchestrators":[preset()],"refused":0});
    let mut other = preset();
    other["key"] = json!("cron");
    let mut p = payload();
    p["orchestrators"] = json!([other, refused_entry()]);
    refresh_saved(&saved.to_string(), p, Refused(saved));
}
#[test]
fn scope_compatibility_accepts_every_version_list_pairing_accepts() {
    for versions in [json!([1]), json!([1, 2]), json!([1, 2, 3]), json!([3, 1])] {
        let mut p = payload();
        p["observation_schema_versions"] = versions;
        refresh(200, p, Applied);
    }
}
#[test]
fn scope_compatibility_refuses_version_lists_pairing_refuses() {
    for versions in [
        json!(null),
        json!(1),
        json!([]),
        json!([2]),
        json!(["1"]),
        json!([1, "2"]),
        json!([1, -2]),
    ] {
        let mut p = payload();
        p["observation_schema_versions"] = versions;
        refresh(200, p, INVALID);
    }
}
#[test]
fn scope_compatibility_declared_run_answer() {
    for answer in [json!(true), json!(false)] {
        let mut p = payload();
        p["accepts_declared_run_ref"] = answer;
        refresh(200, p, Applied);
    }
}
#[test]
fn scope_compatibility_declared_run_answer_must_be_boolean() {
    for answer in [
        json!(null),
        json!("true"),
        json!(1),
        json!([true]),
        json!({}),
    ] {
        let mut p = payload();
        p["accepts_declared_run_ref"] = answer;
        refresh(200, p, INVALID);
    }
}
#[test]
fn scope_compatibility_identity_mismatch() {
    for (field, value) in [
        ("collector_ref", json!("other-collector")),
        ("scope_kind", json!("repository")),
        ("schema_version", json!(2)),
        ("repository_refs", json!(["invalid-repository"])),
    ] {
        let mut p = payload();
        p[field] = value;
        refresh(200, p, INVALID);
    }
    let mut p = payload();
    p["workspace_id"] = json!("other-workspace");
    refresh(200, p, INVALID);
}
#[test]
fn scope_compatibility_http_refusal() {
    for status in [422, 500, 302] {
        let body = json!({"error":"response-secret"});
        refresh(status, body, Kept("scope_refresh_http_failure"));
    }
}
#[test]
fn scope_compatibility_revocation_and_requested_wait_stop() {
    for status in [401, 403, 429] {
        refresh(status, json!({"error":"response-secret"}), Stopped);
    }
}
#[test]
fn scope_compatibility_malformed_refresh_response() {
    refresh(200, json!({"error":"response-secret"}), INVALID);
    refresh_raw(200, "{response-secret".into(), INVALID);
    let duplicate =
        payload()
            .to_string()
            .replacen("{", "{\"collector_ref\":\"response-secret\",", 1);
    refresh_raw(200, duplicate, INVALID);
}

#[test]
fn scope_compatibility_network_failure() {
    refresh_raw(0, String::new(), Kept("scope_refresh_network_failure"));
}
#[test]
fn scope_compatibility_oversized_response() {
    refresh_raw(
        200,
        "x".repeat(262145),
        Kept("scope_refresh_response_too_large"),
    );
}
#[test]
fn scope_compatibility_incomplete_response() {
    refresh_wire(
        200,
        "{response-secret".into(),
        Kept("scope_refresh_read_failure"),
        100,
    );
}
