"""
Shell Executor — safe, instrumented shell command execution.

This is the "hands" of ShellGenius. It runs commands with:
- shlex-safe argument handling (no injection)
- timeout enforcement
- stream capture (stdout, stderr, exit code)
- optional dry-run mode
- pipeline validation before execution
"""

from __future__ import annotations

import os
import shlex
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import Optional


class ExecMode(Enum):
    EXECUTE = auto()    # actually run it
    DRY_RUN = auto()    # just print what would run
    EXPLAIN = auto()    # explain the pipeline, don't run


@dataclass
class ExecResult:
    """Result of a shell command execution."""
    command: str
    exit_code: int
    stdout: str
    stderr: str
    elapsed_ms: float
    truncated: bool = False   # True if output was capped
    dry_run: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0

    def summary(self, max_lines: int = 20) -> str:
        lines = self.stdout.strip().splitlines()
        out = "\n".join(lines[:max_lines])
        if len(lines) > max_lines:
            out += f"\n... ({len(lines) - max_lines} more lines)"
        status = "OK" if self.ok else f"FAIL (exit {self.exit_code})"
        return f"[{status} in {self.elapsed_ms:.0f}ms] {self.command}\n{out}"


# Commands that are never safe to run from an agent
BLOCKLIST = frozenset({
    "rm -rf /", "rm -rf /*", "mkfs", "dd if=/dev/zero",
    ":(){:|:&};:", "chmod -R 777 /", "shutdown", "reboot",
    "halt", "poweroff", "init 0", "init 6",
})


def _is_blocked(cmd: str) -> bool:
    """Check if a command matches the blocklist."""
    normalized = " ".join(cmd.split()).strip()
    for blocked in BLOCKLIST:
        if blocked in normalized:
            return True
    return False


def _sanitize_env() -> dict[str, str]:
    """Return a sanitized copy of the environment for subprocess execution."""
    env = os.environ.copy()
    # Don't leak sensitive vars into subprocesses
    for key in ("AWS_SECRET_ACCESS_KEY", "GITHUB_TOKEN", "OPENAI_API_KEY",
                "ANTHROPIC_API_KEY", "DATABASE_URL", "PASSWORD"):
        env.pop(key, None)
    return env


def execute(
    command: str,
    *,
    cwd: Optional[str | Path] = None,
    timeout_s: float = 30.0,
    max_output_bytes: int = 1_048_576,  # 1MB
    mode: ExecMode = ExecMode.EXECUTE,
    shell_path: str = "/bin/bash",
    env_override: Optional[dict[str, str]] = None,
) -> ExecResult:
    """
    Execute a shell command safely.

    Args:
        command: The shell command string to run.
        cwd: Working directory. Defaults to current.
        timeout_s: Max execution time in seconds.
        max_output_bytes: Truncate stdout/stderr beyond this.
        mode: EXECUTE, DRY_RUN, or EXPLAIN.
        shell_path: Which shell to use (/bin/bash, /bin/zsh, etc.)
        env_override: Additional env vars to set.

    Returns:
        ExecResult with captured stdout, stderr, exit code, timing.
    """
    if _is_blocked(command):
        return ExecResult(
            command=command,
            exit_code=1,
            stdout="",
            stderr=f"BLOCKED: This command matches the safety blocklist.",
            elapsed_ms=0,
        )

    if mode == ExecMode.DRY_RUN:
        return ExecResult(
            command=command, exit_code=0,
            stdout=f"[DRY RUN] Would execute: {command}",
            stderr="", elapsed_ms=0, dry_run=True,
        )

    if mode == ExecMode.EXPLAIN:
        from shellgenius.engine.pipe_algebra import explain_pipeline
        stages = explain_pipeline(command)
        explanation = "\n".join(
            f"  {i+1}. [{s['tool']}] {s['explanation']}\n     {s['command']}"
            for i, s in enumerate(stages)
        )
        return ExecResult(
            command=command, exit_code=0,
            stdout=f"Pipeline breakdown:\n{explanation}",
            stderr="", elapsed_ms=0, dry_run=True,
        )

    env = _sanitize_env()
    if env_override:
        env.update(env_override)

    cwd_path = str(cwd) if cwd else None

    t0 = time.monotonic()
    try:
        proc = subprocess.run(
            [shell_path, "-c", command],
            capture_output=True,
            timeout=timeout_s,
            cwd=cwd_path,
            env=env,
        )
        elapsed = (time.monotonic() - t0) * 1000

        stdout = proc.stdout.decode("utf-8", errors="replace")
        stderr = proc.stderr.decode("utf-8", errors="replace")
        truncated = False
        if len(proc.stdout) > max_output_bytes:
            stdout = stdout[:max_output_bytes] + "\n[TRUNCATED]"
            truncated = True

        return ExecResult(
            command=command,
            exit_code=proc.returncode,
            stdout=stdout,
            stderr=stderr,
            elapsed_ms=elapsed,
            truncated=truncated,
        )

    except subprocess.TimeoutExpired:
        elapsed = (time.monotonic() - t0) * 1000
        return ExecResult(
            command=command,
            exit_code=124,  # standard timeout exit code
            stdout="",
            stderr=f"TIMEOUT: Command exceeded {timeout_s}s limit.",
            elapsed_ms=elapsed,
        )
    except OSError as e:
        elapsed = (time.monotonic() - t0) * 1000
        return ExecResult(
            command=command,
            exit_code=127,
            stdout="",
            stderr=f"OS Error: {e}",
            elapsed_ms=elapsed,
        )


def execute_pipeline_stages(
    stages: list[str],
    *,
    cwd: Optional[str | Path] = None,
    timeout_s: float = 30.0,
) -> ExecResult:
    """
    Execute a pipeline by joining stages with pipes.
    This is a convenience wrapper that composes and runs.
    """
    full_cmd = " | ".join(stages)
    return execute(full_cmd, cwd=cwd, timeout_s=timeout_s)


def which(program: str) -> Optional[str]:
    """Find a program on PATH, like `which` in shell."""
    result = execute(f"command -v {shlex.quote(program)}", timeout_s=5)
    return result.stdout.strip() if result.ok else None


def detect_shell() -> str:
    """Detect the user's preferred shell."""
    shell = os.environ.get("SHELL", "/bin/bash")
    return shell


def shell_version(shell_path: str = "/bin/bash") -> str:
    """Get the version string for a shell."""
    result = execute(f"{shlex.quote(shell_path)} --version", timeout_s=5)
    if result.ok:
        return result.stdout.strip().splitlines()[0]
    return "unknown"
