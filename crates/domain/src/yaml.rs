//! JSON/YAML-shaped values with Python's bool-versus-int distinction.
//! Manifest checks run on this tree so a bool cannot pass for an integer.

use std::collections::BTreeMap;

#[derive(Debug, Clone, PartialEq)]
pub enum Yaml {
    Null,
    Bool(bool),
    Int(i64),
    Float(f64),
    String(String),
    Seq(Vec<Yaml>),
    Map(BTreeMap<String, Yaml>),
}

impl Yaml {
    pub fn string(value: impl Into<String>) -> Self {
        Self::String(value.into())
    }

    pub fn seq(items: impl IntoIterator<Item = Yaml>) -> Self {
        Self::Seq(items.into_iter().collect())
    }

    pub fn map(pairs: impl IntoIterator<Item = (impl Into<String>, Yaml)>) -> Self {
        Self::Map(
            pairs
                .into_iter()
                .map(|(key, value)| (key.into(), value))
                .collect(),
        )
    }

    pub fn as_map(&self) -> Option<&BTreeMap<String, Yaml>> {
        match self {
            Self::Map(map) => Some(map),
            _ => None,
        }
    }

    pub fn as_seq(&self) -> Option<&[Yaml]> {
        match self {
            Self::Seq(items) => Some(items),
            _ => None,
        }
    }

    pub fn as_str(&self) -> Option<&str> {
        match self {
            Self::String(value) => Some(value),
            _ => None,
        }
    }

    pub fn as_bool(&self) -> Option<bool> {
        match self {
            Self::Bool(value) => Some(*value),
            _ => None,
        }
    }

    /// Python `type(value) is int`. Booleans are not integers.
    pub fn as_int(&self) -> Option<i64> {
        match self {
            Self::Int(value) => Some(*value),
            _ => None,
        }
    }

    pub fn is_truthy(&self) -> bool {
        match self {
            Self::Null => false,
            Self::Bool(value) => *value,
            Self::Int(value) => *value != 0,
            Self::Float(value) => *value != 0.0,
            Self::String(value) => !value.is_empty(),
            Self::Seq(items) => !items.is_empty(),
            Self::Map(map) => !map.is_empty(),
        }
    }
}

pub fn python_repr(value: &Yaml) -> String {
    match value {
        Yaml::Null => "None".to_owned(),
        Yaml::Bool(true) => "True".to_owned(),
        Yaml::Bool(false) => "False".to_owned(),
        Yaml::Int(value) => value.to_string(),
        Yaml::Float(value) => format_float(*value),
        Yaml::String(value) => python_str(value),
        Yaml::Seq(items) => {
            let body = items.iter().map(python_repr).collect::<Vec<_>>().join(", ");
            format!("[{body}]")
        }
        Yaml::Map(map) => {
            let body = map
                .iter()
                .map(|(key, item)| format!("{}: {}", python_str(key), python_repr(item)))
                .collect::<Vec<_>>()
                .join(", ");
            format!("{{{body}}}")
        }
    }
}

pub fn python_str(value: &str) -> String {
    let mut escaped = String::with_capacity(value.len());
    for ch in value.chars() {
        match ch {
            '\\' => escaped.push_str("\\\\"),
            '\'' => escaped.push_str("\\'"),
            '\n' => escaped.push_str("\\n"),
            '\r' => escaped.push_str("\\r"),
            '\t' => escaped.push_str("\\t"),
            _ => escaped.push(ch),
        }
    }
    format!("'{escaped}'")
}

pub fn unknown_keys(map: &BTreeMap<String, Yaml>, allowed: &[&str]) -> Vec<String> {
    map.keys()
        .filter(|key| !allowed.contains(&key.as_str()))
        .cloned()
        .collect()
}

pub fn python_string_list(items: impl IntoIterator<Item = impl AsRef<str>>) -> String {
    let body = items
        .into_iter()
        .map(|item| python_str(item.as_ref()))
        .collect::<Vec<_>>()
        .join(", ");
    format!("[{body}]")
}

fn format_float(value: f64) -> String {
    if !value.is_finite() {
        return "None".to_owned();
    }
    let rendered = value.to_string();
    if rendered.contains(['.', 'e', 'E']) {
        rendered
    } else {
        format!("{rendered}.0")
    }
}
