//! A stand-in Latheport.exe for the update tests (tests/test_update.py), never shipped: it appends how it was
//! started to `started.log` beside it (and the working folder it got to `cwd.log`), writes the marker it's given with `--updated MARKER` (as Latheport does
//! once its window is open), and with a file named `crash` beside it exits at once with code 3 instead.

use std::fs::{self, OpenOptions};
use std::io::Write;
use std::path::PathBuf;

fn main() {
    let here = std::env::current_exe().ok().and_then(|p| p.parent().map(PathBuf::from)).unwrap_or_default();
    let args: Vec<String> = std::env::args().skip(1).collect();
    if let Ok(mut log) = OpenOptions::new().create(true).append(true).open(here.join("started.log")) {
        let _ = writeln!(log, "{}", args.join(" "));
    }
    if let Ok(cwd) = std::env::current_dir() {
        let _ = fs::write(here.join("cwd.log"), cwd.to_string_lossy().as_bytes()); // the folder it was started in
    }
    if here.join("crash").exists() {
        std::process::exit(3);
    }
    if let Some(i) = args.iter().position(|a| a == "--updated") {
        if let Some(marker) = args.get(i + 1) {
            std::thread::sleep(std::time::Duration::from_millis(300)); // a window takes a moment to open
            let _ = fs::write(marker, "ok");
        }
    }
    std::thread::sleep(std::time::Duration::from_millis(500));
}
