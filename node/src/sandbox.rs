//! A hardened sandbox for running a user's web app as a real OS process.
//!
//! A web app can't run in the WASM sandbox, so we layer on what the OS gives us:
//! bubblewrap namespaces (user, pid, net, ipc, uts, mount) over a minimal
//! read-only system, and rlimits on file size, open files, and CPU time. The
//! hard memory cap is a cgroup v2 limit, which needs a delegated cgroup tree
//! (systemd `Delegate=yes`, or root); without one the other protections still
//! apply and we log that the memory cap is off.

// This module is the foundation of the serve execution path, which is still
// being wired up, so not everything here has a caller yet.
#![allow(dead_code)]

use std::io;
use std::os::unix::process::CommandExt;
use std::path::PathBuf;
use std::process::{Child, Command};

/// Resource ceilings for a sandboxed app.
#[derive(Debug, Clone)]
pub struct SandboxLimits {
    /// Most open file descriptors (rlimit).
    pub max_open_files: u64,
    /// Biggest single file the app may write, in bytes (rlimit; guards against a
    /// decompression bomb filling the disk).
    pub max_file_bytes: u64,
    /// CPU-seconds the app may burn before it's killed (rlimit).
    pub cpu_seconds: u64,
}

impl Default for SandboxLimits {
    fn default() -> Self {
        Self {
            max_open_files: 1024,
            max_file_bytes: 256 * 1024 * 1024,
            cpu_seconds: 3600,
        }
    }
}

/// Everything needed to launch one sandboxed app.
#[derive(Debug, Clone)]
pub struct SandboxSpec {
    /// The app's private, writable directory. Bind-mounted in as `/app` and used
    /// as the working directory.
    pub work_dir: PathBuf,
    /// The command to run, already split into program + args, e.g.
    /// `["python", "app.py"]`.
    pub command: Vec<String>,
    /// Environment variables to expose (the sandbox otherwise starts with none).
    pub env: Vec<(String, String)>,
    /// Whether the app may use the network. Off by default: a compute-style web
    /// app talks to the outside world only through the node's proxy.
    pub allow_network: bool,
    /// Resource ceilings.
    pub limits: SandboxLimits,
}

/// Is bubblewrap available? Lets us fail early with a clear message.
pub fn bwrap_available() -> bool {
    Command::new("bwrap")
        .arg("--version")
        .output()
        .map(|out| out.status.success())
        .unwrap_or(false)
}

/// Build the `bwrap` command that wraps the app, without spawning it yet.
fn build_bwrap_command(spec: &SandboxSpec) -> Command {
    let mut cmd = Command::new("bwrap");

    // Its own namespaces, so it can't see the node's processes, IPC, or network.
    cmd.arg("--unshare-user")
        .arg("--unshare-pid")
        .arg("--unshare-ipc")
        .arg("--unshare-uts")
        .arg("--unshare-cgroup");
    if !spec.allow_network {
        cmd.arg("--unshare-net");
    }

    // If the node dies, take the app down with it.
    cmd.arg("--die-with-parent").arg("--new-session");

    // Enough of a read-only system to run interpreters and load libraries.
    // /home, /root, and /var are absent, so the operator's files stay unreadable.
    cmd.arg("--ro-bind").arg("/usr").arg("/usr");
    cmd.arg("--symlink").arg("usr/bin").arg("/bin");
    cmd.arg("--symlink").arg("usr/lib").arg("/lib");
    cmd.arg("--symlink").arg("usr/lib64").arg("/lib64");
    cmd.arg("--symlink").arg("usr/sbin").arg("/sbin");
    cmd.arg("--ro-bind-try").arg("/etc").arg("/etc");

    cmd.arg("--bind").arg(&spec.work_dir).arg("/app");
    cmd.arg("--chdir").arg("/app");
    cmd.arg("--tmpfs").arg("/tmp");
    cmd.arg("--proc").arg("/proc");
    cmd.arg("--dev").arg("/dev");

    cmd.arg("--clearenv");
    for (key, value) in &spec.env {
        cmd.arg("--setenv").arg(key).arg(value);
    }

    cmd.arg("--");
    for part in &spec.command {
        cmd.arg(part);
    }
    cmd
}

// libc names setrlimit's resource argument differently per platform: a
// glibc-specific alias on Linux, a plain c_int everywhere else.
#[cfg(target_os = "linux")]
type RlimitResource = libc::__rlimit_resource_t;
#[cfg(not(target_os = "linux"))]
type RlimitResource = libc::c_int;

/// Set one rlimit (soft = hard) for the calling process.
fn set_rlimit(resource: RlimitResource, value: u64) -> io::Result<()> {
    let limit = libc::rlimit {
        rlim_cur: value,
        rlim_max: value,
    };
    let result = unsafe { libc::setrlimit(resource, &limit) };
    if result != 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(())
}

/// Apply the rlimits we can safely set on the child before it execs.
///
/// No RLIMIT_AS (it would constrain bwrap itself) and no RLIMIT_NPROC (counted
/// per real-uid system-wide); memory and process count are the cgroup's job.
fn apply_rlimits(limits: &SandboxLimits) -> io::Result<()> {
    set_rlimit(libc::RLIMIT_FSIZE, limits.max_file_bytes)?;
    set_rlimit(libc::RLIMIT_NOFILE, limits.max_open_files)?;
    set_rlimit(libc::RLIMIT_CPU, limits.cpu_seconds)?;
    Ok(())
}

/// Launch the app inside the sandbox and return the child for the caller to
/// supervise.
pub fn spawn_sandboxed(spec: &SandboxSpec) -> io::Result<Child> {
    if !bwrap_available() {
        return Err(io::Error::new(
            io::ErrorKind::NotFound,
            "bubblewrap (bwrap) is required to run web apps but was not found on PATH",
        ));
    }

    let mut cmd = build_bwrap_command(spec);
    let limits = spec.limits.clone();

    // pre_exec runs in the forked child, so the whole sandboxed tree inherits
    // the rlimits.
    unsafe {
        cmd.pre_exec(move || apply_rlimits(&limits));
    }

    cmd.spawn()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn spec_running(command: &[&str], work_dir: &std::path::Path) -> SandboxSpec {
        SandboxSpec {
            work_dir: work_dir.to_path_buf(),
            command: command.iter().map(|s| s.to_string()).collect(),
            env: vec![("PATH".to_string(), "/usr/bin:/bin".to_string())],
            allow_network: false,
            limits: SandboxLimits::default(),
        }
    }

    // Returns None when bwrap is missing, so these tests skip gracefully.
    fn run_capture(command: &[&str]) -> Option<String> {
        if !bwrap_available() {
            return None;
        }
        let work = std::env::temp_dir().join("tandem_sandbox_test_app");
        let _ = std::fs::create_dir_all(&work);

        let mut spec = spec_running(command, &work);
        spec.limits.max_file_bytes = 1024 * 1024; // 1 MiB file cap
        spec.limits.cpu_seconds = 30;

        let mut cmd = build_bwrap_command(&spec);
        let limits = spec.limits.clone();
        unsafe {
            cmd.pre_exec(move || apply_rlimits(&limits));
        }
        let output = cmd.output().expect("sandboxed command should run");
        Some(String::from_utf8_lossy(&output.stdout).trim().to_string())
    }

    #[test]
    fn bwrap_presence_check_does_not_panic() {
        let _ = bwrap_available();
    }

    #[test]
    fn runs_a_command_and_captures_output() {
        let Some(out) = run_capture(&["/usr/bin/echo", "hello-sandbox"]) else {
            return; // bwrap not available; skip
        };
        assert_eq!(out, "hello-sandbox");
    }

    #[test]
    fn blocks_network_egress() {
        let Some(out) = run_capture(&[
            "/usr/bin/sh",
            "-c",
            "getent hosts example.com >/dev/null 2>&1 && echo HAS_NET || echo NO_NET",
        ]) else {
            return;
        };
        assert_eq!(out, "NO_NET");
    }

    #[test]
    fn hides_the_host_home_directory() {
        let Some(out) = run_capture(&[
            "/usr/bin/sh",
            "-c",
            "ls /home >/dev/null 2>&1 && echo SAW_HOME || echo NO_HOME",
        ]) else {
            return;
        };
        assert_eq!(out, "NO_HOME");
    }

    #[test]
    fn caps_the_size_of_files_the_app_can_write() {
        // RLIMIT_FSIZE is 1 MiB here, so a 4 MiB write is cut off at the cap.
        let Some(out) = run_capture(&[
            "/usr/bin/sh",
            "-c",
            "dd if=/dev/zero of=/app/big bs=65536 count=64 2>/dev/null; wc -c < /app/big",
        ]) else {
            return;
        };
        assert_eq!(out.trim(), "1048576");
    }
}
