use serde_json::{Value, json};
use std::{
    fs,
    io::{Read, Write},
    net::TcpListener,
    os::unix::fs::PermissionsExt,
    process::Command,
};

fn refresh(status: u16, body: Value, accepted: bool) {
    refresh_raw(status, body.to_string(), accepted);
}
fn refresh_raw(status: u16, raw: String, accepted: bool) {
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
        socket.read(&mut request).unwrap();
        write!(
            socket,
            "HTTP/1.1 {status} Test\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{raw}",
            raw.len()
        )
        .unwrap();
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
    assert_eq!(output.status.success(), accepted, "{error}");
    assert!(!error.contains("dc_live_secret_fixture"));
    assert!(!error.contains("response-secret"));
    let stdout = String::from_utf8_lossy(&output.stdout);
    assert!(!stdout.contains("dc_live_secret_fixture"));
    assert!(!stdout.contains("response-secret"));
    if accepted {
        assert_eq!(
            fs::read_to_string(refs).unwrap(),
            "[\"https://github.com/acme/new\"]"
        );
    } else {
        assert!(!error.trim().is_empty());
        assert_eq!(
            fs::read_to_string(refs).unwrap(),
            "[\"https://github.com/acme/old\"]"
        );
    }
}
fn payload() -> Value {
    json!({"schema_version":1,"collector_ref":"collector-fixture","scope_kind":"installation",
        "repository_refs":["https://github.com/acme/new"],"observation_schema_versions":[1,2]})
}
#[test]
fn scope_compatibility_legacy() {
    refresh(200, payload(), true);
}
#[test]
fn scope_compatibility_empty_orchestrators() {
    let mut p = payload();
    p["orchestrators"] = json!([]);
    refresh(200, p, true);
}
fn preset() -> Value {
    json!({"key":"paperclip","env":{"agent_ref":"PAPERCLIP_AGENT_ID", "run_ref":"PAPERCLIP_RUN_ID", "task_ref":"PAPERCLIP_TASK_ID", "checkout_path":["PAPERCLIP_WORKSPACE_CWD"]}})
}
#[test]
fn scope_compatibility_nonempty_orchestrators() {
    let mut p = payload();
    p["orchestrators"] = json!([preset()]);
    refresh(200, p, true);
}
#[test]
fn scope_compatibility_malformed_orchestrators() {
    for invalid in [
        json!(null),
        json!({}),
        json!([{}]),
        json!([preset(), preset()]),
        json!([{"key":"paperclip","env":{"agent_ref":42}}]),
        json!([{"key":"paperclip","env":{"agent_ref":"invalid-name"}}]),
        json!([{"key":"paperclip","env":{"unexpected":"NAME"}}]),
    ] {
        let mut p = payload();
        p["orchestrators"] = invalid;
        refresh(200, p, false);
    }
}
#[test]
fn scope_compatibility_identity_mismatch() {
    for (field, value) in [
        ("collector_ref", json!("other-collector")),
        ("scope_kind", json!("repository")),
        ("schema_version", json!(2)),
        ("repository_refs", json!(["invalid-repository"])),
        ("observation_schema_versions", json!(["1"])),
    ] {
        let mut p = payload();
        p[field] = value;
        refresh(200, p, false);
    }
    let mut p = payload();
    p["workspace_id"] = json!("other-workspace");
    refresh(200, p, false);
}
#[test]
fn scope_compatibility_http_refusal() {
    for status in [401, 403, 422, 429, 500, 302] {
        refresh(status, json!({"error":"response-secret"}), false);
    }
}
#[test]
fn scope_compatibility_malformed_refresh_response() {
    refresh(200, json!({"error":"response-secret"}), false);
    refresh_raw(200, "{response-secret".into(), false);
    let duplicate =
        payload()
            .to_string()
            .replacen("{", "{\"collector_ref\":\"response-secret\",", 1);
    refresh_raw(200, duplicate, false);
}
