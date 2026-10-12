//! Lexical path checks. Nothing here looks at the filesystem.

use crate::error::PolicyError;

pub fn lexical_absolute(path: &str) -> Result<String, PolicyError> {
    let expanded = expand_user(path);
    let displayed = if expanded.is_empty() {
        ".".to_owned()
    } else {
        expanded
    };
    if !displayed.starts_with('/') {
        return Err(PolicyError::new(format!(
            "path must be absolute: {displayed}"
        )));
    }
    Ok(normpath(&displayed))
}

pub fn path_contains(root: &str, path: &str) -> Result<bool, PolicyError> {
    let root = lexical_absolute(root)?;
    let path = lexical_absolute(path)?;
    Ok(contains_normalized(&root, &path))
}

pub fn paths_overlap(left: &str, right: &str) -> Result<bool, PolicyError> {
    Ok(path_contains(left, right)? || path_contains(right, left)?)
}

pub fn join_service_path(directory: &str, value: &str) -> String {
    if value.starts_with('/') {
        value.to_owned()
    } else if directory.ends_with('/') {
        format!("{directory}{value}")
    } else {
        format!("{directory}/{value}")
    }
}

fn contains_normalized(root: &str, path: &str) -> bool {
    if root == path {
        return true;
    }
    let root_parts = components(root);
    let path_parts = components(path);
    path_parts.len() > root_parts.len() && path_parts.starts_with(&root_parts)
}

fn components(path: &str) -> Vec<&str> {
    path.split('/').filter(|part| !part.is_empty()).collect()
}

fn expand_user(path: &str) -> String {
    if path == "~" || path.starts_with("~/") {
        let home = std::env::var("HOME").unwrap_or_default();
        if home.is_empty() {
            return path.to_owned();
        }
        format!("{home}{}", &path[1..])
    } else {
        path.to_owned()
    }
}

/// POSIX `os.path.normpath`, including the two-leading-slash exception.
pub fn normpath(path: &str) -> String {
    if path.is_empty() {
        return ".".to_owned();
    }
    let mut initial_slashes: usize = usize::from(path.starts_with('/'));
    if initial_slashes == 1 && path.starts_with("//") && !path.starts_with("///") {
        initial_slashes = 2;
    }
    let mut components = Vec::new();
    for component in path.split('/') {
        if component.is_empty() || component == "." {
            continue;
        }
        let keep_parent = component != ".."
            || (initial_slashes == 0 && components.is_empty())
            || components.last().is_some_and(|last| *last == "..");
        if keep_parent {
            components.push(component);
        } else if !components.is_empty() {
            components.pop();
        }
    }
    let mut joined = components.join("/");
    if initial_slashes > 0 {
        joined.insert_str(0, &"/".repeat(initial_slashes));
    }
    if joined.is_empty() {
        ".".to_owned()
    } else {
        joined
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn normpath_matches_posix_examples() {
        assert_eq!(normpath("/a/../b"), "/b");
        assert_eq!(normpath("/../a"), "/a");
        assert_eq!(normpath("a/../../b"), "../b");
        assert_eq!(normpath("/tmp/demo/./data"), "/tmp/demo/data");
        assert_eq!(normpath("//foo/bar"), "//foo/bar");
        assert_eq!(normpath("///foo/bar"), "/foo/bar");
    }

    #[test]
    fn overlap_is_component_wise() {
        assert!(paths_overlap("/a", "/a/b").unwrap());
        assert!(!paths_overlap("/a", "/ab").unwrap());
        assert!(paths_overlap("/a/b", "/a/b").unwrap());
    }
}
