//! `convertall-cli.exe`: runs `ConvertAll.exe` (next to it) in its terminal mode, so the download holds
//! ConvertAll's code once, not once per program. ConvertAll.exe writes to this console (the
//! standard handles are passed on) and gets Ctrl+C from it; this waits for it and passes on its
//! exit code. A native program, so it adds next to nothing to the start-up (a Python one added
//! about 0.15 s).

use std::path::PathBuf;
use std::process::{exit, Command};

#[link(name = "kernel32")]
extern "system" {
    fn SetConsoleCtrlHandler(handler: Option<unsafe extern "system" fn(u32) -> i32>, add: i32) -> i32;
}

/// Ctrl+C and Ctrl+Break: handled (ignored) here; ConvertAll.exe on the same console stops its
/// conversion cleanly and this goes on waiting for it.
unsafe extern "system" fn ignore(_event: u32) -> i32 {
    1
}

fn main() {
    let here = std::env::current_exe().ok().and_then(|p| p.parent().map(PathBuf::from)).unwrap_or_default();
    let app = here.join("ConvertAll.exe");
    if !app.is_file() {
        eprintln!("ConvertAll.exe isn't next to convertall-cli.exe");
        exit(1);
    }
    let mut child = match Command::new(&app).arg("--cli").args(std::env::args_os().skip(1)).spawn() {
        Ok(child) => child,
        Err(e) => {
            eprintln!("Couldn't start ConvertAll.exe: {e}");
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
