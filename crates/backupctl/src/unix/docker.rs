//! Containers whose writable mounts intersect a backup target.

use homelab_backup_domain::paths::{lexical_absolute, paths_overlap};
use serde_json::Value;

use super::data::CapturedCommand;
use super::error::SecurityError;

pub fn docker_mount_users(
    target_paths: &[&str],
    volume_names: &[&str],
    include_stopped: bool,
    writable_only: bool,
    run: &mut dyn FnMut(&[String]) -> CapturedCommand,
) -> Result<Vec<String>, SecurityError> {
    if target_paths.is_empty() && volume_names.is_empty() {
        return Ok(Vec::new());
    }
    let list_flag = if include_stopped { "-aq" } else { "-q" };
    let listed = run(&["docker".to_owned(), "ps".to_owned(), list_flag.to_owned()]);
    if listed.code != 0 {
        return Err(SecurityError::runtime("cannot enumerate Docker containers"));
    }
    let ids: Vec<String> = listed
        .stdout
        .split_whitespace()
        .map(str::to_owned)
        .collect();
    if ids.is_empty() {
        return Ok(Vec::new());
    }
    let mut inspect = vec!["docker".to_owned(), "inspect".to_owned()];
    inspect.extend(ids);
    let inspected = run(&inspect);
    if inspected.code != 0 {
        return Err(SecurityError::runtime("cannot inspect Docker containers"));
    }
    let containers: Vec<Value> = serde_json::from_str(&inspected.stdout)
        .map_err(|error| SecurityError::runtime(format!("invalid Docker inspect JSON: {error}")))?;
    let targets: Vec<String> = target_paths
        .iter()
        .map(|path| lexical_absolute(path).map_err(|error| SecurityError::invalid(error.message)))
        .collect::<Result<_, _>>()?;
    let mut users = Vec::new();
    for container in containers {
        let Some(mounts) = container.get("Mounts").and_then(Value::as_array) else {
            continue;
        };
        for mount in mounts {
            if writable_only && mount.get("RW") == Some(&Value::Bool(false)) {
                continue;
            }
            let volume_hit = mount.get("Type").and_then(Value::as_str) == Some("volume")
                && mount
                    .get("Name")
                    .and_then(Value::as_str)
                    .is_some_and(|name| volume_names.contains(&name));
            let bind_hit = mount.get("Type").and_then(Value::as_str) == Some("bind")
                && mount
                    .get("Source")
                    .and_then(Value::as_str)
                    .is_some_and(|source| {
                        lexical_absolute(source).is_ok_and(|source| {
                            targets
                                .iter()
                                .any(|target| paths_overlap(&source, target).unwrap_or(false))
                        })
                    });
            if volume_hit || bind_hit {
                if let Some(id) = container
                    .get("Id")
                    .and_then(Value::as_str)
                    .or_else(|| container.get("Name").and_then(Value::as_str))
                {
                    users.push(id.to_owned());
                }
                break;
            }
        }
    }
    Ok(users)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn scripted(responses: &[&str]) -> impl FnMut(&[String]) -> CapturedCommand {
        let mut responses = responses
            .iter()
            .map(|stdout| CapturedCommand {
                code: 0,
                stdout: (*stdout).to_owned(),
            })
            .collect::<Vec<_>>()
            .into_iter();
        move |_argv| responses.next().expect("unexpected docker call")
    }

    #[test]
    fn writable_parent_bind_is_a_writer_for_nested_target() {
        let mut run = scripted(&[
            "container\n",
            r#"[{"Id": "container", "Mounts": [{"Type": "bind", "Source": "/srv/stacks/Palworld/palworld", "RW": true}]}]"#,
        ]);
        assert_eq!(
            docker_mount_users(
                &["/srv/stacks/Palworld/palworld/Pal/Saved"],
                &[],
                false,
                true,
                &mut run,
            )
            .unwrap(),
            vec!["container".to_owned()]
        );
    }

    #[test]
    fn writable_child_bind_is_a_writer_for_parent_target() {
        let mut run = scripted(&[
            "container\n",
            r#"[{"Id": "container", "Mounts": [{"Type": "bind", "Source": "/srv/data/app/cache", "RW": true}]}]"#,
        ]);
        assert_eq!(
            docker_mount_users(&["/srv/data/app"], &[], false, true, &mut run).unwrap(),
            vec!["container".to_owned()]
        );
    }

    #[test]
    fn readonly_bind_and_volume_mounts_are_not_writers() {
        let mut run = scripted(&[
            "container\n",
            r#"[{"Id": "container", "Mounts": [
                {"Type": "bind", "Source": "/srv/data/app", "RW": false},
                {"Type": "volume", "Name": "demo_data", "RW": false}
            ]}]"#,
        ]);
        assert!(
            docker_mount_users(&["/srv/data/app"], &["demo_data"], false, true, &mut run,)
                .unwrap()
                .is_empty()
        );
    }
}
