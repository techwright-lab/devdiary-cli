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
fn shared_python_decimal_timestamp_goldens() {
    let config = scope(include_bytes!("fixtures/scope.json")).unwrap();
    let cases: Vec<serde_json::Value> =
        serde_json::from_slice(include_bytes!("fixtures/timestamps.json")).unwrap();
    for case in cases {
        let mut row: serde_json::Value =
            serde_json::from_slice(include_bytes!("fixtures/local-observation.json")).unwrap();
        row["observed_at"] = case["observed_at"].clone();
        let actual = freeze(&serde_json::to_vec(&row).unwrap(), &config);
        if case["timestamp"].is_null() {
            assert!(actual.is_err(), "{case}");
        } else {
            let wire: serde_json::Value = serde_json::from_slice(&actual.unwrap()).unwrap();
            assert_eq!(wire["observed_at"], case["timestamp"], "{case}");
        }
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

fn installation() -> devdiary_collector::Object {
    scope(include_bytes!("fixtures/installation-scope.json")).unwrap()
}
fn local_v2() -> serde_json::Value {
    serde_json::from_slice(include_bytes!("fixtures/local-observation-v2.json")).unwrap()
}
fn frozen(
    config: &devdiary_collector::Object,
    row: &serde_json::Value,
) -> Option<serde_json::Value> {
    freeze(&serde_json::to_vec(row).unwrap(), config)
        .ok()
        .map(|w| serde_json::from_slice(&w).unwrap())
}

#[test]
fn installation_scope_shape_is_exact() {
    let base: serde_json::Value =
        serde_json::from_slice(include_bytes!("fixtures/installation-scope.json")).unwrap();
    assert!(scope(&serde_json::to_vec(&base).unwrap()).is_ok());
    let mut opted = base.clone();
    opted["record_orchestrated_outside_repos"] = true.into();
    opted["observation_schema_versions"] = serde_json::json!([1]);
    assert!(scope(&serde_json::to_vec(&opted).unwrap()).is_ok());
    for (key, value) in [
        ("scope_kind", serde_json::json!("repository")),
        ("observation_schema_versions", serde_json::json!([2])),
        ("observation_schema_versions", serde_json::json!([1, 3])),
        ("observation_schema_versions", serde_json::json!([2, 1])),
        ("observation_schema_versions", serde_json::json!([1, 1])),
        ("observation_schema_versions", serde_json::json!("1,2")),
        (
            "record_orchestrated_outside_repos",
            serde_json::json!("true"),
        ),
        ("repository", serde_json::json!("/fixture/repo")),
        (
            "repository_ref",
            serde_json::json!("https://github.com/acme/project"),
        ),
    ] {
        let mut bad = base.clone();
        bad[key] = value;
        assert!(scope(&serde_json::to_vec(&bad).unwrap()).is_err(), "{key}");
    }
}

#[test]
fn v2_frozen_bytes_carry_orchestrator_and_commit_fields() {
    let wire = freeze(
        include_bytes!("fixtures/local-observation-v2.json"),
        &installation(),
    )
    .unwrap();
    assert_eq!(wire, include_bytes!("fixtures/wire-v2.json"));
    assert!(
        !String::from_utf8(wire)
            .unwrap()
            .contains("PRIVATE-SENTINEL")
    );
}

#[test]
fn v2_refuses_identity_claims_and_malformed_capture() {
    let config = installation();
    let cases: Vec<(&str, serde_json::Value)> = vec![
        ("actor_ref", "invented".into()),
        ("attribution_basis", "explicit_local_binding".into()),
        ("orchestrator", "Not A Key".into()),
        ("orchestrator_agent_ref", "agent-7".into()),
        (
            "orchestrator_run_ref",
            "66666666-6666-4666-8666-66666666666G".into(),
        ),
        (
            "commits",
            serde_json::json!(["0123456789ABCDEF0123456789ABCDEF01234567"]),
        ),
        ("commits", serde_json::json!(["0123456"])),
        ("commits", serde_json::json!([])),
        (
            "commits",
            serde_json::json!(vec!["0123456789abcdef0123456789abcdef01234567"; 21]),
        ),
        ("overlap_count", 65.into()),
        ("overlap_count", (-1).into()),
        ("overlap_count", "0".into()),
        ("commit_gap", "reflog_overflow".into()),
        ("repository", "/fixture/repo".into()),
        (
            "repository_ref",
            "https://github.com/acme/project.git".into(),
        ),
        ("schema_version", 1.into()),
    ];
    for (key, value) in cases {
        let mut row = local_v2();
        row[key] = value;
        assert!(frozen(&config, &row).is_none(), "{key}");
    }
    let mut plain = local_v2();
    plain.as_object_mut().unwrap().remove("orchestrator");
    assert!(frozen(&config, &plain).is_none(), "refs need a preset key");
    for key in [
        "orchestrator_agent_ref",
        "orchestrator_run_ref",
        "orchestrator_task_ref",
    ] {
        plain.as_object_mut().unwrap().remove(key);
    }
    assert!(
        frozen(&config, &plain)
            .unwrap()
            .get("orchestrator")
            .is_none()
    );
    let mut other = local_v2();
    other["orchestrator"] = "cron".into();
    assert_eq!(frozen(&config, &other).unwrap()["orchestrator"], "cron");
    for key in ["none", "Upper", "has space", "a/b", ""] {
        let mut row = local_v2();
        row["orchestrator"] = key.into();
        assert!(frozen(&config, &row).is_none(), "{key}");
    }
    let mut gap = local_v2();
    gap.as_object_mut().unwrap().remove("commits");
    gap["commit_gap"] = "reflog_rewritten".into();
    assert_eq!(
        frozen(&config, &gap).unwrap()["commit_gap"],
        "reflog_rewritten"
    );
}

#[test]
fn v2_commit_fields_ride_only_on_a_bash_post_tool_row() {
    let config = installation();
    let failure = serde_json::json!({"event": "PostToolUseFailure"});
    assert!(frozen(&config, &merged(failure)).is_some());
    for change in [
        serde_json::json!({"event": "SessionStart"}),
        serde_json::json!({"event": "UserPromptSubmit"}),
        serde_json::json!({"event": "Stop"}),
        serde_json::json!({"event": "PreToolUse"}),
        serde_json::json!({"tool_name": "Edit"}),
        serde_json::json!({"tool_name": null}),
        serde_json::json!({"tool_use_id": null}),
        serde_json::json!({"overlap_count": null}),
    ] {
        assert!(
            frozen(&config, &merged(change.clone())).is_none(),
            "{change}"
        );
        let mut gap = merged(change.clone());
        gap.as_object_mut().unwrap().remove("commits");
        gap["commit_gap"] = "reflog_rewritten".into();
        if change.get("overlap_count").is_none() {
            assert!(frozen(&config, &gap).is_none(), "gap {change}");
        }
    }
}

fn merged(change: serde_json::Value) -> serde_json::Value {
    let mut row = local_v2();
    for (key, value) in change.as_object().unwrap() {
        row[key.as_str()] = value.clone();
    }
    row
}

#[test]
fn v2_repo_less_rows_keep_the_two_consents_apart() {
    let mut row = local_v2();
    row.as_object_mut().unwrap().remove("repository_ref");
    let mut opted_in = installation();
    opted_in.insert("record_orchestrated_outside_repos".into(), true.into());
    assert!(frozen(&opted_in, &row).is_none(), "repo-less commits");
    for key in ["commits", "overlap_count"] {
        row.as_object_mut().unwrap().remove(key);
    }
    assert!(frozen(&installation(), &row).is_none(), "no agent opt-in");
    let wire = frozen(&opted_in, &row).unwrap();
    assert!(wire.get("repository_ref").is_none());
    for key in [
        "orchestrator_agent_ref",
        "orchestrator_run_ref",
        "orchestrator_task_ref",
    ] {
        row.as_object_mut().unwrap().remove(key);
    }
    row["orchestrator"] = "unapproved".into();
    assert!(frozen(&opted_in, &row).is_none(), "unanswered offer");
    row.as_object_mut().unwrap().remove("orchestrator");
    assert!(frozen(&installation(), &row).is_some(), "owner row");
}

#[test]
fn installation_without_advertised_v2_sends_v1() {
    let mut config = installation();
    config.insert("observation_schema_versions".into(), serde_json::json!([1]));
    let mut row = local_v2();
    row["schema_version"] = 1.into();
    for key in ["commits", "overlap_count"] {
        row.as_object_mut().unwrap().remove(key);
    }
    let wire = frozen(&config, &row).unwrap();
    assert_eq!(wire["schema_version"], 1);
    assert_eq!(wire["repository_ref"], "https://github.com/acme/project");
    assert!(wire.get("orchestrator").is_none());
    assert!(wire.get("orchestrator_agent_ref").is_none());
    row.as_object_mut().unwrap().remove("repository_ref");
    assert!(frozen(&config, &row).is_none());
    row["schema_version"] = 2.into();
    assert!(frozen(&config, &row).is_none());
}

const DECLARED_RUN: &str = "urn:devdiary:run:88888888-8888-4888-8888-888888888888";

#[test]
fn v2_declared_run_ref_freezes_to_exact_bytes() {
    let wire = freeze(
        include_bytes!("fixtures/local-observation-declared-run.json"),
        &installation(),
    )
    .unwrap();
    assert_eq!(wire, include_bytes!("fixtures/wire-declared-run.json"));
}

#[test]
fn v2_refuses_a_declared_run_ref_that_is_not_the_exact_urn() {
    for bad in [
        serde_json::json!("urn:devdiary:run:88888888-8888-4888-8888-88888888888A"),
        serde_json::json!("URN:DEVDIARY:RUN:88888888-8888-4888-8888-888888888888"),
        serde_json::json!("urn:devdiary:event:88888888-8888-4888-8888-888888888888"),
        serde_json::json!("urn:devdiary:run:88888888-8888-4888-8888-888888888888\n"),
        serde_json::json!(" urn:devdiary:run:88888888-8888-4888-8888-888888888888"),
        serde_json::json!("urn:devdiary:run:"),
        serde_json::json!("urn:devdiary:run:fixed-id"),
        serde_json::json!("88888888-8888-4888-8888-888888888888"),
        serde_json::json!(""),
        serde_json::json!(1),
        serde_json::json!([DECLARED_RUN]),
    ] {
        let mut row = local_v2();
        row["declared_run_ref"] = bad.clone();
        assert!(frozen(&installation(), &row).is_none(), "{bad}");
    }
}

#[test]
fn only_v2_rows_carry_a_declared_run_ref() {
    let mut config = installation();
    config.insert("observation_schema_versions".into(), serde_json::json!([1]));
    let mut row = local_v2();
    row["schema_version"] = 1.into();
    row["declared_run_ref"] = DECLARED_RUN.into();
    assert!(
        frozen(&config, &row)
            .unwrap()
            .get("declared_run_ref")
            .is_none()
    );
}
