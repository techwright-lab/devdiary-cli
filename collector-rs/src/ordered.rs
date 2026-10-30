//! Customer settings keep their key order and layout through apply and removal,
//! so removal restores the original bytes of compact and pretty documents.
//! String escapes and number spellings are re-rendered.
use crate::Result;
use serde::{
    Deserialize, Deserializer, Serialize, Serializer,
    de::{MapAccess, SeqAccess, Visitor},
    ser::{SerializeMap, SerializeSeq},
};
use serde_json::{Number, Value};
use std::fmt;

#[derive(Clone, Debug, PartialEq)]
pub(crate) enum Json {
    Null,
    Bool(bool),
    Number(Number),
    String(String),
    Array(Vec<Json>),
    Object(Vec<(String, Json)>),
}

impl Json {
    pub(crate) fn from_value(value: &Value) -> Self {
        match value {
            Value::Null => Self::Null,
            Value::Bool(b) => Self::Bool(*b),
            Value::Number(n) => Self::Number(n.clone()),
            Value::String(s) => Self::String(s.clone()),
            Value::Array(items) => Self::Array(items.iter().map(Self::from_value).collect()),
            Value::Object(map) => Self::Object(
                map.iter()
                    .map(|(k, v)| (k.clone(), Self::from_value(v)))
                    .collect(),
            ),
        }
    }
    pub(crate) fn matches(&self, value: &Value) -> bool {
        self.normalized() == Self::from_value(value)
    }
    fn normalized(&self) -> Self {
        match self {
            Self::Array(items) => Self::Array(items.iter().map(Self::normalized).collect()),
            Self::Object(entries) => {
                let mut sorted: Vec<_> = entries
                    .iter()
                    .map(|(k, v)| (k.clone(), v.normalized()))
                    .collect();
                sorted.sort_by(|a, b| a.0.cmp(&b.0));
                Self::Object(sorted)
            }
            other => other.clone(),
        }
    }
    pub(crate) fn get_mut(&mut self, key: &str) -> Option<&mut Json> {
        match self {
            Self::Object(entries) => entries.iter_mut().find(|(k, _)| k == key).map(|(_, v)| v),
            _ => None,
        }
    }
    /// Appends a missing key at the end, which is where removal deletes it from.
    pub(crate) fn entry(&mut self, key: &str, default: Json) -> Result<&mut Json> {
        let Self::Object(entries) = self else {
            return Err("invalid_hooks".into());
        };
        let index = match entries.iter().position(|(k, _)| k == key) {
            Some(index) => index,
            None => {
                entries.push((key.to_owned(), default));
                entries.len() - 1
            }
        };
        Ok(&mut entries[index].1)
    }
    pub(crate) fn remove(&mut self, key: &str) {
        if let Self::Object(entries) = self {
            entries.retain(|(k, _)| k != key);
        }
    }
    pub(crate) fn is_empty_container(&self) -> bool {
        matches!(self, Self::Object(e) if e.is_empty())
            || matches!(self, Self::Array(a) if a.is_empty())
    }
}

impl Serialize for Json {
    fn serialize<S: Serializer>(&self, s: S) -> std::result::Result<S::Ok, S::Error> {
        match self {
            Self::Null => s.serialize_unit(),
            Self::Bool(b) => s.serialize_bool(*b),
            Self::Number(n) => n.serialize(s),
            Self::String(v) => s.serialize_str(v),
            Self::Array(items) => {
                let mut seq = s.serialize_seq(Some(items.len()))?;
                for item in items {
                    seq.serialize_element(item)?;
                }
                seq.end()
            }
            Self::Object(entries) => {
                let mut map = s.serialize_map(Some(entries.len()))?;
                for (k, v) in entries {
                    map.serialize_entry(k, v)?;
                }
                map.end()
            }
        }
    }
}

impl<'de> Deserialize<'de> for Json {
    fn deserialize<D: Deserializer<'de>>(d: D) -> std::result::Result<Self, D::Error> {
        struct Ordered;
        impl<'de> Visitor<'de> for Ordered {
            type Value = Json;
            fn expecting(&self, f: &mut fmt::Formatter) -> fmt::Result {
                f.write_str("JSON")
            }
            fn visit_unit<E>(self) -> std::result::Result<Json, E> {
                Ok(Json::Null)
            }
            fn visit_bool<E>(self, b: bool) -> std::result::Result<Json, E> {
                Ok(Json::Bool(b))
            }
            fn visit_u64<E>(self, n: u64) -> std::result::Result<Json, E> {
                Ok(Json::Number(n.into()))
            }
            fn visit_i64<E>(self, n: i64) -> std::result::Result<Json, E> {
                Ok(Json::Number(n.into()))
            }
            fn visit_f64<E: serde::de::Error>(self, n: f64) -> std::result::Result<Json, E> {
                Number::from_f64(n)
                    .map(Json::Number)
                    .ok_or_else(|| E::custom("non-finite number"))
            }
            fn visit_str<E>(self, v: &str) -> std::result::Result<Json, E> {
                Ok(Json::String(v.to_owned()))
            }
            fn visit_string<E>(self, v: String) -> std::result::Result<Json, E> {
                Ok(Json::String(v))
            }
            fn visit_seq<A: SeqAccess<'de>>(
                self,
                mut seq: A,
            ) -> std::result::Result<Json, A::Error> {
                let mut items = Vec::new();
                while let Some(item) = seq.next_element()? {
                    items.push(item);
                }
                Ok(Json::Array(items))
            }
            fn visit_map<A: MapAccess<'de>>(
                self,
                mut map: A,
            ) -> std::result::Result<Json, A::Error> {
                let mut entries = Vec::new();
                while let Some(entry) = map.next_entry()? {
                    entries.push(entry);
                }
                Ok(Json::Object(entries))
            }
        }
        d.deserialize_any(Ordered)
    }
}

pub(crate) fn parse(raw: &[u8]) -> Result<Json> {
    Ok(serde_json::from_slice(raw)?)
}

/// Renders in the layout of `like`: compact when it is one line, otherwise
/// pretty with its indentation, line endings and trailing newline.
pub(crate) fn render(doc: &Json, like: &[u8]) -> Result<Vec<u8>> {
    let crlf = like.windows(2).any(|w| w == b"\r\n");
    let body = like.strip_suffix(b"\n").unwrap_or(like);
    let body = body.strip_suffix(b"\r").unwrap_or(body);
    let mut out = if body.contains(&b'\n') {
        pretty(doc, &indent(like))?
    } else {
        serde_json::to_vec(doc)?
    };
    if crlf {
        out = out
            .split(|b| *b == b'\n')
            .collect::<Vec<_>>()
            .join(&b"\r\n"[..]);
    }
    if like.ends_with(b"\n") {
        out.extend_from_slice(if crlf { b"\r\n" } else { b"\n" });
    }
    Ok(out)
}
fn indent(like: &[u8]) -> Vec<u8> {
    like.iter()
        .position(|b| *b == b'\n')
        .map(|start| {
            like[start + 1..]
                .iter()
                .take_while(|b| matches!(b, b' ' | b'\t'))
                .copied()
                .collect()
        })
        .filter(|i: &Vec<u8>| !i.is_empty())
        .unwrap_or_else(|| b"  ".to_vec())
}
fn pretty(doc: &Json, indent: &[u8]) -> Result<Vec<u8>> {
    let mut out = Vec::new();
    let mut ser = serde_json::Serializer::with_formatter(
        &mut out,
        serde_json::ser::PrettyFormatter::with_indent(indent),
    );
    doc.serialize(&mut ser)?;
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn stringify_style_documents_round_trip_byte_for_byte() {
        for raw in [
            "{\n  \"z\": 1,\n  \"a\": {\n    \"y\": [\n      true,\n      null,\n      \"s\"\n    ],\n    \"b\": {}\n  },\n  \"e\": []\n}\n",
            "{\n\t\"z\": -2.5\n}",
        ] {
            let doc = parse(raw.as_bytes()).unwrap();
            assert_eq!(
                String::from_utf8(render(&doc, raw.as_bytes()).unwrap()).unwrap(),
                raw
            );
        }
    }

    #[test]
    fn compact_and_crlf_documents_round_trip_byte_for_byte() {
        for raw in [
            "{\"model\":\"opus\",\"hooks\":{\"Stop\":[]}}",
            "{\"model\":\"opus\"}\n",
            "{\r\n  \"a\": [\r\n    1\r\n  ]\r\n}\r\n",
        ] {
            let doc = parse(raw.as_bytes()).unwrap();
            assert_eq!(
                String::from_utf8(render(&doc, raw.as_bytes()).unwrap()).unwrap(),
                raw
            );
        }
    }

    #[test]
    fn added_keys_go_last_and_removing_them_restores_the_document() {
        let raw = b"{\n  \"b\": 1,\n  \"a\": 2\n}\n";
        let mut doc = parse(raw).unwrap();
        doc.entry("hooks", Json::Object(vec![])).unwrap();
        assert_eq!(
            render(&doc, raw).unwrap(),
            b"{\n  \"b\": 1,\n  \"a\": 2,\n  \"hooks\": {}\n}\n"
        );
        doc.remove("hooks");
        assert_eq!(render(&doc, raw).unwrap(), raw);
    }
}
