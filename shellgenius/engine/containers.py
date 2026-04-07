"""
Container Tools — Toolbox & Podman as first-class agent primitives.

These are not wrappers around containers. They ARE tools — the same way
`grep` is a tool and `awk` is a tool. Toolbox and Podman provide:

1. **State management** — create, start, stop, inspect, remove
2. **Sandboxed execution** — network isolation, read-only fs, capability drops
3. **Persistent environments** — named containers that survive between commands
4. **Agent isolation** — each "agent" is a container with its own process tree

Architecture:
    ShellGenius tools run INSIDE containers managed by this module.
    The container IS the agent boundary.

    ┌─────────────────────────────────────────┐
    │ Host (ShellGenius Agent)                │
    │                                          │
    │  ┌──────────┐  ┌──────────┐  ┌────────┐ │
    │  │ Toolbox  │  │ Podman   │  │ Podman │ │
    │  │ "dev"    │  │ "sandbox"│  │ "job-1"│ │
    │  │ (rich)   │  │ (locked) │  │ (temp) │ │
    │  └──────────┘  └──────────┘  └────────┘ │
    └─────────────────────────────────────────┘
"""

from __future__ import annotations

import json
import shlex
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import Any, Optional

from shellgenius.engine.shell_executor import ExecResult, execute, ExecMode


# ---------------------------------------------------------------------------
# Enums & Types
# ---------------------------------------------------------------------------

class ContainerRuntime(Enum):
    TOOLBOX = "toolbox"
    PODMAN = "podman"


class ContainerState(Enum):
    CREATED = "created"
    RUNNING = "running"
    PAUSED = "paused"
    EXITED = "exited"
    STOPPED = "stopped"      # alias for exited in some contexts
    UNKNOWN = "unknown"
    NOT_FOUND = "not_found"


class SandboxLevel(Enum):
    """Predefined sandbox profiles, from most open to most locked."""
    NONE = "none"            # no isolation (host execution)
    TOOLBOX = "toolbox"      # toolbox: home mounted, full user env, network
    WORKSPACE = "workspace"  # podman: mount workspace rw, home ro, network
    RESTRICTED = "restricted"  # podman: mount workspace ro, no network
    LOCKED = "locked"        # podman: read-only fs, no network, no caps, pid limit


@dataclass(frozen=True)
class SandboxProfile:
    """Concrete sandbox configuration for podman run/create."""
    level: SandboxLevel
    network: str = "host"             # none | host | bridge | slirp4netns | pasta
    read_only: bool = False
    cap_drop: tuple[str, ...] = ()
    cap_add: tuple[str, ...] = ()
    no_new_privileges: bool = False
    pids_limit: int = 0               # 0 = unlimited
    memory: str = ""                  # e.g. "256m", "1g"
    cpus: float = 0.0                 # 0 = unlimited
    tmpfs: tuple[str, ...] = ()       # e.g. ("/tmp:size=100m",)
    volumes: tuple[str, ...] = ()     # e.g. ("./code:/workspace:ro",)
    user: str = ""                    # e.g. "1000:1000"
    workdir: str = ""

    def to_flags(self) -> list[str]:
        """Convert to podman CLI flags."""
        flags = []
        if self.network != "host":
            flags.append(f"--network={self.network}")
        if self.read_only:
            flags.append("--read-only")
        if self.cap_drop:
            for cap in self.cap_drop:
                flags.append(f"--cap-drop={cap}")
        if self.cap_add:
            for cap in self.cap_add:
                flags.append(f"--cap-add={cap}")
        if self.no_new_privileges:
            flags.append("--security-opt=no-new-privileges")
        if self.pids_limit > 0:
            flags.append(f"--pids-limit={self.pids_limit}")
        if self.memory:
            flags.append(f"--memory={self.memory}")
        if self.cpus > 0:
            flags.append(f"--cpus={self.cpus}")
        for t in self.tmpfs:
            flags.append(f"--tmpfs={t}")
        for v in self.volumes:
            flags.append(f"-v={v}")
        if self.user:
            flags.append(f"--user={self.user}")
        if self.workdir:
            flags.append(f"--workdir={self.workdir}")
        return flags


# ---------------------------------------------------------------------------
# Predefined sandbox profiles
# ---------------------------------------------------------------------------

SANDBOX_PROFILES: dict[SandboxLevel, SandboxProfile] = {
    SandboxLevel.NONE: SandboxProfile(
        level=SandboxLevel.NONE,
    ),
    SandboxLevel.TOOLBOX: SandboxProfile(
        level=SandboxLevel.TOOLBOX,
        network="host",
        # toolbox auto-mounts home, sets up user, etc.
    ),
    SandboxLevel.WORKSPACE: SandboxProfile(
        level=SandboxLevel.WORKSPACE,
        network="host",
        no_new_privileges=True,
    ),
    SandboxLevel.RESTRICTED: SandboxProfile(
        level=SandboxLevel.RESTRICTED,
        network="none",
        no_new_privileges=True,
        cap_drop=("ALL",),
        cap_add=("DAC_OVERRIDE",),  # needed for file access in some images
        pids_limit=100,
        memory="512m",
    ),
    SandboxLevel.LOCKED: SandboxProfile(
        level=SandboxLevel.LOCKED,
        network="none",
        read_only=True,
        cap_drop=("ALL",),
        no_new_privileges=True,
        pids_limit=50,
        memory="256m",
        cpus=0.5,
        tmpfs=("/tmp:size=50m",),
    ),
}


# ---------------------------------------------------------------------------
# Container info
# ---------------------------------------------------------------------------

@dataclass
class ContainerInfo:
    """Parsed container state from podman/toolbox."""
    name: str
    id: str
    state: ContainerState
    image: str
    created: str
    runtime: ContainerRuntime
    exit_code: Optional[int] = None
    pid: Optional[int] = None
    ports: list[str] = field(default_factory=list)
    mounts: list[str] = field(default_factory=list)

    def is_running(self) -> bool:
        return self.state == ContainerState.RUNNING

    def is_toolbox(self) -> bool:
        return self.runtime == ContainerRuntime.TOOLBOX


# ---------------------------------------------------------------------------
# Detection — what's available on this system?
# ---------------------------------------------------------------------------

def detect_runtimes() -> dict[ContainerRuntime, str]:
    """Check which container runtimes are available."""
    runtimes = {}
    for rt, cmd in [(ContainerRuntime.PODMAN, "podman"), (ContainerRuntime.TOOLBOX, "toolbox")]:
        result = execute(f"command -v {cmd}", timeout_s=5)
        if result.ok:
            runtimes[rt] = result.stdout.strip()
    return runtimes


def podman_version() -> Optional[str]:
    result = execute("podman --version", timeout_s=5)
    return result.stdout.strip() if result.ok else None


def toolbox_version() -> Optional[str]:
    result = execute("toolbox --version", timeout_s=5)
    return result.stdout.strip() if result.ok else None


# ---------------------------------------------------------------------------
# Toolbox operations
# ---------------------------------------------------------------------------

class ToolboxTool:
    """
    Toolbox as a first-class shell tool.

    Toolbox provides rich dev environments with automatic home dir mounting,
    user identity, system journal, and dev tool integration.
    """

    @staticmethod
    def create(name: str, *, image: Optional[str] = None,
               distro: Optional[str] = None, release: Optional[str] = None) -> ExecResult:
        """Create a new toolbox container."""
        parts = ["toolbox", "create", "-y"]
        if image:
            parts.extend(["--image", shlex.quote(image)])
        if distro:
            parts.extend(["--distro", shlex.quote(distro)])
        if release:
            parts.extend(["--release", shlex.quote(release)])
        parts.append(shlex.quote(name))
        return execute(" ".join(parts), timeout_s=120)

    @staticmethod
    def run(name: str, command: str, *, timeout_s: float = 30.0) -> ExecResult:
        """Execute a command inside a toolbox container."""
        cmd = f"toolbox run --container {shlex.quote(name)} {command}"
        return execute(cmd, timeout_s=timeout_s)

    @staticmethod
    def enter_cmd(name: str) -> str:
        """Return the command to enter an interactive toolbox session."""
        return f"toolbox enter --container {shlex.quote(name)}"

    @staticmethod
    def list_containers() -> ExecResult:
        """List all toolbox containers."""
        return execute("toolbox list --containers", timeout_s=10)

    @staticmethod
    def remove(name: str, *, force: bool = False) -> ExecResult:
        """Remove a toolbox container."""
        cmd = f"toolbox rm {'--force ' if force else ''}{shlex.quote(name)}"
        return execute(cmd, timeout_s=30)

    @staticmethod
    def exists(name: str) -> bool:
        """Check if a toolbox container exists."""
        result = execute(
            f"podman container exists {shlex.quote(name)}",
            timeout_s=5,
        )
        return result.ok


class PodmanTool:
    """
    Podman as a first-class shell tool.

    Podman provides fine-grained container management for:
    - Sandboxed execution (network, fs, capabilities)
    - Persistent environments (named containers)
    - One-shot jobs (--rm)
    - Process tree isolation (each container is its own PID namespace)
    """

    # ----- Lifecycle -----

    @staticmethod
    def create(name: str, image: str, *,
               profile: Optional[SandboxProfile] = None,
               command: Optional[str] = None,
               env: Optional[dict[str, str]] = None,
               volumes: Optional[list[str]] = None) -> ExecResult:
        """Create a container (does not start it)."""
        parts = ["podman", "create", f"--name={shlex.quote(name)}"]
        if profile:
            parts.extend(profile.to_flags())
        if volumes:
            for v in volumes:
                parts.append(f"-v={v}")
        if env:
            for k, v in env.items():
                parts.append(f"-e={shlex.quote(k)}={shlex.quote(v)}")
        parts.append(shlex.quote(image))
        if command:
            parts.append(command)
        return execute(" ".join(parts), timeout_s=60)

    @staticmethod
    def start(name: str) -> ExecResult:
        """Start a created/stopped container."""
        return execute(f"podman start {shlex.quote(name)}", timeout_s=30)

    @staticmethod
    def stop(name: str, *, timeout: int = 10) -> ExecResult:
        """Gracefully stop a container (SIGTERM → SIGKILL after timeout)."""
        return execute(f"podman stop --time={timeout} {shlex.quote(name)}", timeout_s=timeout + 15)

    @staticmethod
    def kill(name: str, *, signal: str = "SIGKILL") -> ExecResult:
        """Send a signal to a container's main process."""
        return execute(f"podman kill --signal={signal} {shlex.quote(name)}", timeout_s=10)

    @staticmethod
    def remove(name: str, *, force: bool = False, volumes: bool = False) -> ExecResult:
        """Remove a container."""
        flags = []
        if force:
            flags.append("--force")
        if volumes:
            flags.append("--volumes")
        return execute(f"podman rm {' '.join(flags)} {shlex.quote(name)}", timeout_s=30)

    @staticmethod
    def pause(name: str) -> ExecResult:
        """Pause (freeze) a container."""
        return execute(f"podman pause {shlex.quote(name)}", timeout_s=10)

    @staticmethod
    def unpause(name: str) -> ExecResult:
        """Unpause (thaw) a container."""
        return execute(f"podman unpause {shlex.quote(name)}", timeout_s=10)

    # ----- Execution -----

    @staticmethod
    def exec(name: str, command: str, *,
             workdir: Optional[str] = None,
             user: Optional[str] = None,
             env: Optional[dict[str, str]] = None,
             interactive: bool = False,
             tty: bool = False,
             timeout_s: float = 30.0) -> ExecResult:
        """Execute a command inside a running container."""
        parts = ["podman", "exec"]
        if interactive:
            parts.append("-i")
        if tty:
            parts.append("-t")
        if workdir:
            parts.append(f"--workdir={shlex.quote(workdir)}")
        if user:
            parts.append(f"--user={shlex.quote(user)}")
        if env:
            for k, v in env.items():
                parts.append(f"-e={shlex.quote(k)}={shlex.quote(v)}")
        parts.append(shlex.quote(name))
        parts.append(command)
        return execute(" ".join(parts), timeout_s=timeout_s)

    @staticmethod
    def run_oneshot(image: str, command: str, *,
                    profile: Optional[SandboxProfile] = None,
                    volumes: Optional[list[str]] = None,
                    env: Optional[dict[str, str]] = None,
                    timeout_s: float = 30.0) -> ExecResult:
        """Run a one-shot command in a temporary container (--rm)."""
        parts = ["podman", "run", "--rm"]
        if profile:
            parts.extend(profile.to_flags())
        if volumes:
            for v in volumes:
                parts.append(f"-v={v}")
        if env:
            for k, v in env.items():
                parts.append(f"-e={shlex.quote(k)}={shlex.quote(v)}")
        parts.append(shlex.quote(image))
        parts.append(command)
        return execute(" ".join(parts), timeout_s=timeout_s)

    # ----- State & Inspection -----

    @staticmethod
    def ps(*, all: bool = True, filter: Optional[str] = None,
            format: str = "json") -> ExecResult:
        """List containers."""
        parts = ["podman", "ps", f"--format={format}"]
        if all:
            parts.append("--all")
        if filter:
            parts.append(f"--filter={filter}")
        return execute(" ".join(parts), timeout_s=10)

    @staticmethod
    def inspect(name: str, *, format: Optional[str] = None) -> ExecResult:
        """Get detailed container metadata as JSON."""
        parts = ["podman", "inspect"]
        if format:
            parts.append(f"--format={shlex.quote(format)}")
        parts.append(shlex.quote(name))
        return execute(" ".join(parts), timeout_s=10)

    @staticmethod
    def logs(name: str, *, follow: bool = False, tail: Optional[int] = None,
             since: Optional[str] = None) -> ExecResult:
        """Get container stdout/stderr logs."""
        parts = ["podman", "logs"]
        if follow:
            parts.append("--follow")
        if tail is not None:
            parts.append(f"--tail={tail}")
        if since:
            parts.append(f"--since={shlex.quote(since)}")
        parts.append(shlex.quote(name))
        timeout = 60 if follow else 10
        return execute(" ".join(parts), timeout_s=timeout)

    @staticmethod
    def exists(name: str) -> bool:
        """Check if a container exists."""
        result = execute(f"podman container exists {shlex.quote(name)}", timeout_s=5)
        return result.ok

    @staticmethod
    def state(name: str) -> ContainerState:
        """Get the current state of a container."""
        result = execute(
            f"podman inspect --format={{{{.State.Status}}}} {shlex.quote(name)}",
            timeout_s=5,
        )
        if not result.ok:
            return ContainerState.NOT_FOUND
        raw = result.stdout.strip().lower()
        state_map = {
            "created": ContainerState.CREATED,
            "running": ContainerState.RUNNING,
            "paused": ContainerState.PAUSED,
            "exited": ContainerState.EXITED,
            "stopped": ContainerState.STOPPED,
        }
        return state_map.get(raw, ContainerState.UNKNOWN)

    @staticmethod
    def exit_code(name: str) -> Optional[int]:
        """Get the exit code of a stopped container."""
        result = execute(
            f"podman inspect --format={{{{.State.ExitCode}}}} {shlex.quote(name)}",
            timeout_s=5,
        )
        if result.ok:
            try:
                return int(result.stdout.strip())
            except ValueError:
                pass
        return None

    # ----- Pod operations (container grouping) -----

    @staticmethod
    def pod_create(name: str, *, network: str = "host",
                   share: str = "ipc,net,uts") -> ExecResult:
        """Create a pod (group of containers sharing namespaces)."""
        return execute(
            f"podman pod create --name={shlex.quote(name)} "
            f"--network={network} --share={share}",
            timeout_s=30,
        )

    @staticmethod
    def pod_start(name: str) -> ExecResult:
        return execute(f"podman pod start {shlex.quote(name)}", timeout_s=30)

    @staticmethod
    def pod_stop(name: str) -> ExecResult:
        return execute(f"podman pod stop {shlex.quote(name)}", timeout_s=30)

    @staticmethod
    def pod_remove(name: str, *, force: bool = False) -> ExecResult:
        return execute(
            f"podman pod rm {'--force ' if force else ''}{shlex.quote(name)}",
            timeout_s=30,
        )

    @staticmethod
    def pod_ps(*, format: str = "json") -> ExecResult:
        return execute(f"podman pod ps --format={format}", timeout_s=10)


# ---------------------------------------------------------------------------
# Sandboxed execution — the high-level interface
# ---------------------------------------------------------------------------

class SandboxExecutor:
    """
    Execute commands with container-backed isolation.

    This is the bridge between ShellGenius's pipe algebra and container
    sandboxing. It decides WHERE to run a command based on the sandbox level.
    """

    def __init__(self, runtimes: Optional[dict[ContainerRuntime, str]] = None):
        self.runtimes = runtimes or detect_runtimes()
        self.has_podman = ContainerRuntime.PODMAN in self.runtimes
        self.has_toolbox = ContainerRuntime.TOOLBOX in self.runtimes

    def run(self, command: str, *,
            sandbox: SandboxLevel = SandboxLevel.NONE,
            container_name: Optional[str] = None,
            image: str = "ubuntu:latest",
            volumes: Optional[list[str]] = None,
            timeout_s: float = 30.0,
            mode: ExecMode = ExecMode.EXECUTE) -> ExecResult:
        """
        Execute a command at the specified sandbox level.

        - NONE: run directly on host
        - TOOLBOX: run in a toolbox container (rich env)
        - WORKSPACE/RESTRICTED/LOCKED: run in a podman container
        """
        if mode != ExecMode.EXECUTE:
            return execute(command, mode=mode, timeout_s=timeout_s)

        if sandbox == SandboxLevel.NONE:
            return execute(command, timeout_s=timeout_s)

        if sandbox == SandboxLevel.TOOLBOX:
            if not self.has_toolbox:
                return ExecResult(
                    command=command, exit_code=1, stdout="",
                    stderr="toolbox is not installed", elapsed_ms=0,
                )
            name = container_name or "shellgenius-dev"
            if not ToolboxTool.exists(name):
                ToolboxTool.create(name)
            return ToolboxTool.run(name, command, timeout_s=timeout_s)

        # Podman-based sandbox levels
        if not self.has_podman:
            return ExecResult(
                command=command, exit_code=1, stdout="",
                stderr="podman is not installed", elapsed_ms=0,
            )

        profile = SANDBOX_PROFILES[sandbox]
        # Add user-specified volumes to profile
        if volumes:
            merged_vols = list(profile.volumes) + volumes
            profile = SandboxProfile(
                level=profile.level,
                network=profile.network,
                read_only=profile.read_only,
                cap_drop=profile.cap_drop,
                cap_add=profile.cap_add,
                no_new_privileges=profile.no_new_privileges,
                pids_limit=profile.pids_limit,
                memory=profile.memory,
                cpus=profile.cpus,
                tmpfs=profile.tmpfs,
                volumes=tuple(merged_vols),
                user=profile.user,
                workdir=profile.workdir,
            )

        if container_name:
            # Persistent container: create if needed, start, exec
            if not PodmanTool.exists(container_name):
                PodmanTool.create(container_name, image, profile=profile,
                                  command="sleep infinity")
            state = PodmanTool.state(container_name)
            if state != ContainerState.RUNNING:
                PodmanTool.start(container_name)
            return PodmanTool.exec(container_name, command, timeout_s=timeout_s)
        else:
            # One-shot: run and remove
            return PodmanTool.run_oneshot(
                image, command, profile=profile,
                timeout_s=timeout_s,
            )

    def describe_sandbox(self, level: SandboxLevel) -> str:
        """Human-readable description of what a sandbox level does."""
        descriptions = {
            SandboxLevel.NONE: (
                "No isolation. Commands run directly on the host with full access."
            ),
            SandboxLevel.TOOLBOX: (
                "Toolbox container: home directory mounted, user identity preserved, "
                "network access, system integration. Like running on host but in a "
                "separate OS image."
            ),
            SandboxLevel.WORKSPACE: (
                "Podman container: workspace mounted read-write, network access, "
                "no privilege escalation. Good for builds and dev work."
            ),
            SandboxLevel.RESTRICTED: (
                "Podman container: workspace mounted read-only, NO network access, "
                "all capabilities dropped, PID limit 100, memory limit 512MB. "
                "Good for analysis and read-only operations."
            ),
            SandboxLevel.LOCKED: (
                "Podman container: read-only filesystem, NO network, all capabilities "
                "dropped, no privilege escalation, PID limit 50, 256MB memory, "
                "0.5 CPU. Only /tmp is writable (50MB tmpfs). Maximum isolation."
            ),
        }
        return descriptions.get(level, "Unknown sandbox level.")
