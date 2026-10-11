use std::io::{self, Write};
use std::process::ExitCode;

use clap::Parser;

mod cli;

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
