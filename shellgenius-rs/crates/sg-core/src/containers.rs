//! Container Tools — Toolbox & Podman as first-class agent primitives.
//!
//! Containers provide: state management, sandboxed execution,
//! persistent environments, and agent isolation.

use std::sync::LazyLock;

use serde::{Deserialize, Serialize};

use crate::exec::{exec, execute, ExecMode};
use crate::types::ExecResult;

// ---------------------------------------------------------------------------
// Enums
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum ContainerRuntime {
    Toolbox,
    Podman,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum ContainerState {
    Created,
    Running,
    Paused,
    Exited,
    Stopped,
    Unknown,
    NotFound,
}

impl ContainerState {
    pub fn from_str_lossy(s: &str) -> Self {
        match s.trim().to_lowercase().as_str() {
            "created" => Self::Created,
            "running" => Self::Running,
            "paused" => Self::Paused,
            "exited" => Self::Exited,
            "stopped" => Self::Stopped,
            _ => Self::Unknown,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum SandboxLevel {
    None,
    Toolbox,
    Workspace,
    Restricted,
    Locked,
}

// ---------------------------------------------------------------------------
// SandboxProfile
// ---------------------------------------------------------------------------

#[derive(Debug, Clone)]
pub struct SandboxProfile {
    pub level: SandboxLevel,
    pub network: String,
    pub read_only: bool,
    pub cap_drop: Vec<String>,
    pub cap_add: Vec<String>,
    pub no_new_privileges: bool,
    pub pids_limit: u32,
    pub memory: String,
    pub cpus: f64,
    pub tmpfs: Vec<String>,
    pub volumes: Vec<String>,
    pub user: String,
    pub workdir: String,
}

impl SandboxProfile {
    pub fn to_flags(&self) -> Vec<String> {
        let mut flags = Vec::new();
        if self.network != "host" {
            flags.push(format!("--network={}", self.network));
        }
        if self.read_only {
            flags.push("--read-only".into());
        }
        for cap in &self.cap_drop {
            flags.push(format!("--cap-drop={}", cap));
        }
        for cap in &self.cap_add {
            flags.push(format!("--cap-add={}", cap));
        }
        if self.no_new_privileges {
            flags.push("--security-opt=no-new-privileges".into());
        }
        if self.pids_limit > 0 {
            flags.push(format!("--pids-limit={}", self.pids_limit));
        }
        if !self.memory.is_empty() {
            flags.push(format!("--memory={}", self.memory));
        }
        if self.cpus > 0.0 {
            flags.push(format!("--cpus={}", self.cpus));
        }
        for t in &self.tmpfs {
            flags.push(format!("--tmpfs={}", t));
        }
        for v in &self.volumes {
            flags.push(format!("-v={}", v));
        }
        if !self.user.is_empty() {
            flags.push(format!("--user={}", self.user));
        }
        if !self.workdir.is_empty() {
            flags.push(format!("--workdir={}", self.workdir));
        }
        flags
    }
}

// ---------------------------------------------------------------------------
// Predefined sandbox profiles
// ---------------------------------------------------------------------------

pub static SANDBOX_PROFILES: LazyLock<std::collections::HashMap<SandboxLevel, SandboxProfile>> =
    LazyLock::new(|| {
        use SandboxLevel::*;
        let mut m = std::collections::HashMap::new();

        m.insert(None, SandboxProfile {
            level: None, network: "host".into(), read_only: false,
            cap_drop: vec![], cap_add: vec![], no_new_privileges: false,
            pids_limit: 0, memory: String::new(), cpus: 0.0,
            tmpfs: vec![], volumes: vec![], user: String::new(), workdir: String::new(),
        });

        m.insert(Toolbox, SandboxProfile {
            level: Toolbox, network: "host".into(), read_only: false,
            cap_drop: vec![], cap_add: vec![], no_new_privileges: false,
            pids_limit: 0, memory: String::new(), cpus: 0.0,
            tmpfs: vec![], volumes: vec![], user: String::new(), workdir: String::new(),
        });

        m.insert(Workspace, SandboxProfile {
            level: Workspace, network: "host".into(), read_only: false,
            cap_drop: vec![], cap_add: vec![], no_new_privileges: true,
            pids_limit: 0, memory: String::new(), cpus: 0.0,
            tmpfs: vec![], volumes: vec![], user: String::new(), workdir: String::new(),
        });

        m.insert(Restricted, SandboxProfile {
            level: Restricted, network: "none".into(), read_only: false,
            cap_drop: vec!["ALL".into()], cap_add: vec!["DAC_OVERRIDE".into()],
            no_new_privileges: true, pids_limit: 100, memory: "512m".into(), cpus: 0.0,
            tmpfs: vec![], volumes: vec![], user: String::new(), workdir: String::new(),
        });

        m.insert(Locked, SandboxProfile {
            level: Locked, network: "none".into(), read_only: true,
            cap_drop: vec!["ALL".into()], cap_add: vec![],
            no_new_privileges: true, pids_limit: 50, memory: "256m".into(), cpus: 0.5,
            tmpfs: vec!["/tmp:size=50m".into()], volumes: vec![],
            user: String::new(), workdir: String::new(),
        });

        m
    });

// ---------------------------------------------------------------------------
// ToolboxTool — static methods
// ---------------------------------------------------------------------------

pub struct ToolboxTool;

impl ToolboxTool {
    pub fn create(name: &str, image: Option<&str>, distro: Option<&str>, release: Option<&str>) -> ExecResult {
        let mut parts = vec!["toolbox".to_string(), "create".into(), "-y".into()];
        if let Some(img) = image {
            parts.push("--image".into());
            parts.push(shlex::try_quote(img).unwrap_or(img.into()).into_owned());
        }
        if let Some(d) = distro {
            parts.push("--distro".into());
            parts.push(shlex::try_quote(d).unwrap_or(d.into()).into_owned());
        }
        if let Some(r) = release {
            parts.push("--release".into());
            parts.push(shlex::try_quote(r).unwrap_or(r.into()).into_owned());
        }
        parts.push(shlex::try_quote(name).unwrap_or(name.into()).into_owned());
        execute(&parts.join(" "), Option::<&std::path::Path>::None, 120.0, 1_048_576, ExecMode::Execute, "/bin/bash")
    }

    pub fn run(name: &str, command: &str, timeout_secs: f64) -> ExecResult {
        let cmd = format!(
            "toolbox run --container {} {}",
            shlex::try_quote(name).unwrap_or(name.into()),
            command
        );
        execute(&cmd, Option::<&std::path::Path>::None, timeout_secs, 1_048_576, ExecMode::Execute, "/bin/bash")
    }

    pub fn list_containers() -> ExecResult {
        exec("toolbox list --containers")
    }

    pub fn remove(name: &str, force: bool) -> ExecResult {
        let force_flag = if force { "--force " } else { "" };
        exec(&format!(
            "toolbox rm {}{}",
            force_flag,
            shlex::try_quote(name).unwrap_or(name.into())
        ))
    }

    pub fn exists(name: &str) -> bool {
        exec(&format!(
            "podman container exists {}",
            shlex::try_quote(name).unwrap_or(name.into())
        ))
        .ok()
    }
}

// ---------------------------------------------------------------------------
// PodmanTool — static methods
// ---------------------------------------------------------------------------

pub struct PodmanTool;

impl PodmanTool {
    // --- Lifecycle ---

    pub fn create(name: &str, image: &str, profile: Option<&SandboxProfile>, command: Option<&str>) -> ExecResult {
        let mut parts = vec![
            "podman".to_string(),
            "create".into(),
            format!("--name={}", shlex::try_quote(name).unwrap_or(name.into())),
        ];
        if let Some(p) = profile {
            parts.extend(p.to_flags());
        }
        parts.push(shlex::try_quote(image).unwrap_or(image.into()).into_owned());
        if let Some(cmd) = command {
            parts.push(cmd.into());
        }
        execute(&parts.join(" "), Option::<&std::path::Path>::None, 60.0, 1_048_576, ExecMode::Execute, "/bin/bash")
    }

    pub fn start(name: &str) -> ExecResult {
        exec(&format!("podman start {}", shlex::try_quote(name).unwrap_or(name.into())))
    }

    pub fn stop(name: &str, timeout: u32) -> ExecResult {
        execute(
            &format!("podman stop --time={} {}", timeout, shlex::try_quote(name).unwrap_or(name.into())),
            Option::<&std::path::Path>::None,
            (timeout + 15) as f64,
            1_048_576,
            ExecMode::Execute,
            "/bin/bash",
        )
    }

    pub fn kill(name: &str, signal: &str) -> ExecResult {
        exec(&format!(
            "podman kill --signal={} {}",
            signal,
            shlex::try_quote(name).unwrap_or(name.into())
        ))
    }

    pub fn remove(name: &str, force: bool) -> ExecResult {
        let flags = if force { "--force " } else { "" };
        exec(&format!(
            "podman rm {}{}",
            flags,
            shlex::try_quote(name).unwrap_or(name.into())
        ))
    }

    pub fn pause(name: &str) -> ExecResult {
        exec(&format!("podman pause {}", shlex::try_quote(name).unwrap_or(name.into())))
    }

    pub fn unpause(name: &str) -> ExecResult {
        exec(&format!("podman unpause {}", shlex::try_quote(name).unwrap_or(name.into())))
    }

    // --- Execution ---

    pub fn exec_cmd(name: &str, command: &str, workdir: Option<&str>, timeout_secs: f64) -> ExecResult {
        let mut parts = vec!["podman".to_string(), "exec".into()];
        if let Some(wd) = workdir {
            parts.push(format!("--workdir={}", shlex::try_quote(wd).unwrap_or(wd.into())));
        }
        parts.push(shlex::try_quote(name).unwrap_or(name.into()).into_owned());
        parts.push(command.into());
        execute(&parts.join(" "), Option::<&std::path::Path>::None, timeout_secs, 1_048_576, ExecMode::Execute, "/bin/bash")
    }

    pub fn run_oneshot(image: &str, command: &str, profile: Option<&SandboxProfile>, timeout_secs: f64) -> ExecResult {
        let mut parts = vec!["podman".to_string(), "run".into(), "--rm".into()];
        if let Some(p) = profile {
            parts.extend(p.to_flags());
        }
        parts.push(shlex::try_quote(image).unwrap_or(image.into()).into_owned());
        parts.push(command.into());
        execute(&parts.join(" "), Option::<&std::path::Path>::None, timeout_secs, 1_048_576, ExecMode::Execute, "/bin/bash")
    }

    // --- State & Inspection ---

    pub fn ps(all: bool) -> ExecResult {
        let all_flag = if all { "--all " } else { "" };
        exec(&format!("podman ps {}--format=json", all_flag))
    }

    pub fn inspect(name: &str) -> ExecResult {
        exec(&format!("podman inspect {}", shlex::try_quote(name).unwrap_or(name.into())))
    }

    pub fn logs(name: &str, tail: Option<u32>) -> ExecResult {
        let mut cmd = format!("podman logs");
        if let Some(n) = tail {
            cmd.push_str(&format!(" --tail={}", n));
        }
        cmd.push_str(&format!(" {}", shlex::try_quote(name).unwrap_or(name.into())));
        exec(&cmd)
    }

    pub fn exists(name: &str) -> bool {
        exec(&format!(
            "podman container exists {}",
            shlex::try_quote(name).unwrap_or(name.into())
        ))
        .ok()
    }

    pub fn state(name: &str) -> ContainerState {
        let result = exec(&format!(
            "podman inspect --format={{{{.State.Status}}}} {}",
            shlex::try_quote(name).unwrap_or(name.into())
        ));
        if result.ok() {
            ContainerState::from_str_lossy(&result.stdout)
        } else {
            ContainerState::NotFound
        }
    }

    pub fn exit_code(name: &str) -> Option<i32> {
        let result = exec(&format!(
            "podman inspect --format={{{{.State.ExitCode}}}} {}",
            shlex::try_quote(name).unwrap_or(name.into())
        ));
        if result.ok() {
            result.stdout.trim().parse().ok()
        } else {
            Option::None
        }
    }

    // --- Pods ---

    pub fn pod_create(name: &str, network: &str) -> ExecResult {
        exec(&format!(
            "podman pod create --name={} --network={} --share=ipc,net,uts",
            shlex::try_quote(name).unwrap_or(name.into()),
            network
        ))
    }

    pub fn pod_start(name: &str) -> ExecResult {
        exec(&format!("podman pod start {}", shlex::try_quote(name).unwrap_or(name.into())))
    }

    pub fn pod_stop(name: &str) -> ExecResult {
        exec(&format!("podman pod stop {}", shlex::try_quote(name).unwrap_or(name.into())))
    }

    pub fn pod_remove(name: &str, force: bool) -> ExecResult {
        let flags = if force { "--force " } else { "" };
        exec(&format!(
            "podman pod rm {}{}",
            flags,
            shlex::try_quote(name).unwrap_or(name.into())
        ))
    }

    pub fn pod_ps() -> ExecResult {
        exec("podman pod ps --format=json")
    }
}

// ---------------------------------------------------------------------------
// Detection
// ---------------------------------------------------------------------------

pub fn detect_runtimes() -> std::collections::HashMap<ContainerRuntime, String> {
    let mut runtimes = std::collections::HashMap::new();
    for (rt, cmd) in [
        (ContainerRuntime::Podman, "podman"),
        (ContainerRuntime::Toolbox, "toolbox"),
    ] {
        if let Some(path) = crate::exec::which(cmd) {
            runtimes.insert(rt, path);
        }
    }
    runtimes
}

pub fn podman_version() -> Option<String> {
    let r = exec("podman --version");
    r.ok().then(|| r.stdout.trim().to_string())
}

pub fn toolbox_version() -> Option<String> {
    let r = exec("toolbox --version");
    r.ok().then(|| r.stdout.trim().to_string())
}

// ---------------------------------------------------------------------------
// SandboxExecutor — high-level interface
// ---------------------------------------------------------------------------

pub struct SandboxExecutor {
    pub has_podman: bool,
    pub has_toolbox: bool,
}

impl SandboxExecutor {
    pub fn new() -> Self {
        let runtimes = detect_runtimes();
        Self {
            has_podman: runtimes.contains_key(&ContainerRuntime::Podman),
            has_toolbox: runtimes.contains_key(&ContainerRuntime::Toolbox),
        }
    }

    pub fn run(
        &self,
        command: &str,
        sandbox: SandboxLevel,
        container_name: Option<&str>,
        image: &str,
        timeout_secs: f64,
    ) -> ExecResult {
        match sandbox {
            SandboxLevel::None => exec(command),
            SandboxLevel::Toolbox => {
                if !self.has_toolbox {
                    return ExecResult {
                        command: command.into(), exit_code: 1, stdout: String::new(),
                        stderr: "toolbox is not installed".into(), elapsed_ms: 0.0,
                        truncated: false, dry_run: false,
                    };
                }
                let name = container_name.unwrap_or("shellgenius-dev");
                if !ToolboxTool::exists(name) {
                    ToolboxTool::create(name, Option::None, Option::None, Option::None);
                }
                ToolboxTool::run(name, command, timeout_secs)
            }
            level @ (SandboxLevel::Workspace | SandboxLevel::Restricted | SandboxLevel::Locked) => {
                if !self.has_podman {
                    return ExecResult {
                        command: command.into(), exit_code: 1, stdout: String::new(),
                        stderr: "podman is not installed".into(), elapsed_ms: 0.0,
                        truncated: false, dry_run: false,
                    };
                }
                let profile = SANDBOX_PROFILES.get(&level).unwrap();
                if let Some(name) = container_name {
                    if !PodmanTool::exists(name) {
                        PodmanTool::create(name, image, Some(profile), Some("sleep infinity"));
                    }
                    if PodmanTool::state(name) != ContainerState::Running {
                        PodmanTool::start(name);
                    }
                    PodmanTool::exec_cmd(name, command, Option::None, timeout_secs)
                } else {
                    PodmanTool::run_oneshot(image, command, Some(profile), timeout_secs)
                }
            }
        }
    }

    pub fn describe_sandbox(level: SandboxLevel) -> &'static str {
        match level {
            SandboxLevel::None => "No isolation. Commands run directly on the host with full access.",
            SandboxLevel::Toolbox => "Toolbox container: home directory mounted, user identity preserved, network access, system integration.",
            SandboxLevel::Workspace => "Podman container: workspace mounted read-write, network access, no privilege escalation.",
            SandboxLevel::Restricted => "Podman container: workspace mounted read-only, NO network access, all capabilities dropped, PID limit 100, memory limit 512MB.",
            SandboxLevel::Locked => "Podman container: read-only filesystem, NO network, all capabilities dropped, no privilege escalation, PID limit 50, 256MB memory, 0.5 CPU. Only /tmp is writable (50MB tmpfs).",
        }
    }
}

impl Default for SandboxExecutor {
    fn default() -> Self {
        Self::new()
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_all_levels_defined() {
        for level in [
            SandboxLevel::None,
            SandboxLevel::Toolbox,
            SandboxLevel::Workspace,
            SandboxLevel::Restricted,
            SandboxLevel::Locked,
        ] {
            assert!(SANDBOX_PROFILES.contains_key(&level));
        }
    }

    #[test]
    fn test_none_profile_has_no_restrictions() {
        let p = &SANDBOX_PROFILES[&SandboxLevel::None];
        assert!(!p.read_only);
        assert!(p.cap_drop.is_empty());
        assert!(!p.no_new_privileges);
        assert_eq!(p.pids_limit, 0);
    }

    #[test]
    fn test_locked_profile_is_maximally_restrictive() {
        let p = &SANDBOX_PROFILES[&SandboxLevel::Locked];
        assert!(p.read_only);
        assert_eq!(p.network, "none");
        assert!(p.cap_drop.contains(&"ALL".to_string()));
        assert!(p.no_new_privileges);
        assert!(p.pids_limit > 0);
        assert!(!p.memory.is_empty());
        assert!(p.cpus > 0.0);
        assert!(!p.tmpfs.is_empty());
    }

    #[test]
    fn test_restricted_has_no_network() {
        let p = &SANDBOX_PROFILES[&SandboxLevel::Restricted];
        assert_eq!(p.network, "none");
        assert!(p.cap_drop.contains(&"ALL".to_string()));
    }

    #[test]
    fn test_workspace_has_network() {
        let p = &SANDBOX_PROFILES[&SandboxLevel::Workspace];
        assert_eq!(p.network, "host");
    }

    #[test]
    fn test_profiles_are_ordered_by_restriction() {
        let none = &SANDBOX_PROFILES[&SandboxLevel::None];
        let workspace = &SANDBOX_PROFILES[&SandboxLevel::Workspace];
        let restricted = &SANDBOX_PROFILES[&SandboxLevel::Restricted];
        let locked = &SANDBOX_PROFILES[&SandboxLevel::Locked];
        assert!(!none.no_new_privileges);
        assert!(workspace.no_new_privileges);
        assert!(restricted.pids_limit > 0);
        assert!(locked.pids_limit < restricted.pids_limit);
        assert!(locked.read_only && !restricted.read_only);
    }

    #[test]
    fn test_locked_to_flags() {
        let flags = SANDBOX_PROFILES[&SandboxLevel::Locked].to_flags();
        assert!(flags.contains(&"--network=none".to_string()));
        assert!(flags.contains(&"--read-only".to_string()));
        assert!(flags.contains(&"--cap-drop=ALL".to_string()));
        assert!(flags.contains(&"--security-opt=no-new-privileges".to_string()));
        assert!(flags.iter().any(|f| f.starts_with("--pids-limit=")));
        assert!(flags.iter().any(|f| f.starts_with("--memory=")));
        assert!(flags.iter().any(|f| f.starts_with("--cpus=")));
        assert!(flags.iter().any(|f| f.starts_with("--tmpfs=")));
    }

    #[test]
    fn test_none_to_flags_is_empty() {
        let flags = SANDBOX_PROFILES[&SandboxLevel::None].to_flags();
        assert!(flags.is_empty());
    }

    #[test]
    fn test_container_state_from_str() {
        assert_eq!(ContainerState::from_str_lossy("running"), ContainerState::Running);
        assert_eq!(ContainerState::from_str_lossy("exited"), ContainerState::Exited);
        assert_eq!(ContainerState::from_str_lossy("  Created  "), ContainerState::Created);
        assert_eq!(ContainerState::from_str_lossy("bogus"), ContainerState::Unknown);
    }

    #[test]
    fn test_sandbox_executor_none_runs_directly() {
        let executor = SandboxExecutor { has_podman: false, has_toolbox: false };
        let result = executor.run("echo hello", SandboxLevel::None, Option::None, "ubuntu", 30.0);
        assert!(result.ok());
        assert!(result.stdout.contains("hello"));
    }

    #[test]
    fn test_sandbox_executor_toolbox_without_runtime_errors() {
        let executor = SandboxExecutor { has_podman: false, has_toolbox: false };
        let result = executor.run("echo test", SandboxLevel::Toolbox, Option::None, "ubuntu", 30.0);
        assert!(!result.ok());
        assert!(result.stderr.contains("not installed"));
    }

    #[test]
    fn test_sandbox_executor_podman_without_runtime_errors() {
        let executor = SandboxExecutor { has_podman: false, has_toolbox: false };
        let result = executor.run("echo test", SandboxLevel::Locked, Option::None, "ubuntu", 30.0);
        assert!(!result.ok());
        assert!(result.stderr.contains("not installed"));
    }
}
