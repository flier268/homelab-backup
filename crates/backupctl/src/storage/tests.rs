use std::collections::BTreeMap;
use std::os::unix::fs::{MetadataExt, PermissionsExt};
use std::path::Path;

use homelab_backup_domain::yaml::Yaml;

use super::*;
use crate::unix::error::SecurityError;
use crate::unix::mount::MountSnapshot;

fn string(value: &str) -> Yaml {
    Yaml::string(value)
}

fn manifest(dir: &Path, paths: Yaml) -> BTreeMap<String, Yaml> {
    let Yaml::Map(map) = Yaml::map([
        (
            "_path",
            string(&dir.join("backup.yaml").display().to_string()),
        ),
        ("_dir", string(&dir.display().to_string())),
        ("version", Yaml::Int(1)),
        ("service", string("demo")),
        (
            "sources",
            Yaml::map([("paths", paths), ("volumes", Yaml::seq([]))]),
        ),
    ]) else {
        unreachable!("manifest is a map");
    };
    map
}

fn source(id: &str, path: &str, extra: &[(&str, Yaml)]) -> Yaml {
    let mut pairs = vec![("id", string(id)), ("path", string(path))];
    pairs.extend(extra.iter().map(|(key, value)| (*key, value.clone())));
    Yaml::map(pairs)
}

#[test]
fn path_filter_args_are_ordered_with_excludes_first() {
    let exclude = vec!["world/cache/**".to_owned()];
    let include = vec!["world/**".to_owned(), "server.properties".to_owned()];
    assert_eq!(
        build_path_filter_args(&exclude, &include, false),
        [
            "--exclude",
            "world/cache/**",
            "--include",
            "*/",
            "--include",
            "world/**",
            "--include",
            "server.properties",
            "--exclude",
            "*",
            "--prune-empty-dirs",
        ]
    );
    assert_eq!(
        build_path_filter_args(&exclude, &include, true)[2..4],
        ["--filter".to_owned(), "P */".to_owned()]
    );
    assert_eq!(
        build_path_filter_args(&["cache/**".to_owned()], &[], false),
        ["--exclude", "cache/**"]
    );
    assert!(build_path_filter_args(&[], &[], false).is_empty());
}

#[test]
fn path_policy_rejects_root_runtime_and_manifest_overlap() {
    let root = std::env::temp_dir().join(format!("homelab-policy-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&root);
    let dir = root.join("demo");
    std::fs::create_dir_all(&dir).unwrap();
    let value = manifest(&dir, Yaml::seq([source("data", "/", &[])]));
    let error = validate_path_source_policy(
        &BTreeMap::new(),
        &value,
        value
            .get("sources")
            .unwrap()
            .as_map()
            .unwrap()
            .get("paths")
            .unwrap()
            .as_seq()
            .unwrap()[0]
            .as_map()
            .unwrap(),
    )
    .unwrap_err();
    assert!(
        error.message.contains("filesystem root"),
        "{}",
        error.message
    );

    let data = dir.join("data");
    std::fs::create_dir(&data).unwrap();
    let value = manifest(
        &dir,
        Yaml::seq([source("data", &data.display().to_string(), &[])]),
    );
    let source_map = value
        .get("sources")
        .unwrap()
        .as_map()
        .unwrap()
        .get("paths")
        .unwrap()
        .as_seq()
        .unwrap()[0]
        .as_map()
        .unwrap();
    let error = validate_path_source_policy(
        &BTreeMap::from([(
            "staging_root".to_owned(),
            data.join("nested").display().to_string(),
        )]),
        &value,
        source_map,
    )
    .unwrap_err();
    assert!(error.message.contains("staging_root"), "{}", error.message);
    let repository = data.join("repository").display().to_string();
    let error = validate_path_source_policy(
        &BTreeMap::from([("repository".to_owned(), format!("local:{repository}"))]),
        &value,
        source_map,
    )
    .unwrap_err();
    assert!(error.message.contains("repository"), "{}", error.message);

    let value = manifest(&dir, Yaml::seq([source("data", ".", &[])]));
    let source_map = value
        .get("sources")
        .unwrap()
        .as_map()
        .unwrap()
        .get("paths")
        .unwrap()
        .as_seq()
        .unwrap()[0]
        .as_map()
        .unwrap();
    let error = validate_path_source_policy(&BTreeMap::new(), &value, source_map).unwrap_err();
    assert!(error.message.contains("manifest"), "{}", error.message);
    let _ = std::fs::remove_dir_all(&root);
}

#[test]
fn include_copies_only_selected_paths() {
    let root = std::env::temp_dir().join(format!("homelab-include-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&root);
    let dir = root.join("demo");
    let payload = dir.join("data");
    std::fs::create_dir_all(payload.join("world/region")).unwrap();
    std::fs::create_dir_all(payload.join("world/cache")).unwrap();
    std::fs::create_dir_all(payload.join("junk/nested")).unwrap();
    std::fs::write(payload.join("world/region/r.0.0.mca"), "region").unwrap();
    std::fs::write(payload.join("world/cache/drop.bin"), "drop").unwrap();
    std::fs::write(payload.join("junk/nested/drop.txt"), "drop").unwrap();
    std::fs::write(payload.join("server.properties"), "online-mode=true").unwrap();
    let value = manifest(
        &dir,
        Yaml::seq([source(
            "data",
            "data",
            &[
                (
                    "include",
                    Yaml::seq([string("world/**"), string("server.properties")]),
                ),
                ("exclude", Yaml::seq([string("world/cache/**")])),
            ],
        )]),
    );
    let stage = root.join("stage");
    sync_paths(
        &BTreeMap::new(),
        &value,
        &stage,
        &mut SyncOptions::default(),
    )
    .unwrap();
    let archived = stage.join("paths/data");
    assert!(archived.join("world/region/r.0.0.mca").is_file());
    assert!(archived.join("server.properties").is_file());
    assert!(!archived.join("world/cache").exists());
    assert!(!archived.join("junk").exists());
    let _ = std::fs::remove_dir_all(&root);
}

#[test]
fn symlink_sources_keep_the_link_and_timestamp() {
    let root = std::env::temp_dir().join(format!("homelab-linkcopy-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&root);
    let dir = root.join("demo");
    std::fs::create_dir_all(&dir).unwrap();
    let link = dir.join("link");
    std::os::unix::fs::symlink("missing", &link).unwrap();
    let value = manifest(&dir, Yaml::seq([source("link", "link", &[])]));
    let stage = root.join("stage");
    let inventory = sync_paths(
        &BTreeMap::new(),
        &value,
        &stage,
        &mut SyncOptions::default(),
    )
    .unwrap();
    assert_eq!(inventory[0].kind.as_deref(), Some("symlink"));
    let archived = stage.join("paths/link/link");
    assert!(
        archived
            .symlink_metadata()
            .unwrap()
            .file_type()
            .is_symlink()
    );
    assert_eq!(
        std::fs::read_link(&archived).unwrap(),
        std::fs::read_link(&link).unwrap()
    );
    assert_eq!(
        archived.symlink_metadata().unwrap().mtime_nsec(),
        link.symlink_metadata().unwrap().mtime_nsec()
    );
    let _ = std::fs::remove_dir_all(&root);
}

#[test]
fn optional_missing_path_is_recorded_as_missing() {
    let root = std::env::temp_dir().join(format!("homelab-missing-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&root);
    let dir = root.join("demo");
    std::fs::create_dir_all(&dir).unwrap();
    let value = manifest(
        &dir,
        Yaml::seq([source(
            "optional",
            "missing",
            &[("required", Yaml::Bool(false))],
        )]),
    );
    let inventory: Vec<PathInventory> = sync_paths(
        &BTreeMap::new(),
        &value,
        &root.join("stage"),
        &mut SyncOptions::default(),
    )
    .unwrap();
    assert_eq!(inventory.len(), 1);
    assert!(!inventory[0].present);
    assert!(inventory[0].kind.is_none());
    let _ = std::fs::remove_dir_all(&root);
}

#[test]
fn directory_copy_records_one_file_system_and_mount_change() {
    let root = std::env::temp_dir().join(format!("homelab-mount-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&root);
    let dir = root.join("demo");
    std::fs::create_dir_all(dir.join("data")).unwrap();
    let value = manifest(&dir, Yaml::seq([source("data", "data", &[])]));
    let mut calls = 0;
    let mut mounts = |_path: &str| {
        calls += 1;
        Ok(MountSnapshot {
            mount_id: calls,
            device: "8:1".to_owned(),
            mount_point: "/".to_owned(),
            nested: Vec::new(),
        })
    };
    let mut seen = Vec::new();
    let mut run = |argv: &[String]| {
        seen = argv.to_vec();
        Ok(())
    };
    let error = sync_paths(
        &BTreeMap::new(),
        &value,
        &root.join("stage"),
        &mut SyncOptions {
            run: Some(&mut run),
            mounts: Some(&mut mounts),
            capture_method: String::new(),
            before_copy: None,
            ..SyncOptions::default()
        },
    )
    .unwrap_err();
    assert!(
        error.message.contains("mount topology changed"),
        "{}",
        error.message
    );
    assert!(seen.iter().any(|arg| arg == "--one-file-system"));
    let _ = std::fs::remove_dir_all(&root);
}

#[test]
fn hardlinks_are_preserved() {
    let root = std::env::temp_dir().join(format!("homelab-hardlink-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&root);
    let dir = root.join("demo");
    let payload = dir.join("data");
    std::fs::create_dir_all(&payload).unwrap();
    std::fs::write(payload.join("first"), "shared").unwrap();
    std::fs::hard_link(payload.join("first"), payload.join("second")).unwrap();
    let value = manifest(&dir, Yaml::seq([source("data", "data", &[])]));
    sync_paths(
        &BTreeMap::new(),
        &value,
        &root.join("stage"),
        &mut SyncOptions::default(),
    )
    .unwrap();
    let archived = root.join("stage/paths/data");
    assert_eq!(
        archived.join("first").metadata().unwrap().ino(),
        archived.join("second").metadata().unwrap().ino()
    );
    let _ = std::fs::remove_dir_all(&root);
}

#[test]
fn space_check_names_the_reserve_shortfall() {
    assert_eq!(MIN_FREE_BYTES, 1024 * 1024 * 1024);
    assert_eq!(format_bytes(MIN_FREE_BYTES), "1.00 GiB");
    let error = check_backup_space("demo", "/stage", 1024, 100, false).unwrap_err();
    assert!(error.1.contains("--allow-low-space"));
    assert!(error.0[0].contains("1.00 GiB reserve"));
    let warnings = check_backup_space("demo", "/stage", 1024, 100, true).unwrap();
    assert!(warnings[1].contains("--allow-low-space"));
}

#[test]
fn staging_directory_must_not_cover_live_data_or_be_a_symlink() {
    let root = std::env::temp_dir().join(format!("homelab-stage-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&root);
    std::fs::create_dir_all(root.join("live")).unwrap();
    std::fs::set_permissions(&root, std::fs::Permissions::from_mode(0o755)).unwrap();
    let error = prepare_stage(
        &root.join("staging").display().to_string(),
        "demo",
        &[root.join("staging/demo/data").display().to_string()],
    )
    .unwrap_err();
    assert!(
        error.message.contains("overlaps protected path"),
        "{}",
        error.message
    );

    let staging = root.join("private");
    std::fs::create_dir_all(&staging).unwrap();
    std::fs::set_permissions(&staging, std::fs::Permissions::from_mode(0o755)).unwrap();
    std::os::unix::fs::symlink(root.join("elsewhere"), staging.join("demo")).unwrap();
    let error = prepare_stage(&staging.display().to_string(), "demo", &[]).unwrap_err();
    assert!(
        error.message.contains("symbolic link") || error.message.contains("real directory"),
        "{}",
        error.message
    );
    let _ = std::fs::remove_dir_all(&root);
}

#[test]
fn restic_commands_and_repository_id() {
    assert_eq!(
        snapshots_command("host", &["--tag".to_owned(), "service:demo".to_owned()]),
        [
            "restic",
            "snapshots",
            "--json",
            "--host",
            "host",
            "--tag",
            "service:demo"
        ]
    );
    let retention = BTreeMap::from([("keep_last".to_owned(), Yaml::Int(7))]);
    let command = retention_command("host", "demo", &retention, true, false);
    assert!(
        command
            .windows(2)
            .any(|pair| pair == ["--keep-last".to_owned(), "7".to_owned()])
    );
    assert!(command.iter().any(|arg| arg == "--prune"));
    let id = "ab".repeat(32);
    assert_eq!(
        parse_repository_id(&format!(r#"{{"id":"{id}"}}"#)).unwrap(),
        id
    );
    assert!(parse_repository_id(r#"{"id":"short"}"#).is_err());
    let _ = SecurityError::invalid("keep the import used");
}
