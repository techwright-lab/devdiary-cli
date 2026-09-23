use devdiary_collector::store::Store;
use std::os::unix::fs::PermissionsExt;

#[test]
fn concurrent_writers() {
    let dir = tempfile::tempdir().unwrap();
    std::fs::set_permissions(dir.path(), std::fs::Permissions::from_mode(0o700)).unwrap();
    Store::open(dir.path(), Some(include_bytes!("fixtures/scope.json"))).unwrap();
    std::thread::scope(|threads| {
        let handles: Vec<_> = (0..8)
            .map(|i| {
                let path = dir.path();
                threads.spawn(move || {
                    let raw = include_str!("fixtures/local-observation.json")
                        .replace("22222222-2222", &format!("{i:08}-2222"));
                    Store::open(path, None)
                        .and_then(|mut s| s.collect(raw.as_bytes()))
                        .map_err(|e| e.to_string())
                })
            })
            .collect();
        for h in handles {
            h.join().unwrap().unwrap();
        }
    });
    assert_eq!(
        Store::open(dir.path(), None).unwrap().counts().unwrap(),
        (8, 0)
    );
}
