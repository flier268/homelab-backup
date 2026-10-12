//! Run injected `btrfs` and `findmnt` commands.
//!
//! `std::process::Command` uses `posix_spawn` with `CLOEXEC_DEFAULT`, which
//! would close the directory descriptor snapshot creation passes as
//! `/proc/self/fd/N`. A no-op `pre_exec` hook forces `fork` so descriptors
//! whose `CLOEXEC` bit was cleared stay open in the child.

use std::os::fd::BorrowedFd;
use std::os::unix::process::CommandExt;
use std::process::Command;

use rustix::io::{FdFlags, fcntl_getfd, fcntl_setfd};

use crate::btrfs::{BtrfsCommand, CapturedCommand};

pub fn run_btrfs_command(command: BtrfsCommand) -> CapturedCommand {
    println!(
        "+ {}",
        command
            .argv
            .iter()
            .map(|part| shell_quote(part))
            .collect::<Vec<_>>()
            .join(" ")
    );
    if command.argv.is_empty() {
        return CapturedCommand {
            code: 127,
            stdout: String::new(),
            stderr: "empty command".to_owned(),
        };
    }
    let mut saved = Vec::new();
    for fd in &command.pass_fds {
        // The caller keeps this descriptor open for the duration of the call.
        let borrowed = unsafe { BorrowedFd::borrow_raw(*fd) };
        if let Ok(flags) = fcntl_getfd(borrowed) {
            let _ = fcntl_setfd(borrowed, flags.difference(FdFlags::CLOEXEC));
            saved.push((*fd, flags));
        }
    }
    let mut child = Command::new(&command.argv[0]);
    child.args(&command.argv[1..]);
    unsafe {
        child.pre_exec(|| Ok(()));
    }
    let output = child.output();
    for (fd, flags) in saved {
        let borrowed = unsafe { BorrowedFd::borrow_raw(fd) };
        let _ = fcntl_setfd(borrowed, flags);
    }
    match output {
        Ok(output) => {
            let stdout = String::from_utf8_lossy(&output.stdout).into_owned();
            let stderr = String::from_utf8_lossy(&output.stderr).into_owned();
            let quiet = command.argv.first().is_some_and(|name| name == "findmnt")
                || command.argv.iter().any(|arg| arg == "show");
            if !quiet {
                if !stdout.is_empty() {
                    print!("{stdout}");
                }
                if !stderr.is_empty() {
                    eprint!("{stderr}");
                }
            }
            CapturedCommand {
                code: output.status.code().unwrap_or(1),
                stdout,
                stderr,
            }
        }
        Err(error) => CapturedCommand {
            code: 127,
            stdout: String::new(),
            stderr: error.to_string(),
        },
    }
}

fn shell_quote(value: &str) -> String {
    if value.chars().all(|ch| {
        ch.is_ascii_alphanumeric()
            || matches!(
                ch,
                '_' | '-' | '.' | '/' | ':' | '=' | ',' | '{' | '}' | '$'
            )
    }) && !value.is_empty()
    {
        value.to_owned()
    } else {
        format!("'{}'", value.replace('\'', "'\\''"))
    }
}
