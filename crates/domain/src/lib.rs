//! Pure backup policy: schedules, durations, and later manifest checks.
//! This crate does not touch the filesystem or spawn processes.

#![forbid(unsafe_code)]

pub mod config;
pub mod duration;
pub mod error;
pub mod identity;
pub mod manifest;
pub mod names;
pub mod paths;
pub mod schedule;
pub mod yaml;

pub const VERSION: &str = env!("CARGO_PKG_VERSION");
