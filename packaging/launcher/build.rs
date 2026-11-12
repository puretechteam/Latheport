//! Gives `latheport-cli.exe` Latheport's icon: a resource script compiled with the Windows SDK's
//! rc.exe and linked in. Without the SDK the exe is built without an icon.

use std::path::PathBuf;
use std::process::Command;

fn rc_exe() -> Option<PathBuf> {
    let kits = PathBuf::from(r"C:\Program Files (x86)\Windows Kits\10\bin");
    let mut found: Vec<PathBuf> = std::fs::read_dir(&kits)
        .ok()?
        .filter_map(|e| e.ok().map(|e| e.path().join("x64").join("rc.exe")))
        .filter(|p| p.is_file())
        .collect();
    found.sort();
    found.pop()
}

fn main() {
    let manifest = PathBuf::from(std::env::var("CARGO_MANIFEST_DIR").unwrap());
    let icon = manifest.join("..").join("..").join("latheport").join("assets").join("icon.ico");
    println!("cargo:rerun-if-changed={}", icon.display());
    let Some(rc) = rc_exe() else {
        println!("cargo:warning=rc.exe (Windows SDK) not found: latheport-cli.exe gets no icon");
        return;
    };
    let out = PathBuf::from(std::env::var("OUT_DIR").unwrap());
    let script = out.join("launcher.rc");
    std::fs::write(&script, format!("1 ICON \"{}\"\n", icon.display().to_string().replace('\\', "\\\\"))).unwrap();
    let res = out.join("launcher.res");
    let ok = Command::new(rc).args(["/nologo", "/fo"]).arg(&res).arg(&script).status().map(|s| s.success());
    if matches!(ok, Ok(true)) {
        println!("cargo:rustc-link-arg-bins={}", res.display());
    } else {
        println!("cargo:warning=rc.exe failed: latheport-cli.exe gets no icon");
    }
}
