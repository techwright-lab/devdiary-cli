use devdiary_collector::{
    Object, freeze, object,
    store::{Store, repair, sync},
};
use rusqlite::{Connection, params};
use std::{
    collections::{BTreeMap, HashSet},
    io::{BufRead, BufReader, Read, Write},
    net::{TcpListener, TcpStream},
    os::unix::fs::PermissionsExt,
    path::{Path, PathBuf},
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, Ordering},
    },
};

#[derive(Clone, Default)]
struct FakeServer {
    port: u16,
    rejected: Arc<Mutex<HashSet<String>>>,
    bodies: Arc<Mutex<Vec<Vec<u8>>>>,
    lose_responses: Arc<AtomicBool>,
}
impl FakeServer {
    fn start() -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let server = Self {
            port: listener.local_addr().unwrap().port(),
            ..Self::default()
        };
        let handle = server.clone();
        std::thread::spawn(move || {
            for stream in listener.incoming().flatten() {
                let handle = handle.clone();
                std::thread::spawn(move || handle.serve(stream));
            }
        });
        server
    }
    fn serve(&self, mut stream: TcpStream) {
        stream.set_nodelay(true).unwrap();
        let mut reader = BufReader::new(stream.try_clone().unwrap());
        loop {
            let mut line = String::new();
            if reader.read_line(&mut line).unwrap_or(0) == 0 {
                return;
            }
            let mut length = 0;
            loop {
                line.clear();
                reader.read_line(&mut line).unwrap();
                if line == "\r\n" {
                    break;
                }
                if let Some(value) = line.to_ascii_lowercase().strip_prefix("content-length:") {
                    length = value.trim().parse().unwrap();
                }
            }
            let mut body = vec![0; length];
            reader.read_exact(&mut body).unwrap();
            self.bodies.lock().unwrap().push(body.clone());
            if self.lose_responses.load(Ordering::SeqCst) {
                return;
            }
            let sent = object(&body).unwrap();
            let id = sent["observation_id"].as_str().unwrap();
            let (status, reply) = if self.rejected.lock().unwrap().contains(id) {
                (422, "{}".to_string())
            } else {
                let receipt = serde_json::json!({"collector_ref": "collector-fixture",
                    "observation_id": id, "installation_id": sent["installation_id"], "record_id": 7});
                (201, receipt.to_string())
            };
            let response = format!(
                "HTTP/1.1 {status} Fixture\r\nContent-Type: application/json\r\nContent-Length: {}\r\n\r\n{reply}",
                reply.len()
            );
            stream.write_all(response.as_bytes()).unwrap();
        }
    }
}

struct Fixture {
    _dir: tempfile::TempDir,
    state: PathBuf,
    key: PathBuf,
}
fn fixture(port: u16) -> Fixture {
    let dir = tempfile::tempdir().unwrap();
    let state = dir.path().join("state");
    std::fs::create_dir(&state).unwrap();
    std::fs::set_permissions(&state, std::fs::Permissions::from_mode(0o700)).unwrap();
    let key = dir.path().join("key");
    std::fs::write(&key, "dc_live_SYNTHETIC_ONLY").unwrap();
    std::fs::set_permissions(&key, std::fs::Permissions::from_mode(0o600)).unwrap();
    Store::open(&state, Some(&scope(port))).unwrap();
    Fixture {
        _dir: dir,
        state,
        key,
    }
}
fn scope(port: u16) -> Vec<u8> {
    let mut config = object(include_bytes!("fixtures/scope.json")).unwrap();
    config.insert(
        "endpoint".into(),
        format!("http://127.0.0.1:{port}/ingest/v1/observations").into(),
    );
    serde_json::to_vec(&config).unwrap()
}
fn id(n: usize) -> String {
    format!("{n:08}-2222-4222-8222-222222222222")
}
fn observation(n: usize) -> String {
    include_str!("fixtures/local-observation.json")
        .replace("22222222-2222-4222-8222-222222222222", &id(n))
}
fn payload(state: &Path, observation_id: &str) -> Option<Vec<u8>> {
    Store::open(state, None)
        .unwrap()
        .db
        .query_row(
            "SELECT payload FROM outbox WHERE observation_id=?",
            [observation_id],
            |r| r.get(0),
        )
        .ok()
}

#[test]
fn concurrent_writers() {
    let f = fixture(9999);
    std::thread::scope(|threads| {
        let handles: Vec<_> = (0..8)
            .map(|i| {
                let path = f.state.as_path();
                threads.spawn(move || {
                    Store::open(path, None)
                        .and_then(|mut s| s.collect(observation(i).as_bytes()))
                        .map_err(|e| e.to_string())
                })
            })
            .collect();
        for h in handles {
            h.join().unwrap().unwrap();
        }
    });
    assert_eq!(
        Store::open(&f.state, None).unwrap().counts().unwrap(),
        (8, 0)
    );
}

#[test]
fn delivered_rows_are_reclaimed_while_pending_rows_are_kept() {
    let server = FakeServer::start();
    let f = fixture(server.port);
    let mut held = BTreeMap::new();
    for cycle in 0..50 {
        let mut store = Store::open(&f.state, None).unwrap();
        for n in cycle * 100..(cycle + 1) * 100 {
            store.collect(observation(n).as_bytes()).unwrap();
        }
        drop(store);
        let rejected = id(cycle * 100);
        server.rejected.lock().unwrap().insert(rejected.clone());
        held.insert(rejected.clone(), payload(&f.state, &rejected).unwrap());
        let mut drained = false;
        for _ in 0..5 {
            let (pending, _) = sync(&f.state, &f.key, 100).unwrap();
            if pending as usize == held.len() {
                drained = true;
                break;
            }
        }
        assert!(drained, "cycle {cycle} did not drain");
        for (observation_id, bytes) in &held {
            assert_eq!(payload(&f.state, observation_id).as_ref(), Some(bytes));
        }
    }
    server.rejected.lock().unwrap().clear();
    for _ in 0..5 {
        sync(&f.state, &f.key, 100).unwrap();
    }
    assert_eq!(
        Store::open(&f.state, None).unwrap().counts().unwrap(),
        (0, 5000)
    );
}

#[test]
fn lost_receipt_keeps_the_row_pending_and_retries_identical_bytes() {
    let server = FakeServer::start();
    let f = fixture(server.port);
    Store::open(&f.state, None)
        .unwrap()
        .collect(observation(1).as_bytes())
        .unwrap();
    server.lose_responses.store(true, Ordering::SeqCst);
    assert_eq!(sync(&f.state, &f.key, 1).unwrap(), (1, 0));
    assert!(payload(&f.state, &id(1)).is_some());
    server.lose_responses.store(false, Ordering::SeqCst);
    assert_eq!(sync(&f.state, &f.key, 1).unwrap(), (0, 1));
    let bodies = server.bodies.lock().unwrap();
    assert_eq!(bodies.len(), 2);
    assert_eq!(bodies[0], bodies[1]);
}

fn old_format_store(state: &Path, config: &Object) -> Vec<Vec<u8>> {
    let path = state.join("collector-rust-v1.sqlite3");
    let db = Connection::open(&path).unwrap();
    db.execute_batch("PRAGMA application_id=1145328177; PRAGMA user_version=1;
        CREATE TABLE scope (singleton INTEGER PRIMARY KEY CHECK(singleton=1), json BLOB NOT NULL, cursor INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE outbox (seq INTEGER PRIMARY KEY, observation_id TEXT UNIQUE NOT NULL, payload BLOB NOT NULL, delivered INTEGER NOT NULL DEFAULT 0, receipt BLOB, failure TEXT);
        CREATE INDEX pending ON outbox(delivered, seq);").unwrap();
    db.execute(
        "INSERT INTO scope(singleton,json) VALUES(1,?)",
        [serde_json::to_vec(config).unwrap()],
    )
    .unwrap();
    let payloads: Vec<_> = (0..3)
        .map(|n| freeze(observation(n).as_bytes(), config).unwrap())
        .collect();
    for (n, bytes) in payloads.iter().enumerate() {
        db.execute(
            "INSERT INTO outbox(observation_id,payload,delivered,receipt) VALUES(?,?,?,?)",
            params![
                id(n),
                bytes,
                (n < 2) as i64,
                (n < 2).then_some(b"{}".to_vec())
            ],
        )
        .unwrap();
    }
    drop(db);
    std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600)).unwrap();
    payloads
}

#[test]
fn old_format_store_requires_repair_then_keeps_pending_rows() {
    let dir = tempfile::tempdir().unwrap();
    let state = dir.path().join("state");
    std::fs::create_dir(&state).unwrap();
    std::fs::set_permissions(&state, std::fs::Permissions::from_mode(0o700)).unwrap();
    let config = object(&scope(9999)).unwrap();
    let payloads = old_format_store(&state, &config);
    let refused = Store::open(&state, None).err().unwrap().to_string();
    assert_eq!(refused, "repair_required");
    repair(&state).unwrap();
    repair(&state).unwrap();
    let mut store = Store::open(&state, None).unwrap();
    assert_eq!(store.counts().unwrap(), (1, 2));
    let vacuum: i64 = store
        .db
        .query_row("PRAGMA auto_vacuum", [], |r| r.get(0))
        .unwrap();
    assert_eq!(vacuum, 2);
    assert_eq!(payload(&state, &id(2)), Some(payloads[2].clone()));
    store.collect(observation(0).as_bytes()).unwrap();
    assert_eq!(store.counts().unwrap(), (1, 2));
}
