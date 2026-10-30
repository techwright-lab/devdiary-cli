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
const SECRET_WORDS: [&str; 8] = [
    "SECRET",
    "TOKEN",
    "KEY",
    "PASSWORD",
    "PASSWD",
    "CREDENTIAL",
    "AUTH",
    "COOKIE",
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
    shaped && !SECRET_WORDS.iter().any(|word| name.contains(word))
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
        && entry["key"].as_str().is_some_and(orchestrator_key)
        && (1..=MAX_NAMES).contains(&names.len())
        && names.iter().all(|name| allowed_name(name))
        && REFS.iter().any(|(field, _)| env.contains_key(*field))
}
/// One bad name refuses its whole entry; a list that cannot be read is refused whole.
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
    let Some(saved) = read(&state.join(OFFERED))? else {
        return Ok(None);
    };
    let checked = offer(&saved["orchestrators"]);
    Ok(Some(
        json!({"orchestrators": checked["orchestrators"], "refused": saved["refused"]}),
    ))
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
pub fn waiting(state: &Path) -> Result<bool> {
    let Ok(approved) = approved(state) else {
        return Ok(true);
    };
    Ok(offered(state)?.is_some_and(|offer| list(&offer) != approved))
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
pub fn summary(state: &Path) -> Result<Value> {
    let saved = approved(state);
    let unreadable = saved.is_err();
    let approved = saved.unwrap_or_default();
    let offer = offered(state)?.unwrap_or_else(|| json!({}));
    let offered = list(&offer);
    Ok(json!({
        "approved": keys(&approved),
        "approved_list_unreadable": unreadable,
        "offered": keys(&offered),
        "waiting_for_approval": waiting(state)?,
        "refused": offer["refused"].as_u64().unwrap_or(0),
    }))
}
pub(crate) fn describe(entries: &[Value]) -> Vec<String> {
    entries
        .iter()
        .filter_map(|entry| {
            let key = entry["key"].as_str()?;
            let names = env_names(entry["env"].as_object()?)?;
            Some(format!("{key}: {}", names.join(", ")))
        })
        .collect()
}

pub(crate) struct Declared {
    pub(crate) row: Object,
    pub(crate) checkout: Vec<String>,
}
pub(crate) fn declared(entries: &[Value]) -> Option<Declared> {
    entries.iter().find_map(declare)
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
    Some(Declared { row, checkout })
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
        for word in SECRET_WORDS {
            for name in [word.to_string(), format!("A_{word}_B"), format!("X{word}")] {
                assert!(!allowed_name(&name), "{name}");
            }
        }
        assert!(!allowed_name("MONKEY_ID"));
        assert!(!allowed_name("OAUTH_ID"));
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
                "paperclip: PAPERCLIP_AGENT_ID, PAPERCLIP_WORKSPACE_WORKTREE_PATH, PAPERCLIP_WORKSPACE_CWD, PAPERCLIP_RUN_ID, PAPERCLIP_TASK_ID"
            ]
        );
    }
}
