use std::io::{self, Write};
use std::process::ExitCode;

use clap::Parser;

#[allow(dead_code)]
mod actions;
mod backup;
#[allow(dead_code)]
mod capture;
mod catalog;
mod cli;
mod commands;
mod documents;
mod due;
mod files;
mod fingerprint;
mod ops;
mod state;
mod storage;
#[allow(dead_code)]
mod unix;

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
