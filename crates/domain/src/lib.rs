//! Pure backup policy: schedules, durations, and later manifest checks.
//! This crate does not touch the filesystem or spawn processes.

#![forbid(unsafe_code)]

pub mod duration;
pub mod schedule;

pub const VERSION: &str = env!("CARGO_PKG_VERSION");
