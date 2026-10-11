use std::io::{self, Write};
use std::process::ExitCode;

use clap::Parser;

#[allow(dead_code)]
mod backup;
mod cli;
#[allow(dead_code)]
mod fingerprint;
#[allow(dead_code)]
mod storage;
#[allow(dead_code)]
mod unix;

fn main() -> ExitCode {
    let command = cli::Cli::parse();
    match cli::run(command) {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            let _ = writeln!(io::stderr(), "ERROR: {error}");
            ExitCode::from(1)
        }
    }
}
