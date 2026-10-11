use std::fmt;

/// A filesystem-policy failure. Remediable errors carry exactly one suggestion.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SecurityError {
    pub message: String,
    pub suggested_command: Option<Vec<String>>,
    pub suggested_action: Option<String>,
    runtime: bool,
}

impl SecurityError {
    pub fn invalid(message: impl Into<String>) -> Self {
        Self {
            message: message.into(),
            suggested_command: None,
            suggested_action: None,
            runtime: false,
        }
    }

    pub fn runtime(message: impl Into<String>) -> Self {
        Self {
            message: message.into(),
            suggested_command: None,
            suggested_action: None,
            runtime: true,
        }
    }

    pub fn remediable(
        message: impl Into<String>,
        suggested_command: Option<Vec<String>>,
        suggested_action: Option<String>,
    ) -> Self {
        assert_eq!(
            suggested_command.is_none(),
            suggested_action.is_some(),
            "a remediable validation error needs exactly one suggestion"
        );
        Self {
            message: message.into(),
            suggested_command,
            suggested_action,
            runtime: false,
        }
    }

    pub fn is_runtime(&self) -> bool {
        self.runtime
    }

    pub fn is_missing_path(&self) -> bool {
        self.message.starts_with("missing path:")
    }
}

impl fmt::Display for SecurityError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.message)
    }
}

impl std::error::Error for SecurityError {}

pub fn permission_error(
    message: impl Into<String>,
    operation: &str,
    path: &str,
    owner_uid: u32,
    command_allowed: bool,
) -> SecurityError {
    if !command_allowed {
        return SecurityError::remediable(
            message,
            None,
            Some(format!(
                "review this directory manually or relocate the configured path; do not automatically modify directory permissions: {path}"
            )),
        );
    }
    let command = if operation == "chown" {
        let owner = if owner_uid == 0 {
            "root".to_owned()
        } else {
            owner_uid.to_string()
        };
        vec![
            "sudo".to_owned(),
            "chown".to_owned(),
            owner,
            "--".to_owned(),
            path.to_owned(),
        ]
    } else {
        vec![
            "sudo".to_owned(),
            "chmod".to_owned(),
            "go-w".to_owned(),
            "--".to_owned(),
            path.to_owned(),
        ]
    };
    SecurityError::remediable(message, Some(command), None)
}
