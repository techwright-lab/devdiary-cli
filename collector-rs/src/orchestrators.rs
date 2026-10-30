//! Environment names that mark a run as started by a control plane. The server
//! offers the list; the hook reads only what the local person approved.
use crate::{
    Object, Result, orchestrator_key,
    store::{self, create_private, private},
};
use serde_json::{Value, json};
use std::{collections::HashSet, fs::File, io::Read, path::Path};

const OFFERED: &str = "orchestrators-offered.json";
const APPROVED: &str = "orchestrators-approved.json";
pub(crate) const STATE_FILES: [&str; 4] = [
    OFFERED,
    APPROVED,
    "orchestrators-offered.json.next",
    "orchestrators-approved.json.next",
];
const MAX_FILE: u64 = 65536;
const MAX_ORCHESTRATORS: usize = 8;
const MAX_NAMES: usize = 8;
const MAX_NAME: usize = 64;
const UNREADABLE: &str = "offered_list_unreadable";
const UNAPPROVED: &str = "unapproved";
const SECRET_WORDS: [&str; 14] = [
    "SECRET",
    "KEY",
    "AUTH",
    "COOKIE",
    "PASS",
    "PWD",
    "CRED",
    "TOK",
    "DSN",
    "BEARER",
    "JWT",
    "PRIVATE",
    "SESSION",
    "DATABASE_URL",
];
const ALWAYS_SET: [&str; 11] = [
    "HOME", "PATH", "USER", "LOGNAME", "PWD", "OLDPWD", "SHELL", "TERM", "LANG", "HOSTNAME",
    "TMPDIR",
];
const REFS: [(&str, &str); 3] = [
    ("agent_ref", "orchestrator_agent_ref"),
    ("run_ref", "orchestrator_run_ref"),
    ("task_ref", "orchestrator_task_ref"),
];

fn allowed_name(name: &str) -> bool {
    let shaped = name.len() <= MAX_NAME
        && name.bytes().next().is_some_and(|b| b.is_ascii_uppercase())
        && name
            .bytes()
            .all(|b| b.is_ascii_uppercase() || b.is_ascii_digit() || b == b'_');
    shaped
        && !ALWAYS_SET.contains(&name)
        && !SECRET_WORDS.iter().any(|word| name.contains(word))
        && !name.split('_').any(|segment| segment == "PAT")
}
fn env_names(env: &Object) -> Option<Vec<&str>> {
    let mut names = Vec::new();
    for (field, value) in env {
        match (field.as_str(), value) {
            ("agent_ref" | "run_ref" | "task_ref", Value::String(name)) => {
                names.push(name.as_str())
            }
            ("checkout_path", Value::Array(list)) => {
                for name in list {
                    names.push(name.as_str()?);
                }
            }
            _ => return None,
        }
    }
    Some(names)
}
fn valid(entry: &Value) -> bool {
    let Some(env) = entry.get("env").and_then(Value::as_object) else {
        return false;
    };
    let names = env_names(env).unwrap_or_default();
    entry.as_object().is_some_and(|o| o.len() == 2)
        && entry["key"]
            .as_str()
            .is_some_and(|key| orchestrator_key(key) && key != UNAPPROVED)
        && (1..=MAX_NAMES).contains(&names.len())
        && names.iter().all(|name| allowed_name(name))
        && REFS.iter().any(|(field, _)| env.contains_key(*field))
}
pub(crate) fn offer(list: &Value) -> Value {
    let Some(entries) = list.as_array().filter(|l| l.len() <= MAX_ORCHESTRATORS) else {
        let refused = list.as_array().map_or(1, Vec::len);
        return json!({"orchestrators": [], "refused": refused});
    };
    let mut keys = HashSet::new();
    let kept: Vec<&Value> = entries
        .iter()
        .filter(|entry| valid(entry) && keys.insert(entry["key"].as_str()))
        .collect();
    json!({"orchestrators": kept, "refused": entries.len() - kept.len()})
}
fn read(path: &Path) -> Result<Option<Value>> {
    if !path.exists() {
        return Ok(None);
    }
    private(path, false)?;
    let mut raw = Vec::new();
    File::open(path)?.take(MAX_FILE + 1).read_to_end(&mut raw)?;
    if raw.len() as u64 > MAX_FILE {
        return Err("invalid_orchestrators".into());
    }
    let value = serde_json::from_slice(&raw).map_err(|_| "invalid_orchestrators")?;
    Ok(Some(value))
}
fn list(offer: &Value) -> Vec<Value> {
    offer["orchestrators"]
        .as_array()
        .cloned()
        .unwrap_or_default()
}
fn offered(state: &Path) -> Result<Option<Value>> {
    let Some(saved) = read(&state.join(OFFERED)).map_err(|_| UNREADABLE)? else {
        return Ok(None);
    };
    let stored = saved.get("orchestrators").filter(|l| l.is_array());
    let earlier = saved.get("refused").and_then(Value::as_u64);
    let (Some(stored), Some(earlier)) = (stored, earlier) else {
        return Err(UNREADABLE.into());
    };
    let mut checked = offer(stored);
    checked["refused"] = json!(checked["refused"].as_u64().unwrap_or(0) + earlier);
    Ok(Some(checked))
}
/// Rereads the rules on every load, so a list saved by any other means is no wider.
pub fn approved(state: &Path) -> Result<Vec<Value>> {
    let Some(saved) = read(&state.join(APPROVED))? else {
        return Ok(Vec::new());
    };
    let checked = offer(&saved);
    if checked["refused"] != json!(0) {
        return Err("invalid_orchestrators".into());
    }
    Ok(list(&checked))
}
pub(crate) fn save_offer(state: &Path, offer: &Value) -> Result<()> {
    store::replace(state, OFFERED, &serde_json::to_vec(offer)?)
}
pub fn waiting(state: &Path) -> bool {
    let Ok(approved) = approved(state) else {
        return true;
    };
    offered(state)
        .ok()
        .flatten()
        .is_some_and(|offer| list(&offer) != approved)
}
pub fn offer_unreadable(state: &Path) -> bool {
    offered(state).is_err()
}
/// What approval would save, as read once: approving never takes a later offer.
pub(crate) fn pending(state: &Path) -> Result<(Vec<Value>, u64)> {
    let offer = offered(state)?.unwrap_or_else(|| json!({}));
    Ok((list(&offer), offer["refused"].as_u64().unwrap_or(0)))
}
pub(crate) fn approve(state: &Path, entries: &[Value]) -> Result<()> {
    let lock = create_private(&state.join("sync.lock"))?;
    lock.lock()?;
    store::replace(state, APPROVED, &serde_json::to_vec(entries)?)
}
fn keys(entries: &[Value]) -> Vec<&Value> {
    entries.iter().map(|entry| &entry["key"]).collect()
}
fn notice(offered: &[Value], approved: &[Value]) -> Option<String> {
    let outcome = if approved.is_empty() {
        "stay unknown"
    } else {
        "may look interactive"
    };
    let known = keys(approved);
    let missing: Vec<&str> = keys(offered)
        .into_iter()
        .filter(|key| !known.contains(key))
        .filter_map(Value::as_str)
        .collect();
    (!missing.is_empty()).then(|| {
        format!(
            "Runs of {} {outcome} until approved. In a terminal run: devdiary-collector orchestrators approve",
            missing.join(", ")
        )
    })
}
pub fn summary(state: &Path) -> Value {
    let saved = approved(state);
    let approved_unreadable = saved.is_err();
    let approved = saved.unwrap_or_default();
    let stored = offered(state);
    let offered_unreadable = stored.is_err();
    let offer = stored.ok().flatten().unwrap_or_else(|| json!({}));
    let offered = list(&offer);
    json!({
        "approved": keys(&approved),
        "approved_list_unreadable": approved_unreadable,
        "offered": keys(&offered),
        "offered_list_unreadable": offered_unreadable,
        "waiting_for_approval": waiting(state),
        "refused": offer["refused"].as_u64().unwrap_or(0),
        "notice": notice(&offered, &approved),
    })
}
fn field_names(entry: &Value, fields: &[&str]) -> Vec<String> {
    fields
        .iter()
        .filter_map(|field| entry["env"].get(*field))
        .flat_map(|value| match value {
            Value::Array(list) => list.clone(),
            other => vec![other.clone()],
        })
        .filter_map(|name| name.as_str().map(str::to_owned))
        .collect()
}
pub(crate) fn describe(entries: &[Value]) -> Vec<String> {
    let mut lines = Vec::new();
    for entry in entries {
        lines.push(entry["key"].as_str().unwrap_or_default().to_owned());
        let refs = field_names(entry, &["agent_ref", "run_ref", "task_ref"]);
        lines.push(format!("  refs: {}", refs.join(", ")));
        let paths = field_names(entry, &["checkout_path"]);
        if !paths.is_empty() {
            lines.push(format!(
                "  checkout paths (used here only): {}",
                paths.join(", ")
            ));
        }
    }
    lines
}
fn fields(entries: &[Value]) -> Vec<String> {
    let mut lines = Vec::new();
    for entry in entries {
        let key = entry["key"].as_str().unwrap_or_default();
        for field in ["agent_ref", "checkout_path", "run_ref", "task_ref"] {
            for name in field_names(entry, &[field]) {
                lines.push(format!("{key} {field} {name}"));
            }
        }
    }
    lines
}
pub(crate) fn changes(old: &[Value], new: &[Value]) -> (Vec<String>, Vec<String>) {
    let (old, new) = (fields(old), fields(new));
    let missing = |from: &[String], other: &[String]| -> Vec<String> {
        from.iter()
            .filter(|l| !other.contains(l))
            .cloned()
            .collect()
    };
    (missing(&new, &old), missing(&old, &new))
}

pub(crate) struct Declared {
    pub(crate) row: Object,
    pub(crate) checkout: Vec<String>,
    pub(crate) named: bool,
}
fn offer_unanswered(state: &Path) -> bool {
    match offered(state) {
        Ok(Some(offer)) => !list(&offer).is_empty() || offer["refused"] != json!(0),
        Ok(None) => false,
        Err(_) => true,
    }
}
// An agent run must never read as the machine owner's, so an offer nobody has
// answered marks every row, and no env name is read until a person approves.
pub(crate) fn declared(state: &Path) -> Result<Option<Declared>> {
    let approved = approved(state);
    if approved.as_ref().map_or(true, Vec::is_empty) && offer_unanswered(state) {
        let mut row = Object::new();
        row.insert("orchestrator".into(), UNAPPROVED.into());
        return Ok(Some(Declared {
            row,
            checkout: Vec::new(),
            named: false,
        }));
    }
    Ok(approved?.iter().find_map(declare))
}
// Any id, even malformed, marks the run orchestrated: leaving the key out would
// let the server bind someone else's run to the machine owner.
fn declare(entry: &Value) -> Option<Declared> {
    let env = entry["env"].as_object()?;
    let mut row = Object::new();
    let mut matched = false;
    for (field, wire) in REFS {
        let Some(name) = env.get(field).and_then(Value::as_str) else {
            continue;
        };
        match std::env::var(name) {
            Ok(value) => {
                matched = true;
                let value = value.to_ascii_lowercase();
                if crate::is_uuid(&value) {
                    row.insert(wire.into(), value.into());
                }
            }
            Err(std::env::VarError::NotUnicode(_)) => matched = true,
            Err(std::env::VarError::NotPresent) => {}
        }
    }
    if !matched {
        return None;
    }
    row.insert("orchestrator".into(), entry["key"].clone());
    let checkout = env
        .get("checkout_path")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter_map(|name| std::env::var(name.as_str()?).ok())
        .collect();
    Some(Declared {
        row,
        checkout,
        named: true,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn entry(key: &str, name: &str) -> Value {
        json!({"key": key, "env": {"agent_ref": name}})
    }
    fn kept(list: Value) -> (usize, u64) {
        let result = offer(&list);
        (
            result["orchestrators"].as_array().unwrap().len(),
            result["refused"].as_u64().unwrap(),
        )
    }

    #[test]
    fn names_keep_the_exact_shape() {
        for name in ["A", "A1_B", &format!("A{}", "B".repeat(63))] {
            assert!(allowed_name(name), "{name}");
        }
        for name in [
            "",
            "1A",
            "_A",
            "a",
            "Aa",
            "A-B",
            "A B",
            "A=B",
            "A\n",
            "É",
            &"A".repeat(65),
        ] {
            assert!(!allowed_name(name), "{name}");
        }
    }

    #[test]
    fn a_name_holding_a_secret_word_is_refused_wherever_the_word_sits() {
        for word in SECRET_WORDS
            .iter()
            .chain(&["TOKEN", "PASSWORD", "PASSWD", "CREDENTIAL"])
        {
            for name in [word.to_string(), format!("A_{word}_B"), format!("X{word}")] {
                assert!(!allowed_name(&name), "{name}");
            }
        }
        assert!(!allowed_name("MONKEY_ID"));
        assert!(!allowed_name("OAUTH_ID"));
    }

    #[test]
    fn short_secret_spellings_are_refused() {
        for name in [
            "GH_PAT",
            "PAT_VALUE",
            "MY_PAT_ID",
            "PGPASS",
            "MYSQL_PWD",
            "API_TOK",
            "DB_CREDS",
            "SENTRY_DSN",
            "OIDC_BEARER",
            "CI_JWT",
            "SSH_PRIVATE_FILE",
            "TERM_SESSION_ID",
            "DATABASE_URL",
            "PRIMARY_DATABASE_URL",
        ] {
            assert!(!allowed_name(name), "{name}");
        }
    }

    #[test]
    fn pat_counts_only_as_a_whole_segment() {
        for name in ["WORK_PATH", "PATH_HINT", "PATCH_ID", "SPAT_ID"] {
            assert!(allowed_name(name), "{name}");
        }
    }

    #[test]
    fn names_set_in_every_shell_are_refused_exactly() {
        for name in [
            "HOME", "PATH", "USER", "LOGNAME", "PWD", "OLDPWD", "SHELL", "TERM", "LANG",
            "HOSTNAME", "TMPDIR",
        ] {
            assert!(!allowed_name(name), "{name}");
        }
        for name in ["HOME_DIR", "USER_ID", "TERM_ID", "MY_SHELL"] {
            assert!(allowed_name(name), "{name}");
        }
    }

    #[test]
    fn the_contract_preset_names_all_pass() {
        for name in [
            "PAPERCLIP_AGENT_ID",
            "PAPERCLIP_RUN_ID",
            "PAPERCLIP_TASK_ID",
            "PAPERCLIP_WORKSPACE_WORKTREE_PATH",
            "PAPERCLIP_WORKSPACE_CWD",
        ] {
            assert!(allowed_name(name), "{name}");
        }
    }

    #[test]
    fn changes_name_the_field_that_moved() {
        let old = json!([{"key": "a", "env": {"agent_ref": "ONE", "run_ref": "TWO"}}]);
        let new = json!([
            {"key": "a", "env": {"agent_ref": "TWO", "checkout_path": ["DIR"]}},
            {"key": "b", "env": {"run_ref": "THREE"}},
        ]);
        let (added, removed) = changes(old.as_array().unwrap(), new.as_array().unwrap());
        assert_eq!(
            added,
            ["a agent_ref TWO", "a checkout_path DIR", "b run_ref THREE"]
        );
        assert_eq!(removed, ["a agent_ref ONE", "a run_ref TWO"]);
    }

    #[test]
    fn one_bad_name_refuses_only_its_entry() {
        let bad = json!({"key": "bad", "env": {
            "agent_ref": "GOOD_ID", "checkout_path": ["FINE", "API_TOKEN"]}});
        assert_eq!(kept(json!([bad, entry("good", "GOOD_ID")])), (1, 1));
    }

    #[test]
    fn eight_is_the_limit_for_entries_and_for_names() {
        let entries = |n: usize| -> Vec<Value> {
            (0..n).map(|i| entry(&format!("k{i}"), "GOOD_ID")).collect()
        };
        assert_eq!(kept(json!(entries(8))), (8, 0));
        assert_eq!(kept(json!(entries(9))), (0, 9));
        let names = |n: usize| -> Vec<String> { (0..n).map(|i| format!("N{i}")).collect() };
        let sized =
            |n: usize| json!({"key": "a", "env": {"run_ref": "R", "checkout_path": names(n)}});
        assert_eq!(kept(json!([sized(7)])), (1, 0));
        assert_eq!(kept(json!([sized(8)])), (0, 1));
    }

    #[test]
    fn an_entry_needs_an_exact_shape_and_a_key_the_wire_can_carry() {
        for bad in [
            json!("text"),
            json!({"env": {"run_ref": "R"}}),
            json!({"key": "a"}),
            json!({"key": "a", "env": {}}),
            json!({"key": "a", "env": {"checkout_path": ["P"]}}),
            json!({"key": "a", "env": {"run_ref": "R"}, "extra": 1}),
            json!({"key": "a", "env": {"run_ref": "R", "other": "S"}}),
            json!({"key": "a", "env": {"run_ref": ["R"]}}),
            json!({"key": "a", "env": {"run_ref": null}}),
            json!({"key": "a", "env": {"run_ref": "R", "checkout_path": "P"}}),
            json!({"key": "a", "env": {"run_ref": "R", "checkout_path": [1]}}),
            entry("none", "R"),
            entry("unapproved", "R"),
            entry("Upper", "R"),
            entry("has space", "R"),
            entry("", "R"),
            entry(&"a".repeat(65), "R"),
        ] {
            assert_eq!(kept(json!([bad])), (0, 1), "{bad}");
        }
    }

    #[test]
    fn a_repeated_key_keeps_the_first_entry() {
        let result = offer(&json!([entry("a", "ONE"), entry("a", "TWO")]));
        assert_eq!(result["orchestrators"], json!([entry("a", "ONE")]));
        assert_eq!(result["refused"], 1);
    }

    #[test]
    fn an_unreadable_list_is_refused_whole() {
        assert_eq!(kept(json!("paperclip")), (0, 1));
        assert_eq!(kept(json!({"key": "a"})), (0, 1));
    }

    #[test]
    fn the_contract_preset_is_accepted_unchanged() {
        let preset = json!({"key": "paperclip", "env": {
            "agent_ref": "PAPERCLIP_AGENT_ID",
            "run_ref": "PAPERCLIP_RUN_ID",
            "task_ref": "PAPERCLIP_TASK_ID",
            "checkout_path": ["PAPERCLIP_WORKSPACE_WORKTREE_PATH", "PAPERCLIP_WORKSPACE_CWD"],
        }});
        assert_eq!(offer(&json!([preset]))["orchestrators"], json!([preset]));
        assert_eq!(
            describe(&[preset]),
            [
                "paperclip",
                "  refs: PAPERCLIP_AGENT_ID, PAPERCLIP_RUN_ID, PAPERCLIP_TASK_ID",
                "  checkout paths (used here only): PAPERCLIP_WORKSPACE_WORKTREE_PATH, PAPERCLIP_WORKSPACE_CWD",
            ]
        );
    }
}
