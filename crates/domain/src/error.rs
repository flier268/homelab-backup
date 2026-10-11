use thiserror::Error;

/// A policy failure whose text matches the existing Python `ValueError`.
#[derive(Debug, Error, Clone, PartialEq, Eq)]
#[error("{message}")]
pub struct PolicyError {
    pub message: String,
}

impl PolicyError {
    pub fn new(message: impl Into<String>) -> Self {
        Self {
            message: message.into(),
        }
    }
}
