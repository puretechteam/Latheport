//! `--finish-update PID STAGED APP MARKER VERSION`: the last step of an update, run from a copy of this program
//! outside the app's folder (Windows won't rename a folder a running program is in).
//!
//! 1. Wait for Latheport (PID) to close.
//! 2. Rename the app's folder aside (`<app>.old`) and the downloaded one (STAGED) into its place.
//! 3. Start the new Latheport.exe with `--updated MARKER`; it writes MARKER once its window is open.
//! 4. MARKER written (or the new one still running after two minutes): the old folder is deleted. The new one
//!    closed without writing it: it's moved aside (`<app>.failed`), the old folder goes back, and the old
//!    Latheport starts with `--update-failed VERSION` to say so.
//!
//! Every step is written to `update.log` beside MARKER.
//!
//! Windows won't rename a folder that is any running program's working folder either, and Latheport started from
//! its Start menu shortcut or by a double-click runs in its own folder. So this runs in MARKER's folder, and each
//! Latheport it starts runs in the app's folder (as the shortcut starts it), never in `<app>.old`.

use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::thread::sleep;
use std::time::{Duration, Instant};

#[link(name = "kernel32")]
extern "system" {
    fn OpenProcess(access: u32, inherit: i32, pid: u32) -> isize;
    fn WaitForSingleObject(handle: isize, ms: u32) -> u32;
    fn CloseHandle(handle: isize) -> i32;
}

const SYNCHRONIZE: u32 = 0x0010_0000;
const START_WAIT: Duration = Duration::from_secs(120);

struct Log(Option<fs::File>);

impl Log {
    fn say(&mut self, text: &str) {
        if let Some(f) = self.0.as_mut() {
            let _ = writeln!(f, "{text}");
        }
    }
}

fn wait_for_exit(pid: u32, log: &mut Log) {
    // SAFETY: a handle opened here, waited on and closed here; a pid that's gone gives 0 and no wait
    unsafe {
        let h = OpenProcess(SYNCHRONIZE, 0, pid);
        if h == 0 {
            log.say("Latheport had already closed");
            return;
        }
        let r = WaitForSingleObject(h, 60_000);
        CloseHandle(h);
        log.say(if r == 0 { "Latheport closed" } else { "Latheport didn't close in a minute: going on" });
    }
}

/// A rename, tried again for a while: a virus scanner or the indexer can hold a file for a moment.
fn rename(from: &Path, to: &Path, log: &mut Log) -> bool {
    for _ in 0..60 {
        match fs::rename(from, to) {
            Ok(()) => {
                log.say(&format!("renamed {} to {}", from.display(), to.display()));
                return true;
            }
            Err(_) => sleep(Duration::from_millis(500)),
        }
    }
    log.say(&format!("couldn't rename {} to {}", from.display(), to.display()));
    false
}

fn remove(dir: &Path, log: &mut Log) {
    for _ in 0..20 {
        if !dir.exists() || fs::remove_dir_all(dir).is_ok() {
            log.say(&format!("removed {}", dir.display()));
            return;
        }
        sleep(Duration::from_millis(500));
    }
    log.say(&format!("couldn't remove {}", dir.display()));
}

/// Written into `<app>.old` and `<app>.failed` as they are made, so only folders an update moved aside are ever
/// deleted: a folder of the user's own with that name (a backup, say) is never touched.
const ASIDE: &str = ".latheport-moved-aside";

fn ours(dir: &Path) -> bool {
    dir.join(ASIDE).is_file()
}

fn mark(dir: &Path, log: &mut Log) {
    if fs::write(dir.join(ASIDE), b"").is_err() {
        log.say(&format!("couldn't mark {} as moved aside by an update", dir.display()));
    }
}

/// Clears the way for `dir`: removes it if an update made it. False when it's the user's own and in the way.
fn clear(dir: &Path, log: &mut Log) -> bool {
    if !dir.exists() {
        return true;
    }
    if !ours(dir) {
        log.say(&format!("{} wasn't made by an update, so it is left alone and nothing is moved", dir.display()));
        return false;
    }
    remove(dir, log);
    true
}

fn beside(app: &Path, suffix: &str) -> PathBuf {
    let name = app.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_else(|| "Latheport".into());
    app.with_file_name(format!("{name}.{suffix}"))
}

fn start(app: &Path, args: &[&std::ffi::OsStr], log: &mut Log) -> Option<std::process::Child> {
    match Command::new(app.join("Latheport.exe")).args(args).current_dir(app).spawn() {
        Ok(child) => Some(child),
        Err(e) => {
            log.say(&format!("couldn't start {}: {e}", app.join("Latheport.exe").display()));
            None
        }
    }
}

pub fn finish(args: &[std::ffi::OsString]) -> i32 {
    if args.len() < 5 {
        eprintln!("--finish-update PID STAGED APP MARKER VERSION");
        return 2;
    }
    let pid: u32 = args[0].to_string_lossy().parse().unwrap_or(0);
    let (staged, app, marker) = (PathBuf::from(&args[1]), PathBuf::from(&args[2]), PathBuf::from(&args[3]));
    let version = args[4].clone();
    let mut log = Log(marker.parent().and_then(|d| fs::File::create(d.join("update.log")).ok()));
    if let Some(work) = marker.parent() {
        let _ = std::env::set_current_dir(work); // out of the app's folder, whatever folder this was started in
    }
    log.say(&format!("updating {} to {}", app.display(), version.to_string_lossy()));
    if !staged.join("Latheport.exe").is_file() {
        log.say("the new version has no Latheport.exe: nothing done");
        return 1;
    }
    wait_for_exit(pid, &mut log);
    let old = beside(&app, "old");
    if !clear(&old, &mut log) || !rename(&app, &old, &mut log) {
        start(&app, &["--update-failed".as_ref(), version.as_os_str()], &mut log);
        return 1;
    }
    mark(&old, &mut log);
    if !rename(&staged, &app, &mut log) {
        if rename(&old, &app, &mut log) {
            start(&app, &["--update-failed".as_ref(), version.as_os_str()], &mut log);
        } else {
            log.say(&format!("THE OLD VERSION COULDN'T BE PUT BACK: it is in {}", old.display()));
        }
        return 1;
    }
    let _ = fs::remove_file(&marker);
    let Some(mut child) = start(&app, &["--updated".as_ref(), marker.as_os_str()], &mut log) else {
        return roll_back(&app, &old, &version, &mut log);
    };
    let began = Instant::now();
    loop {
        if marker.exists() {
            log.say("the new version opened");
            break;
        }
        if let Ok(Some(status)) = child.try_wait() {
            if !marker.exists() {
                log.say(&format!("the new version closed before opening ({status})"));
                return roll_back(&app, &old, &version, &mut log);
            }
            break;
        }
        if began.elapsed() > START_WAIT {
            log.say("the new version is still starting after two minutes: keeping it");
            break;
        }
        sleep(Duration::from_millis(200));
    }
    remove(&old, &mut log);
    0
}

fn roll_back(app: &Path, old: &Path, version: &std::ffi::OsString, log: &mut Log) -> i32 {
    let failed = beside(app, "failed");
    if !clear(&failed, log) || !rename(app, &failed, log) {
        log.say(&format!("the new version couldn't be moved aside: it stays, the old one is in {}", old.display()));
        return 1;
    }
    mark(&failed, log);
    if rename(old, app, log) {
        start(app, &["--update-failed".as_ref(), version.as_os_str()], log);
        log.say("rolled back");
    } else {
        log.say(&format!("THE OLD VERSION COULDN'T BE PUT BACK: it is in {}", old.display()));
    }
    1
}
