use std::collections::BTreeMap;

use homelab_backup_domain::config::validate_global_config;
use homelab_backup_domain::manifest::{requires_docker, service_label, validate_manifest};
use homelab_backup_domain::yaml::Yaml;

fn string(value: &str) -> Yaml {
    Yaml::string(value)
}

fn demo() -> BTreeMap<String, Yaml> {
    let Yaml::Map(map) = Yaml::map([
        ("_path", string("/srv/stacks/demo/backup.yaml")),
        ("_dir", string("/srv/stacks/demo")),
        ("version", Yaml::Int(1)),
        ("service", string("demo")),
        ("schedule", Yaml::map([("cron", string("0 0 * * *"))])),
        ("retention", Yaml::map([("keep_last", Yaml::Int(1))])),
        ("consistency", Yaml::map([("mode", string("external"))])),
        (
            "sources",
            Yaml::map([("paths", Yaml::seq([])), ("volumes", Yaml::seq([]))]),
        ),
    ]) else {
        unreachable!("demo manifest is a map");
    };
    map
}

fn manifest(overrides: BTreeMap<String, Yaml>) -> Yaml {
    let mut value = demo();
    value.extend(overrides);
    Yaml::Map(value)
}

fn assert_manifest_err(value: Yaml, pattern: &str) {
    let error = validate_manifest(&value).expect_err(pattern);
    assert!(
        error.message.contains(pattern),
        "expected {pattern:?} in {}",
        error.message
    );
}

#[test]
fn files_mode_rejects_docker_sections() {
    for mode in ["stop", "hooks", "external", "live", "snapshot"] {
        let mut value = demo();
        value.insert(
            "consistency".to_owned(),
            Yaml::map([("mode", string(mode))]),
        );
        assert!(requires_docker(&value), "{mode}");
    }
    let mut files = demo();
    files.insert(
        "consistency".to_owned(),
        Yaml::map([("mode", string("files"))]),
    );
    files.insert(
        "sources".to_owned(),
        Yaml::map([
            (
                "paths",
                Yaml::seq([Yaml::map([
                    ("id", string("data")),
                    ("path", string("data")),
                ])]),
            ),
            ("volumes", Yaml::seq([])),
        ]),
    );
    assert!(!requires_docker(&files));
    validate_manifest(&Yaml::Map(files.clone())).unwrap();

    let mut with_compose = files.clone();
    with_compose.insert(
        "compose".to_owned(),
        Yaml::map([("files", Yaml::seq([string("compose.yaml")]))]),
    );
    assert_manifest_err(Yaml::Map(with_compose), "compose is not supported");
    let mut compose_null = files.clone();
    compose_null.insert("compose".to_owned(), Yaml::Null);
    assert_manifest_err(Yaml::Map(compose_null), "compose is not supported");
    let mut volumes = files.clone();
    volumes.insert(
        "sources".to_owned(),
        Yaml::map([(
            "volumes",
            Yaml::seq([Yaml::map([
                ("id", string("data")),
                ("name", string("demo_data")),
            ])]),
        )]),
    );
    assert_manifest_err(Yaml::Map(volumes), "volumes is not supported");
    let mut hooks = files;
    hooks.insert(
        "consistency".to_owned(),
        Yaml::map([
            ("mode", string("files")),
            ("before", Yaml::seq([string("systemctl stop demo")])),
        ]),
    );
    assert_manifest_err(Yaml::Map(hooks), "only valid with mode hooks");
}

#[test]
fn validation_order_and_schema_edges() {
    let mut invalid_mode = demo();
    invalid_mode.insert(
        "consistency".to_owned(),
        Yaml::map([("mode", string("invalid"))]),
    );
    invalid_mode.insert("compose".to_owned(), Yaml::seq([]));
    assert_manifest_err(Yaml::Map(invalid_mode), "invalid consistency.mode");

    for version in [None, Some(Yaml::Int(2)), Some(Yaml::Bool(true))] {
        let mut value = demo();
        match version {
            None => {
                value.remove("version");
            }
            Some(version) => {
                value.insert("version".to_owned(), version);
            }
        }
        assert_manifest_err(Yaml::Map(value), "version must be 1");
    }

    let mut impossible = demo();
    impossible.insert(
        "schedule".to_owned(),
        Yaml::map([("cron", string("0 0 31 2 *"))]),
    );
    assert_manifest_err(Yaml::Map(impossible), "never matches");

    let mut service = demo();
    service.insert("service".to_owned(), Yaml::Int(123));
    assert!(validate_manifest(&Yaml::Map(service)).is_err());

    let mut retention = demo();
    retention.insert(
        "retention".to_owned(),
        Yaml::map([("keep_last", Yaml::Bool(true))]),
    );
    assert_manifest_err(
        Yaml::Map(retention),
        "retention.keep_last must be an integer >= 0",
    );

    let mut zero = demo();
    zero.insert(
        "retention".to_owned(),
        Yaml::map([("keep_last", Yaml::Int(0))]),
    );
    assert_manifest_err(Yaml::Map(zero), "must keep at least one snapshot");

    assert_manifest_err(
        manifest(BTreeMap::from([("sources".to_owned(), Yaml::seq([]))])),
        "sources must be a mapping",
    );
    for unsafe_id in ["/etc", "../etc", "a/b", ".", ".."] {
        let value = manifest(BTreeMap::from([(
            "sources".to_owned(),
            Yaml::map([(
                "paths",
                Yaml::seq([Yaml::map([
                    ("id", string(unsafe_id)),
                    ("path", string("data")),
                ])]),
            )]),
        )]));
        assert!(validate_manifest(&value).is_err(), "{unsafe_id}");
    }
}

#[test]
fn sources_paths_and_volumes() {
    let valid = manifest(BTreeMap::from([(
        "sources".to_owned(),
        Yaml::map([(
            "paths",
            Yaml::seq([Yaml::map([
                ("id", string("data")),
                ("path", string("data")),
                (
                    "include",
                    Yaml::seq([string("world/**"), string("server.properties")]),
                ),
            ])]),
        )]),
    )]));
    validate_manifest(&valid).unwrap();
    for include in [
        Yaml::seq([]),
        string("world/**"),
        Yaml::seq([Yaml::Int(1)]),
        Yaml::seq([string("")]),
    ] {
        let value = manifest(BTreeMap::from([(
            "sources".to_owned(),
            Yaml::map([(
                "paths",
                Yaml::seq([Yaml::map([
                    ("id", string("data")),
                    ("path", string("data")),
                    ("include", include),
                ])]),
            )]),
        )]));
        assert_manifest_err(value, "include must be");
    }

    for source_path in [".", "backup.yaml"] {
        let value = manifest(BTreeMap::from([
            (
                "compose".to_owned(),
                Yaml::map([("files", Yaml::seq([string("config/compose.yaml")]))]),
            ),
            (
                "sources".to_owned(),
                Yaml::map([(
                    "paths",
                    Yaml::seq([Yaml::map([
                        ("id", string("data")),
                        ("path", string(source_path)),
                    ])]),
                )]),
            ),
        ]));
        assert_manifest_err(value, "manifest");
    }
    let compose_parent = manifest(BTreeMap::from([
        (
            "compose".to_owned(),
            Yaml::map([("files", Yaml::seq([string("config/compose.yaml")]))]),
        ),
        (
            "sources".to_owned(),
            Yaml::map([(
                "paths",
                Yaml::seq([Yaml::map([
                    ("id", string("data")),
                    ("path", string("config")),
                ])]),
            )]),
        ),
    ]));
    assert_manifest_err(compose_parent, "Compose control");

    let deferred = manifest(BTreeMap::from([
        (
            "compose".to_owned(),
            Yaml::map([
                ("files", Yaml::seq([string("compose.yaml")])),
                ("env_file", string("compose.env")),
            ]),
        ),
        (
            "sources".to_owned(),
            Yaml::map([(
                "paths",
                Yaml::seq([
                    Yaml::map([("id", string("compose")), ("path", string("compose.yaml"))]),
                    Yaml::map([("id", string("env")), ("path", string("compose.env"))]),
                ]),
            )]),
        ),
    ]));
    validate_manifest(&deferred).unwrap();

    for second in ["data", "data/child"] {
        let value = manifest(BTreeMap::from([(
            "sources".to_owned(),
            Yaml::map([(
                "paths",
                Yaml::seq([
                    Yaml::map([("id", string("first")), ("path", string("./data"))]),
                    Yaml::map([("id", string("second")), ("path", string(second))]),
                ]),
            )]),
        )]));
        assert_manifest_err(value, "overlapping path target");
    }

    for unsafe_name in ["/etc", "../data", "name,readonly", "name:/dst"] {
        let value = manifest(BTreeMap::from([(
            "sources".to_owned(),
            Yaml::map([(
                "volumes",
                Yaml::seq([Yaml::map([
                    ("id", string("data")),
                    ("name", string(unsafe_name)),
                ])]),
            )]),
        )]));
        assert_manifest_err(value, "Docker volume name");
    }

    let both = manifest(BTreeMap::from([(
        "sources".to_owned(),
        Yaml::map([(
            "volumes",
            Yaml::seq([Yaml::map([
                ("id", string("data")),
                ("name", string("one")),
                ("compose_volume", string("two")),
            ])]),
        )]),
    )]));
    assert!(validate_manifest(&both).is_err());
}

#[test]
fn names_actions_and_unknown_fields() {
    for name in ["Advent of Ascension Plus-2026", "Minecraft_Server 2.0"] {
        validate_manifest(&manifest(BTreeMap::from([(
            "name".to_owned(),
            string(name),
        )])))
        .unwrap();
    }
    for name in [
        "",
        " leading",
        "trailing ",
        "two  spaces",
        "a/b",
        "line\nbreak",
    ] {
        assert_manifest_err(
            manifest(BTreeMap::from([("name".to_owned(), string(name))])),
            "name contains unsupported characters",
        );
    }
    let mut labeled = BTreeMap::new();
    labeled.insert("service".to_owned(), string("advent-plus"));
    labeled.insert("name".to_owned(), string("Advent Plus"));
    assert_eq!(service_label(&labeled), "Advent Plus [advent-plus]");

    assert_manifest_err(
        manifest(BTreeMap::from([(
            "unexpected".to_owned(),
            Yaml::Bool(true),
        )])),
        "unsupported manifest fields: ['unexpected']",
    );

    let actions = Yaml::map([
        (
            "before",
            Yaml::seq([Yaml::map([
                ("name", string("save")),
                ("command", Yaml::seq([string("curl")])),
                ("run_as", string("root")),
            ])]),
        ),
        (
            "finally",
            Yaml::seq([Yaml::map([
                ("name", string("resume")),
                ("command", Yaml::seq([string("true")])),
                ("run_as", string("2000:3000")),
            ])]),
        ),
    ]);
    validate_manifest(&manifest(BTreeMap::from([("actions".to_owned(), actions)]))).unwrap();

    let duplicate = Yaml::map([(
        "before",
        Yaml::seq([
            Yaml::map([
                ("name", string("save")),
                ("command", Yaml::seq([string("one")])),
                ("run_as", string("root")),
            ]),
            Yaml::map([
                ("name", string("save")),
                ("command", Yaml::seq([string("two")])),
                ("run_as", string("root")),
            ]),
        ]),
    )]);
    assert_manifest_err(
        manifest(BTreeMap::from([("actions".to_owned(), duplicate)])),
        "duplicate action name 'save'",
    );

    let mixed = Yaml::map([
        ("backup", Yaml::Map(BTreeMap::new())),
        ("before", Yaml::seq([])),
    ]);
    assert_manifest_err(
        manifest(BTreeMap::from([("actions".to_owned(), mixed)])),
        "cannot be mixed with legacy",
    );
    let missing_exec = Yaml::map([(
        "backup",
        Yaml::map([(
            "before",
            Yaml::seq([Yaml::map([
                ("name", string("missing-execution")),
                ("run_as", string("root")),
            ])]),
        )]),
    )]);
    assert_manifest_err(
        manifest(BTreeMap::from([("actions".to_owned(), missing_exec)])),
        "exactly one of command or script",
    );
    let not_mapping = Yaml::map([("backup", Yaml::Null)]);
    assert_manifest_err(
        manifest(BTreeMap::from([("actions".to_owned(), not_mapping)])),
        "actions.backup must be a mapping",
    );
}

fn global(root: &str) -> BTreeMap<String, Yaml> {
    let Yaml::Map(map) = Yaml::map([
        ("version", Yaml::Int(1)),
        ("host_id", string("host")),
        ("repository", string("repo")),
        ("password_file", string(&format!("{root}/password"))),
        ("rclone_config", string(&format!("{root}/rclone.conf"))),
        ("cache_root", string(&format!("{root}/cache"))),
        ("volume_helper_image", string("helper")),
        ("state_root", string(&format!("{root}/state"))),
        ("lock_file", string(&format!("{root}/lock"))),
        ("services_root", string(&format!("{root}/services"))),
        ("staging_root", string(&format!("{root}/staging"))),
        ("restore_root", string(&format!("{root}/restore"))),
    ]) else {
        unreachable!("global config is a map");
    };
    map
}

#[test]
fn global_config_policy() {
    let cfg = "/etc/homelab-backup/config.yaml";
    let mut missing_version = global("/var/lib/homelab-backup");
    missing_version.remove("version");
    let error = validate_global_config(cfg, &missing_version).unwrap_err();
    assert!(
        error.message.contains("version must be 1"),
        "{}",
        error.message
    );

    let mut unknown = global("/var/lib/homelab-backup");
    unknown.insert("unexpected".to_owned(), Yaml::Bool(true));
    let error = validate_global_config(cfg, &unknown).unwrap_err();
    assert_eq!(
        error.message,
        format!("{cfg}: unsupported fields: ['unexpected']")
    );

    let mut trusted = global("/var/lib/homelab-backup");
    trusted.insert(
        "trusted_data_roots".to_owned(),
        Yaml::seq([string("relative")]),
    );
    let validated = validate_global_config(cfg, &trusted).unwrap();
    assert!(!validated.values.contains_key("trusted_data_roots"));
    assert_eq!(
        validated.deprecation_warning.as_deref(),
        Some(
            "WARNING: /etc/homelab-backup/config.yaml: trusted_data_roots is deprecated and ignored; remove it from the configuration"
        )
    );

    let mut rclone = global("/var/lib/homelab-backup");
    rclone.insert("rclone".to_owned(), Yaml::seq([]));
    rclone.insert(
        "staging_root".to_owned(),
        string("/var/lib/homelab-backup/restore"),
    );
    let error = validate_global_config(cfg, &rclone).unwrap_err();
    assert_eq!(error.message, format!("{cfg}: rclone must be a mapping"));

    let mut overlap = global("/var/lib/homelab-backup");
    overlap.insert(
        "staging_root".to_owned(),
        string("/var/lib/homelab-backup/services"),
    );
    let error = validate_global_config(cfg, &overlap).unwrap_err();
    assert_eq!(error.message, "services_root must not overlap staging_root");

    let mut inside = global("/var/lib/homelab-backup");
    inside.insert(
        "password_file".to_owned(),
        string("/var/lib/homelab-backup/staging/password"),
    );
    let error = validate_global_config(cfg, &inside).unwrap_err();
    assert_eq!(error.message, "staging_root must not overlap password_file");

    validate_global_config(cfg, &global("/var/lib/homelab-backup")).unwrap();
}
