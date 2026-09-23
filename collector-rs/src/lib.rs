#![forbid(unsafe_code)]
#[cfg(not(unix))]
compile_error!("This experimental collector currently requires Unix private-file semantics");

pub mod store;
use chrono::{DateTime, Datelike, Utc};
use serde::de::{MapAccess, Visitor};
use serde_json::{Map, Value};
use std::fmt;

pub type Result<T> = std::result::Result<T, Box<dyn std::error::Error>>;
pub const MAX_INPUT: usize = 65536;
pub const MAX_WIRE: usize = 16384;
pub type Object = Map<String, Value>;

// Reject duplicate top-level keys before interpretation. Nested private values
// are discarded, never inspected or persisted; all accepted fields are scalars.
pub fn object(raw: &[u8]) -> Result<Object> {
    struct Unique;
    impl<'de> Visitor<'de> for Unique {
        type Value = Object;
        fn expecting(&self, f: &mut fmt::Formatter) -> fmt::Result {
            f.write_str("unique object")
        }
        fn visit_map<A: MapAccess<'de>>(self, mut map: A) -> std::result::Result<Object, A::Error> {
            let mut result = Object::new();
            while let Some((k, v)) = map.next_entry::<String, Value>()? {
                if result.insert(k, v).is_some() {
                    return Err(serde::de::Error::custom("duplicate"));
                }
            }
            Ok(result)
        }
    }
    let mut de = serde_json::Deserializer::from_slice(raw);
    let result = serde::Deserializer::deserialize_map(&mut de, Unique)?;
    de.end()?;
    Ok(result)
}

fn text<'a>(o: &'a Object, key: &str) -> Result<&'a str> {
    o.get(key)
        .and_then(Value::as_str)
        .ok_or_else(|| "invalid_metadata".into())
}
fn token(s: &str) -> bool {
    !s.is_empty()
        && s.len() <= 200
        && s.bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"_:./@+-".contains(&b))
}
fn uuid(s: &str) -> bool {
    s.len() == 36
        && s.bytes().enumerate().all(|(i, b)| {
            if [8, 13, 18, 23].contains(&i) {
                b == b'-'
            } else {
                b.is_ascii_digit() || (b'a'..=b'f').contains(&b)
            }
        })
}
fn repository(s: &str) -> bool {
    let Some(rest) = s.strip_prefix("https://github.com/") else {
        return false;
    };
    let parts: Vec<_> = rest.split('/').collect();
    parts.len() == 2
        && parts.iter().all(|p| !p.is_empty())
        && parts[0]
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"_-".contains(&b))
        && parts[1]
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"_.-".contains(&b))
        && !s.ends_with(".git")
        && !s.ends_with("/.")
        && !s.ends_with("/..")
        && token(s)
}
pub fn endpoint(s: &str) -> Result<()> {
    let u = reqwest::Url::parse(s)?;
    if !u.username().is_empty()
        || u.password().is_some()
        || u.query().is_some()
        || u.fragment().is_some()
        || u.path() != "/ingest/v1/observations"
        || u.host_str().is_none()
        || u.as_str() != s
        || !(u.scheme() == "https"
            || (u.scheme() == "http"
                && matches!(u.host_str(), Some("127.0.0.1" | "[::1]" | "localhost"))))
    {
        return Err("invalid_endpoint".into());
    }
    Ok(())
}
pub fn scope(raw: &[u8]) -> Result<Object> {
    if raw.len() > MAX_WIRE {
        return Err("scope_too_large".into());
    }
    let o = object(raw)?;
    let keys = [
        "endpoint",
        "collector_ref",
        "repository",
        "repository_ref",
        "installation_id",
    ];
    if o.len() != keys.len()
        || keys.iter().any(|k| !o.contains_key(*k))
        || !token(text(&o, "collector_ref")?)
        || !uuid(text(&o, "installation_id")?)
        || !repository(text(&o, "repository_ref")?)
        || !text(&o, "repository")?.starts_with('/')
        || text(&o, "repository")?.len() > 4096
    {
        return Err("invalid_scope".into());
    }
    endpoint(text(&o, "endpoint")?)?;
    Ok(o)
}

/// Local normalized Python-observer metadata, not raw vendor hooks. Unknown
/// identity only in v1. Nulls are omitted exactly like Python's SQL projection.
pub fn freeze(raw: &[u8], config: &Object) -> Result<Vec<u8>> {
    if raw.len() > MAX_INPUT {
        return Err("input_too_large".into());
    }
    let input = object(raw)?;
    if input.get("schema_version").and_then(Value::as_u64) != Some(1)
        || input.get("repository") != config.get("repository")
        || input.get("installation_id") != config.get("installation_id")
        || input.get("attribution_basis").and_then(Value::as_str) != Some("unknown")
        || input.get("actor_ref").is_some_and(|v| !v.is_null())
    {
        return Err("invalid_scope_or_identity".into());
    }
    let fields = [
        "schema_version",
        "observation_id",
        "installation_id",
        "runtime",
        "session_id",
        "event",
        "observed_at",
        "attribution_basis",
        "prompt_id",
        "turn_id",
        "tool_use_id",
        "agent_id",
        "agent_type",
        "model",
        "tool_name",
        "source",
        "reason",
    ];
    let mut out = Object::new();
    for field in fields {
        if let Some(v) = input.get(field).filter(|v| !v.is_null()) {
            out.insert(field.into(), v.clone());
        }
    }
    for field in [
        "observation_id",
        "installation_id",
        "runtime",
        "session_id",
        "event",
        "attribution_basis",
    ] {
        if !token(text(&out, field)?) {
            return Err("invalid_metadata".into());
        }
    }
    if !uuid(text(&out, "observation_id")?)
        || !matches!(text(&out, "runtime")?, "claude-code" | "codex")
    {
        return Err("invalid_metadata".into());
    }
    let n = out
        .get("observed_at")
        .filter(|v| v.is_number())
        .ok_or("invalid_timestamp")?;
    let micros = decimal_micros(&n.to_string())?;
    let time = DateTime::<Utc>::from_timestamp_micros(micros).ok_or("invalid_timestamp")?;
    // Python strftime's sub-1000 year padding varies by platform. This slice
    // refuses that historical range rather than freezing different wire bytes.
    if time.year() < 1000 {
        return Err("invalid_timestamp".into());
    }
    let timestamp = time.format("%Y-%m-%dT%H:%M:%S%.6fZ").to_string();
    if timestamp.len() != 27 {
        return Err("invalid_timestamp".into());
    }
    out.insert("observed_at".into(), timestamp.into());
    out.insert("repository_ref".into(), config["repository_ref"].clone());
    for (k, v) in &out {
        if k != "schema_version" && !v.as_str().is_some_and(token) {
            return Err("invalid_metadata".into());
        }
    }
    let bytes = serde_json::to_vec(&out)?; // sorted map; retained strings are ASCII
    if bytes.len() > MAX_WIRE {
        return Err("wire_too_large".into());
    }
    Ok(bytes)
}

// Input is serde_json's validated, round-trip numeric spelling, matching the
// Python reference's Decimal(str(number)), not binary float multiplication.
// Move the decimal point logically: never materialize exponent-sized integers
// or impose a fixed decimal scale. Only the bounded integer microseconds are
// accumulated; discarded digits decide exact half-even rounding.
fn decimal_micros(number: &str) -> Result<i64> {
    let negative = number.starts_with('-');
    let magnitude = number.strip_prefix('-').unwrap_or(number);
    let (coefficient, exponent) = match magnitude.split_once(['e', 'E']) {
        Some((coefficient, exponent)) => (coefficient, exponent.parse::<i32>()?),
        None => (magnitude, 0),
    };
    let whole = coefficient.find('.').unwrap_or(coefficient.len()) as i32;
    let digits: Vec<u8> = coefficient.bytes().filter(|b| *b != b'.').collect();
    let point = whole + exponent + 6;
    if point < 0 {
        return Ok(0);
    }
    // An i64 has at most 19 decimal digits. No accepted timestamp can be
    // larger; range rejection happens before loops proportional to exponent.
    if point > 19 {
        return Err("invalid_timestamp".into());
    }
    let point = point as usize;
    let mut micros = 0_u64;
    for i in 0..point {
        let digit = digits.get(i).copied().unwrap_or(b'0') - b'0';
        micros = micros
            .checked_mul(10)
            .and_then(|n| n.checked_add(u64::from(digit)))
            .ok_or("invalid_timestamp")?;
    }
    if let Some(&first) = digits.get(point) {
        let above_half =
            first > b'5' || (first == b'5' && digits[point + 1..].iter().any(|b| *b != b'0'));
        if above_half || (first == b'5' && micros % 2 == 1) {
            micros = micros.checked_add(1).ok_or("invalid_timestamp")?;
        }
    }
    let signed = if negative {
        -i128::from(micros)
    } else {
        i128::from(micros)
    };
    Ok(i64::try_from(signed)?)
}

pub fn receipt(raw: &[u8], payload: &[u8], collector: &str) -> Result<Object> {
    if raw.len() > MAX_WIRE {
        return Err("invalid_receipt".into());
    }
    let r = object(raw)?;
    let p = object(payload)?;
    if r.len() != 4
        || r.get("collector_ref").and_then(Value::as_str) != Some(collector)
        || r.get("observation_id") != p.get("observation_id")
        || r.get("installation_id") != p.get("installation_id")
        || !r
            .get("record_id")
            .and_then(Value::as_u64)
            .is_some_and(|id| id > 0)
    {
        return Err("invalid_receipt".into());
    }
    Ok(r)
}
