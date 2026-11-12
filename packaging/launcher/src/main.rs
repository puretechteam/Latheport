//! `latheport-cli.exe`: runs `Latheport.exe` (next to it) in its terminal mode, so the download holds
//! Latheport's code once, not once per program. Latheport.exe writes to this console (the
//! standard handles are passed on) and gets Ctrl+C from it; this waits for it and passes on its
//! exit code. A native program, so it adds next to nothing to the start-up (a Python one added
//! about 0.15 s).

//!
//! `--finish-update ...` is the last step of an update instead (update.rs), run from a copy of this program.

mod update;

use std::path::PathBuf;
use std::process::{exit, Command};

#[link(name = "kernel32")]
extern "system" {
    fn SetConsoleCtrlHandler(handler: Option<unsafe extern "system" fn(u32) -> i32>, add: i32) -> i32;
}

/// Ctrl+C and Ctrl+Break: handled (ignored) here; Latheport.exe on the same console stops its
/// conversion cleanly and this goes on waiting for it.
unsafe extern "system" fn ignore(_event: u32) -> i32 {
    1
}

fn main() {
    let args: Vec<std::ffi::OsString> = std::env::args_os().skip(1).collect();
    if args.first().is_some_and(|a| a == "--finish-update") {
        exit(update::finish(&args[1..]));
    }
    let here = std::env::current_exe().ok().and_then(|p| p.parent().map(PathBuf::from)).unwrap_or_default();
    let app = here.join("Latheport.exe");
    if !app.is_file() {
        eprintln!("Latheport.exe isn't next to latheport-cli.exe");
        exit(1);
    }
    let mut child = match Command::new(&app).arg("--cli").args(std::env::args_os().skip(1)).spawn() {
        Ok(child) => child,
        Err(e) => {
            eprintln!("Couldn't start Latheport.exe: {e}");
            exit(1);
        }
    };
    // only now: a program started while Ctrl+C is ignored would ignore it too
    // SAFETY: registers a handler function that lives as long as the program
    unsafe {
        SetConsoleCtrlHandler(Some(ignore), 1);
    }
    let code = match child.wait() {
        Ok(status) => status.code().unwrap_or(1),
        Err(_) => 1,
    };
    exit(code);
}
