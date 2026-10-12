//! Consistency modes. Cleanup runs before finally actions and does not hide
//! the original capture error.

use crate::unix::error::SecurityError;

type Step<'a> = &'a mut dyn FnMut() -> Result<(), SecurityError>;

pub struct CaptureIo<'a> {
    pub sync: Step<'a>,
    pub hook: &'a mut dyn FnMut(&str) -> Result<(), SecurityError>,
    pub compose: &'a mut dyn FnMut(&str) -> Result<(), SecurityError>,
    pub running: &'a mut dyn FnMut() -> Result<Vec<String>, SecurityError>,
    pub snapshot_create: Step<'a>,
    pub snapshot_cleanup: Step<'a>,
}

pub struct StageIo<'a> {
    pub before: Step<'a>,
    pub after: Step<'a>,
    pub finally: Step<'a>,
    pub write_inventory: Step<'a>,
    pub capture: CaptureIo<'a>,
    pub path_method: &'a mut Option<String>,
}

pub fn stage_service(mode: &str, io: &mut StageIo<'_>) -> Result<(), SecurityError> {
    let captured = (|| {
        (io.before)()?;
        capture_mode(mode, &mut io.capture, io.path_method)?;
        (io.after)()
    })();
    let finished = (io.finally)();
    if let Err(error) = captured {
        if let Err(cleanup) = finished {
            eprintln!("ERROR: finally actions cleanup also failed: {cleanup}");
        }
        return Err(error);
    }
    finished?;
    (io.write_inventory)()
}

fn capture_mode(
    mode: &str,
    io: &mut CaptureIo<'_>,
    path_method: &mut Option<String>,
) -> Result<(), SecurityError> {
    match mode {
        "files" => {
            *path_method = Some("best-effort".to_owned());
            (io.sync)()
        }
        "external" => {
            *path_method = Some("quiesced-copy".to_owned());
            (io.sync)()
        }
        "hooks" => {
            *path_method = Some("quiesced-copy".to_owned());
            let captured = (|| {
                (io.hook)("before")?;
                (io.sync)()
            })();
            finish_cleanup(captured, (io.hook)("after"), "after hook")
        }
        "stop" => {
            *path_method = Some("quiesced-copy".to_owned());
            let targets = (io.running)()?;
            let captured = (|| {
                if !targets.is_empty() {
                    (io.compose)("stop")?;
                }
                (io.sync)()
            })();
            let cleanup = if targets.is_empty() {
                Ok(())
            } else {
                (io.compose)("start")
            };
            finish_cleanup(captured, cleanup, "service restart")
        }
        "live" => {
            *path_method = Some("best-effort".to_owned());
            (io.sync)()
        }
        "snapshot" => {
            *path_method = Some("btrfs-snapshot".to_owned());
            let captured = (|| {
                (io.snapshot_create)()?;
                (io.sync)()
            })();
            finish_cleanup(captured, (io.snapshot_cleanup)(), "Btrfs snapshot")
        }
        _ => Err(SecurityError::runtime(format!(
            "unsupported consistency mode: {mode}"
        ))),
    }
}

fn finish_cleanup(
    captured: Result<(), SecurityError>,
    cleanup: Result<(), SecurityError>,
    label: &str,
) -> Result<(), SecurityError> {
    match (captured, cleanup) {
        (Err(error), Err(cleanup)) => {
            eprintln!("ERROR: {label} cleanup also failed: {cleanup}");
            Err(error)
        }
        (Err(error), Ok(())) => Err(error),
        (Ok(()), cleanup) => cleanup,
    }
}

pub const CONSISTENCY_MODES: &[&str] = &["hooks", "stop", "external", "live", "snapshot", "files"];

#[cfg(test)]
mod tests {
    use super::*;

    fn idle() -> impl FnMut() -> Result<(), SecurityError> {
        || Ok(())
    }

    fn run(
        mode: &str,
        events: &std::cell::RefCell<Vec<&'static str>>,
        sync_error: bool,
        finally_error: bool,
    ) -> Result<(), SecurityError> {
        let mut before = || {
            events.borrow_mut().push("before");
            Ok(())
        };
        let mut after = idle();
        let mut finally = || {
            events.borrow_mut().push("finally");
            if finally_error {
                Err(SecurityError::runtime("resume failed"))
            } else {
                Ok(())
            }
        };
        let mut inventory = || {
            events.borrow_mut().push("inventory");
            Ok(())
        };
        let mut sync = || {
            events.borrow_mut().push("sync");
            if sync_error {
                Err(SecurityError::runtime("capture failed"))
            } else {
                Ok(())
            }
        };
        let mut hook = |phase: &str| {
            if phase == "after" {
                events.borrow_mut().push("cleanup");
            }
            Ok(())
        };
        let mut compose = |command: &str| {
            if command == "start" {
                events.borrow_mut().push("cleanup");
            }
            Ok(())
        };
        let mut running = || Ok(vec!["app".to_owned()]);
        let mut snapshot_create = idle();
        let mut snapshot_cleanup = || {
            events.borrow_mut().push("cleanup");
            Ok(())
        };
        let mut method = None;
        stage_service(
            mode,
            &mut StageIo {
                before: &mut before,
                after: &mut after,
                finally: &mut finally,
                write_inventory: &mut inventory,
                path_method: &mut method,
                capture: CaptureIo {
                    sync: &mut sync,
                    hook: &mut hook,
                    compose: &mut compose,
                    running: &mut running,
                    snapshot_create: &mut snapshot_create,
                    snapshot_cleanup: &mut snapshot_cleanup,
                },
            },
        )?;
        let _ = method;
        Ok(())
    }

    #[test]
    fn every_mode_has_one_dispatch_and_cleanup_precedes_finally() {
        assert_eq!(
            CONSISTENCY_MODES
                .iter()
                .copied()
                .collect::<std::collections::BTreeSet<_>>(),
            ["external", "files", "hooks", "live", "snapshot", "stop"]
                .into_iter()
                .collect()
        );
        for mode in ["hooks", "stop", "snapshot"] {
            let events = std::cell::RefCell::new(Vec::new());
            run(mode, &events, false, false).unwrap();
            assert_eq!(
                events.borrow().as_slice(),
                ["before", "sync", "cleanup", "finally", "inventory"],
                "{mode}"
            );
        }
    }

    #[test]
    fn capture_failure_keeps_the_original_error_and_runs_finally_once() {
        let events = std::cell::RefCell::new(Vec::new());
        let error = run("external", &events, true, true).unwrap_err();
        assert!(
            error.message.contains("capture failed"),
            "{}",
            error.message
        );
        assert_eq!(events.borrow().as_slice(), ["before", "sync", "finally"]);
    }

    #[test]
    fn unknown_mode_still_runs_finally_once() {
        let events = std::cell::RefCell::new(Vec::new());
        let error = run("none", &events, false, false).unwrap_err();
        assert!(
            error.message.contains("unsupported consistency mode"),
            "{}",
            error.message
        );
        assert_eq!(events.borrow().as_slice(), ["before", "finally"]);
    }

    #[test]
    fn finally_failure_skips_inventory() {
        let events = std::cell::RefCell::new(Vec::new());
        let error = run("files", &events, false, true).unwrap_err();
        assert!(error.message.contains("resume failed"), "{}", error.message);
        assert!(!events.borrow().contains(&"inventory"));
    }
}
