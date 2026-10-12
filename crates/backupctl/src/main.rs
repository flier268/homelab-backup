use std::io::{self, Write};
use std::process::ExitCode;

use clap::Parser;

#[allow(dead_code)]
mod actions;
mod alerts;
mod backup;
mod btrfs;
mod btrfs_exec;
#[allow(dead_code)]
mod capture;
mod catalog;
mod cli;
mod commands;
mod config_ops;
mod documents;
mod due;
mod files;
mod fingerprint;
mod ops;
mod restore_apply;
mod restore_cmd;
mod restore_inventory;
mod restore_plan;
mod state;
mod storage;
#[allow(dead_code)]
mod unix;
mod upgrade;
mod volumes;

fn main() -> ExitCode {
    let command = cli::Cli::parse();
    match cli::run(command) {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            let text = error.to_string();
            if !text.is_empty() {
                let _ = writeln!(io::stderr(), "ERROR: {text}");
            }
            ExitCode::from(error.code())
        }
    }
}
