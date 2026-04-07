//! Shell Executor — safe, instrumented shell command execution.
//!
//! Runs commands with: shlex-safe argument handling, timeout enforcement,
//! stream capture, optional dry-run mode, and blocklist safety checks.

use std::collections::HashSet;
use std::path::Path;
use std::process::Command;
use std::sync::LazyLock;
use std::time::Instant;

use serde::{Deserialize, Serialize};
use wait_timeout::ChildExt;

use crate::types::ExecResult;

// ---------------------------------------------------------------------------
// ExecMode
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum ExecMode {
    Execute,
    DryRun,
    Explain,
}

// ---------------------------------------------------------------------------
// Blocklist — commands that are never safe from an agent
// ---------------------------------------------------------------------------

static BLOCKLIST: LazyLock<HashSet<&'static str>> = LazyLock::new(|| {
    HashSet::from([
        "rm -rf /",
        "rm -rf /*",
        "mkfs",
        "dd if=/dev/zero",
        ":(){:|:&};:",
        "chmod -R 777 /",
        "shutdown",
        "reboot",
        "halt",
        "poweroff",
        "init 0",
        "init 6",
    ])
});

fn is_blocked(cmd: &str) -> bool {
    let normalized: String = cmd.split_whitespace().collect::<Vec<_>>().join(" ");
    BLOCKLIST.iter().any(|blocked| normalized.contains(blocked))
}

// ---------------------------------------------------------------------------
// Environment sanitization
// ---------------------------------------------------------------------------

static SENSITIVE_VARS: &[&str] = &[
    "AWS_SECRET_ACCESS_KEY",
    "GITHUB_TOKEN",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "DATABASE_URL",
    "PASSWORD",
];

fn sanitized_env() -> Vec<(String, String)> {
    let sensitive: HashSet<&str> = SENSITIVE_VARS.iter().copied().collect();
    std::env::vars()
        .filter(|(k, _)| !sensitive.contains(k.as_str()))
        .collect()
}

// ---------------------------------------------------------------------------
// execute() — the main entry point
// ---------------------------------------------------------------------------

pub fn execute(
    command: &str,
    cwd: Option<&Path>,
    timeout_secs: f64,
    max_output_bytes: usize,
    mode: ExecMode,
    shell_path: &str,
) -> ExecResult {
    // Safety check
    if is_blocked(command) {
        return ExecResult {
            command: command.into(),
            exit_code: 1,
            stdout: String::new(),
            stderr: "BLOCKED: This command matches the safety blocklist.".into(),
            elapsed_ms: 0.0,
            truncated: false,
            dry_run: false,
        };
    }

    match mode {
        ExecMode::DryRun => ExecResult {
            command: command.into(),
            exit_code: 0,
            stdout: format!("[DRY RUN] Would execute: {}", command),
            stderr: String::new(),
            elapsed_ms: 0.0,
            truncated: false,
            dry_run: true,
        },
        ExecMode::Explain => {
            let stages = crate::pipe::explain_pipeline(command);
            let explanation = stages
                .iter()
                .enumerate()
                .map(|(i, s)| {
                    format!("  {}. [{}] {}\n     {}", i + 1, s.tool, s.command, s.explanation)
                })
                .collect::<Vec<_>>()
                .join("\n");
            ExecResult {
                command: command.into(),
                exit_code: 0,
                stdout: format!("Pipeline breakdown:\n{}", explanation),
                stderr: String::new(),
                elapsed_ms: 0.0,
                truncated: false,
                dry_run: true,
            }
        }
        ExecMode::Execute => execute_real(command, cwd, timeout_secs, max_output_bytes, shell_path),
    }
}

fn execute_real(
    command: &str,
    cwd: Option<&Path>,
    timeout_secs: f64,
    max_output_bytes: usize,
    shell_path: &str,
) -> ExecResult {
    let env = sanitized_env();

    let mut cmd = Command::new(shell_path);
    cmd.arg("-c").arg(command);

    if let Some(dir) = cwd {
        cmd.current_dir(dir);
    }

    // Clear env and set sanitized version
    cmd.env_clear();
    for (k, v) in &env {
        cmd.env(k, v);
    }

    let t0 = Instant::now();

    let child = match cmd
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped())
        .spawn()
    {
        Ok(c) => c,
        Err(e) => {
            let elapsed = t0.elapsed().as_secs_f64() * 1000.0;
            return ExecResult {
                command: command.into(),
                exit_code: 127,
                stdout: String::new(),
                stderr: format!("OS Error: {}", e),
                elapsed_ms: elapsed,
                truncated: false,
                dry_run: false,
            };
        }
    };

    let timeout = std::time::Duration::from_secs_f64(timeout_secs);
    let mut child = child;

    match child.wait_timeout(timeout) {
        Ok(Some(status)) => {
            let elapsed = t0.elapsed().as_secs_f64() * 1000.0;
            let output = child.wait_with_output().unwrap_or_else(|_| {
                std::process::Output {
                    status,
                    stdout: Vec::new(),
                    stderr: Vec::new(),
                }
            });

            let mut stdout = String::from_utf8_lossy(&output.stdout).into_owned();
            let mut truncated = false;
            if output.stdout.len() > max_output_bytes {
                stdout.truncate(max_output_bytes);
                stdout.push_str("\n[TRUNCATED]");
                truncated = true;
            }
            let stderr = String::from_utf8_lossy(&output.stderr).into_owned();

            ExecResult {
                command: command.into(),
                exit_code: status.code().unwrap_or(1),
                stdout,
                stderr,
                elapsed_ms: elapsed,
                truncated,
                dry_run: false,
            }
        }
        Ok(None) => {
            // Timeout — kill the process
            let _ = child.kill();
            let _ = child.wait();
            let elapsed = t0.elapsed().as_secs_f64() * 1000.0;
            ExecResult {
                command: command.into(),
                exit_code: 124, // standard timeout exit code
                stdout: String::new(),
                stderr: format!("TIMEOUT: Command exceeded {:.1}s limit.", timeout_secs),
                elapsed_ms: elapsed,
                truncated: false,
                dry_run: false,
            }
        }
        Err(e) => {
            let elapsed = t0.elapsed().as_secs_f64() * 1000.0;
            ExecResult {
                command: command.into(),
                exit_code: 127,
                stdout: String::new(),
                stderr: format!("OS Error: {}", e),
                elapsed_ms: elapsed,
                truncated: false,
                dry_run: false,
            }
        }
    }
}

// ---------------------------------------------------------------------------
// Convenience wrappers
// ---------------------------------------------------------------------------

/// Execute with sensible defaults (30s timeout, 1MB output cap, bash, cwd=.)
pub fn exec(command: &str) -> ExecResult {
    execute(command, None, 30.0, 1_048_576, ExecMode::Execute, "/bin/bash")
}

/// Execute a pipeline by joining stages with pipes.
pub fn execute_pipeline_stages(stages: &[&str], cwd: Option<&Path>, timeout_secs: f64) -> ExecResult {
    let full_cmd = stages.join(" | ");
    execute(&full_cmd, cwd, timeout_secs, 1_048_576, ExecMode::Execute, "/bin/bash")
}

/// Find a program on PATH, like `which` in shell.
pub fn which(program: &str) -> Option<String> {
    let quoted = shlex::try_quote(program).ok()?;
    let result = exec(&format!("command -v {}", quoted));
    if result.ok() {
        Some(result.stdout.trim().to_string())
    } else {
        None
    }
}

/// Detect the user's preferred shell.
pub fn detect_shell() -> String {
    std::env::var("SHELL").unwrap_or_else(|_| "/bin/bash".into())
}

/// Get the version string for a shell.
pub fn shell_version(shell_path: &str) -> String {
    let quoted = shlex::try_quote(shell_path)
        .map(|s| s.into_owned())
        .unwrap_or_else(|_| shell_path.to_string());
    let result = execute(
        &format!("{} --version", quoted),
        None,
        5.0,
        4096,
        ExecMode::Execute,
        "/bin/bash",
    );
    if result.ok() {
        result
            .stdout
            .lines()
            .next()
            .unwrap_or("unknown")
            .trim()
            .to_string()
    } else {
        "unknown".into()
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_exec_echo() {
        let r = exec("echo hello");
        assert!(r.ok());
        assert_eq!(r.stdout.trim(), "hello");
    }

    #[test]
    fn test_blocklist() {
        let r = exec("rm -rf /");
        assert!(!r.ok());
        assert!(r.stderr.contains("BLOCKED"));
    }

    #[test]
    fn test_dry_run() {
        let r = execute("echo hello", None, 30.0, 1_048_576, ExecMode::DryRun, "/bin/bash");
        assert!(r.dry_run);
        assert!(r.stdout.contains("DRY RUN"));
    }

    #[test]
    fn test_which_finds_bash() {
        let path = which("bash");
        assert!(path.is_some());
        assert!(path.unwrap().contains("bash"));
    }

    #[test]
    fn test_detect_shell() {
        let shell = detect_shell();
        assert!(!shell.is_empty());
    }

    #[test]
    fn test_shell_version() {
        let ver = shell_version("/bin/bash");
        assert_ne!(ver, "unknown");
    }

    #[test]
    fn test_exit_code_propagation() {
        let r = exec("exit 42");
        assert_eq!(r.exit_code, 42);
        assert!(!r.ok());
    }
}
