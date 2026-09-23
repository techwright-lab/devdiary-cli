use devdiary_collector::{endpoint, freeze, receipt, scope};

#[test]
fn exact_receipt_types_and_duplicate_keys() {
    let wire = include_bytes!("fixtures/wire.json");
    let source = include_str!("fixtures/receipt.json");
    for (from, to) in [
        ("\"record_id\": 17", "\"record_id\": true"),
        ("\"record_id\": 17", "\"record_id\": 17.0"),
        ("\"record_id\": 17", "\"record_id\": -1"),
        ("\"record_id\": 17", "\"record_id\": 0"),
        ("\"record_id\": 17", "\"record_id\": 17, \"extra\": 1"),
        ("\"record_id\": 17", "\"record_id\": 17, \"record_id\": 17"),
        ("collector-fixture", "wrong"),
        ("22222222", "33333333"),
        ("11111111", "33333333"),
    ] {
        assert!(
            receipt(
                source.replace(from, to).as_bytes(),
                wire,
                "collector-fixture"
            )
            .is_err()
        );
    }
}

#[test]
fn no_remote_plaintext_or_endpoint_normalization() {
    for valid in [
        "https://example.test/ingest/v1/observations",
        "http://127.0.0.1:8888/ingest/v1/observations",
        "http://[::1]:8888/ingest/v1/observations",
    ] {
        assert!(endpoint(valid).is_ok());
    }
    for bad in [
        "http://example.test/ingest/v1/observations",
        "https://user:password@example.test/ingest/v1/observations",
        "https://example.test/ingest/v1/observations?q=1",
        "https://example.test/ingest/v1/observations#fragment",
        "http://127.1/ingest/v1/observations",
        "https://example.test/ingest/v1/sessions",
    ] {
        assert!(endpoint(bad).is_err());
    }
}

#[test]
fn python_frozen_bytes_and_rails_receipt() {
    let config = scope(include_bytes!("fixtures/scope.json")).unwrap();
    let wire = freeze(include_bytes!("fixtures/local-observation.json"), &config).unwrap();
    assert_eq!(wire, include_bytes!("fixtures/wire.json"));
    assert!(
        receipt(
            include_bytes!("fixtures/receipt.json"),
            &wire,
            "collector-fixture"
        )
        .is_ok()
    );
    for bad in [
        br#"{"record_id":true}"#.as_slice(),
        br#"{"record_id":17,"record_id":18}"#,
        br#"{"record_id":0}"#,
    ] {
        assert!(receipt(bad, &wire, "collector-fixture").is_err());
    }
}

#[test]
fn refuses_scope_and_identity_coercion() {
    let config = scope(include_bytes!("fixtures/scope.json")).unwrap();
    let original = include_str!("fixtures/local-observation.json");
    for (from, to) in [
        ("\"schema_version\": 1", "\"schema_version\": true"),
        ("\"actor_ref\": null", "\"actor_ref\": \"invented\""),
        ("\"turn_id\": \"turn-1\"", "\"turn_id\": 1"),
        ("1720000000.123456", "1e25"),
        ("1720000000.123456", "-62135596800"),
        ("/fixture/repo", "/other/repo"),
    ] {
        assert!(freeze(original.replace(from, to).as_bytes(), &config).is_err());
    }
}
