use devdiary::{
    object,
    store::{Store, sync_report},
};
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
}
// macOS puts the temp folder under the `/var` symlink, which the
// symlink-ancestor rule refuses, so fixtures start from its canonical path.
fn tempdir() -> tempfile::TempDir {
    tempfile::tempdir_in(std::fs::canonicalize(std::env::temp_dir()).unwrap()).unwrap()
}
const TOKEN: &str = "dc_live_SYNTHETIC_ONLY";
fn sync(state: &Path, limit: usize) -> (i64, i64) {
    let report = sync_report(state, TOKEN, limit, None).unwrap();
    (report.pending, report.delivered)
}
fn fixture(port: u16) -> Fixture {
    let dir = tempdir();
    let state = dir.path().join("state");
    std::fs::create_dir(&state).unwrap();
    std::fs::set_permissions(&state, std::fs::Permissions::from_mode(0o700)).unwrap();
    Store::open(&state, Some(&scope(port))).unwrap();
    Fixture { _dir: dir, state }
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
            let (pending, _) = sync(&f.state, 100);
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
        sync(&f.state, 100);
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
    assert_eq!(sync(&f.state, 1), (1, 0));
    assert!(payload(&f.state, &id(1)).is_some());
    server.lose_responses.store(false, Ordering::SeqCst);
    assert_eq!(sync(&f.state, 1), (0, 1));
    let bodies = server.bodies.lock().unwrap();
    assert_eq!(bodies.len(), 2);
    assert_eq!(bodies[0], bodies[1]);
}
