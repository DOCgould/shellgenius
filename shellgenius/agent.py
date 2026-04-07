"""
ShellGenius Agent — the orchestrator.

This is the brain that ties together:
- The knowledge corpus (pipe idioms, fd tricks, quoting rules)
- The pipe algebra engine (composition, type-checking, validation)
- The shell executor (safe execution with instrumentation)
- The container tools (toolbox & podman for isolation and state)
- The OpenClaw skill interface (LLM integration, tool calling)

The agent's job is to take a user's intent (natural language or partial command)
and produce the simplest, most correct shell pipeline to achieve it.

Design philosophy:
- Simplicity first: prefer one clean pipeline over a script
- Explain everything: every pipe stage gets a human-readable annotation
- Safety by default: dry-run first, execute only when confirmed
- Cross-shell awareness: know what's portable and what isn't
- Container-native: toolbox for rich envs, podman for sandboxing
"""

from __future__ import annotations

import json
import shlex
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import Any, Optional

from shellgenius.knowledge.corpus import (
    COMPAT_NOTES,
    FD_TRICKS,
    PIPE_IDIOMS,
    QUOTING_RULES,
    PipePattern,
    Shell,
    lookup_fd_tricks,
    lookup_idioms,
)
from shellgenius.engine.pipe_algebra import (
    Pipeline,
    PipeStage,
    PipelineError,
    StreamType,
    chain,
    explain_pipeline,
    safely_quote_command,
)
from shellgenius.engine.shell_executor import (
    ExecMode,
    ExecResult,
    detect_shell,
    execute,
    shell_version,
    which,
)
from shellgenius.engine.dispatch import (
    DispatchType,
    MimeHandler,
    Shim,
    SHIMS,
    plan_dispatch,
    query_mime_handler,
    query_file_mime,
    introspect_dispatch_system,
    discover_sockets,
)
from shellgenius.engine.containers import (
    ContainerRuntime,
    ContainerState,
    SandboxLevel,
    SandboxProfile,
    SandboxExecutor,
    ToolboxTool,
    PodmanTool,
    SANDBOX_PROFILES,
    detect_runtimes,
    podman_version,
    toolbox_version,
)


class Intent(Enum):
    """What the user is trying to do."""
    BUILD_PIPELINE = auto()      # compose a new pipeline from description
    EXPLAIN_PIPELINE = auto()    # break down an existing pipeline
    FIX_PIPELINE = auto()        # debug/fix a broken pipeline
    OPTIMIZE_PIPELINE = auto()   # make an existing pipeline faster/simpler
    TRANSLATE_SHELL = auto()     # convert between shells (bash→fish, etc.)
    QUOTING_HELP = auto()        # fix quoting / shlex issues
    FD_REDIRECT = auto()         # help with file descriptors
    FIND_TOOL = auto()           # which tool is best for X?
    EXECUTE = auto()             # just run this command
    # Container intents
    CONTAINER_CREATE = auto()    # create a container (toolbox or podman)
    CONTAINER_EXEC = auto()      # run command in a container
    CONTAINER_STATE = auto()     # inspect / list / check state
    CONTAINER_LIFECYCLE = auto() # start / stop / pause / remove
    CONTAINER_SANDBOX = auto()   # run sandboxed command (one-shot)
    # Dispatch intents
    DISPATCH = auto()            # route content between pipe/MIME/socket/container
    MIME_QUERY = auto()          # query MIME handler for a file or type
    INTROSPECT = auto()          # full system dispatch introspection


@dataclass
class AgentContext:
    """Runtime context for the agent."""
    shell: str = field(default_factory=detect_shell)
    shell_enum: Shell = Shell.BASH
    cwd: Path = field(default_factory=Path.cwd)
    available_tools: dict[str, str] = field(default_factory=dict)  # tool -> path
    container_runtimes: dict[str, str] = field(default_factory=dict)  # runtime -> path
    dry_run: bool = True  # safe default
    verbose: bool = False

    def detect_tools(self) -> None:
        """Probe the system for available shell tools."""
        tools_to_check = [
            "grep", "sed", "awk", "sort", "uniq", "cut", "tr", "head", "tail",
            "wc", "tee", "xargs", "find", "jq", "parallel", "pv", "comm",
            "paste", "join", "column", "rg", "fd", "fzf", "bat", "delta",
            "hyperfine", "sd", "choose", "procs", "dust", "tokei", "bottom",
        ]
        for tool in tools_to_check:
            path = which(tool)
            if path:
                self.available_tools[tool] = path
        # Detect container runtimes
        for rt, path in detect_runtimes().items():
            self.container_runtimes[rt.value] = path

    def has(self, tool: str) -> bool:
        return tool in self.available_tools

    def has_runtime(self, runtime: str) -> bool:
        return runtime in self.container_runtimes


@dataclass
class AgentResponse:
    """What the agent returns to the user/LLM."""
    intent: Intent
    pipeline: Optional[str] = None           # the composed command
    explanation: Optional[str] = None        # human-readable breakdown
    warnings: list[str] = field(default_factory=list)
    alternatives: list[str] = field(default_factory=list)
    exec_result: Optional[ExecResult] = None
    knowledge_refs: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "intent": self.intent.name,
            "pipeline": self.pipeline,
            "explanation": self.explanation,
        }
        if self.warnings:
            d["warnings"] = self.warnings
        if self.alternatives:
            d["alternatives"] = self.alternatives
        if self.exec_result:
            d["result"] = {
                "exit_code": self.exec_result.exit_code,
                "stdout": self.exec_result.stdout[:2000],
                "stderr": self.exec_result.stderr[:500],
                "elapsed_ms": self.exec_result.elapsed_ms,
            }
        if self.knowledge_refs:
            d["knowledge"] = self.knowledge_refs
        return d

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)


class ShellGeniusAgent:
    """
    The ShellGenius agent.

    This is designed to be called either:
    1. Directly from Python
    2. As an OpenClaw skill (via the tool-call interface)
    3. As a CLI tool

    The agent doesn't contain an LLM itself — it provides structured
    shell expertise that an LLM (running locally on RTX/DGX Spark via
    OpenClaw) can call as a tool to get expert shell answers.
    """

    def __init__(self, context: Optional[AgentContext] = None):
        self.ctx = context or AgentContext()

    def setup(self) -> dict[str, Any]:
        """Initialize the agent: detect shell, available tools, container runtimes."""
        self.ctx.detect_tools()
        self._sandbox = SandboxExecutor()
        shell_ver = shell_version(self.ctx.shell)
        info: dict[str, Any] = {
            "shell": self.ctx.shell,
            "version": shell_ver,
            "tools_available": len(self.ctx.available_tools),
            "modern_tools": [
                t for t in ("rg", "fd", "fzf", "bat", "jq", "parallel")
                if self.ctx.has(t)
            ],
            "cwd": str(self.ctx.cwd),
            "container_runtimes": {},
        }
        if self.ctx.has_runtime("podman"):
            info["container_runtimes"]["podman"] = podman_version() or "installed"
        if self.ctx.has_runtime("toolbox"):
            info["container_runtimes"]["toolbox"] = toolbox_version() or "installed"
        return info

    # ----- Core capabilities (exposed as OpenClaw tools) -----

    def compose_pipeline(self, description: str, **hints: Any) -> AgentResponse:
        """
        Build a pipeline from a natural-language description.

        This is the primary entry point. The LLM describes what it wants,
        and ShellGenius returns the best pipeline with explanation.

        This method provides the structured knowledge — the LLM handles
        the natural-language understanding and calls this with parsed intent.
        """
        # Match against known idiom patterns
        matching_idioms = self._match_idioms(description)

        # Build suggested pipeline (the LLM will refine this)
        response = AgentResponse(intent=Intent.BUILD_PIPELINE)
        if matching_idioms:
            response.pipeline = matching_idioms[0].pattern
            response.explanation = matching_idioms[0].explanation
            response.knowledge_refs = [f"idiom:{m.name}" for m in matching_idioms]
            if matching_idioms[0].gotchas:
                response.warnings.append(matching_idioms[0].gotchas)
            # Offer alternatives
            for m in matching_idioms[1:3]:
                response.alternatives.append(f"{m.pattern}  # {m.explanation}")
        return response

    def explain(self, command: str) -> AgentResponse:
        """Break down an existing pipeline into explained stages."""
        stages = explain_pipeline(command)
        explanation_parts = []
        for i, s in enumerate(stages):
            explanation_parts.append(
                f"Stage {i+1}: {s['tool']}\n"
                f"  Command: {s['command']}\n"
                f"  Purpose: {s['explanation']}"
            )
        # Validate
        p = Pipeline()
        for s in stages:
            p.add(PipeStage(s["command"]))
        warnings = p.validate()

        return AgentResponse(
            intent=Intent.EXPLAIN_PIPELINE,
            pipeline=command,
            explanation="\n\n".join(explanation_parts),
            warnings=warnings,
        )

    def fix_quoting(self, broken_command: str) -> AgentResponse:
        """Analyze and fix quoting issues in a command."""
        issues = []
        suggestions = []

        # Check for common quoting problems
        if "$(" in broken_command and "'" in broken_command:
            # Check if $() is inside single quotes (won't expand)
            in_single = False
            for i, c in enumerate(broken_command):
                if c == "'":
                    in_single = not in_single
                if c == "$" and in_single and i + 1 < len(broken_command) and broken_command[i + 1] == "(":
                    issues.append(
                        f"Command substitution $() at position {i} is inside single quotes — it won't expand. "
                        f"Use double quotes instead."
                    )

        # Check for unquoted variables
        import re
        unquoted_vars = re.findall(r'(?<!")\$\w+(?!")', broken_command)
        for var in unquoted_vars:
            # Check it's not already inside double quotes (rough check)
            issues.append(
                f"Variable {var} may be unquoted — risk of word splitting. "
                f'Use "{var}" instead.'
            )

        # Try shlex.split to see if it parses
        try:
            tokens = shlex.split(broken_command)
            suggestions.append(f"shlex parses OK into {len(tokens)} tokens")
        except ValueError as e:
            issues.append(f"shlex parse error: {e}")
            suggestions.append("Fix unmatched quotes before proceeding")

        # Provide relevant quoting rules
        refs = [f"rule:{r.name}" for r in QUOTING_RULES]

        return AgentResponse(
            intent=Intent.QUOTING_HELP,
            pipeline=broken_command,
            explanation="\n".join(issues) if issues else "No obvious quoting issues found.",
            warnings=issues,
            alternatives=suggestions,
            knowledge_refs=refs,
        )

    def translate(self, command: str, from_shell: Shell, to_shell: Shell) -> AgentResponse:
        """Translate a command between shell dialects."""
        notes = []
        for feature, compat in COMPAT_NOTES.items():
            from_name = from_shell.name.lower()
            to_name = to_shell.name.lower()
            if from_name in compat and to_name in compat:
                if "NOT SUPPORTED" in compat[to_name]:
                    notes.append(
                        f"Feature '{feature}' used in {from_shell.name} "
                        f"is NOT available in {to_shell.name}: {compat[to_name]}"
                    )

        return AgentResponse(
            intent=Intent.TRANSLATE_SHELL,
            pipeline=command,
            explanation=f"Translation from {from_shell.name} to {to_shell.name}",
            warnings=notes,
            knowledge_refs=[f"compat:{k}" for k in COMPAT_NOTES.keys()],
        )

    def fd_help(self, description: str) -> AgentResponse:
        """Help with file descriptor operations."""
        tricks = lookup_fd_tricks(self.ctx.shell_enum)
        matching = [t for t in tricks if any(
            word in t.name.lower() or word in t.explanation.lower()
            for word in description.lower().split()
        )]

        explanation_parts = []
        for t in (matching or tricks[:3]):
            explanation_parts.append(
                f"**{t.name}**\n"
                f"  Pattern: `{t.pattern}`\n"
                f"  {t.explanation}"
            )

        return AgentResponse(
            intent=Intent.FD_REDIRECT,
            explanation="\n\n".join(explanation_parts),
            knowledge_refs=[f"fd:{t.name}" for t in (matching or tricks[:3])],
        )

    def find_best_tool(self, task: str) -> AgentResponse:
        """Recommend the best shell tool for a given task."""
        # Map task keywords to tool recommendations
        tool_map = {
            "search": ("rg", "grep") if self.ctx.has("rg") else ("grep",),
            "find file": ("fd", "find") if self.ctx.has("fd") else ("find",),
            "json": ("jq",),
            "csv": ("awk", "cut", "csvkit"),
            "parallel": ("parallel", "xargs -P") if self.ctx.has("parallel") else ("xargs -P",),
            "replace": ("sd", "sed") if self.ctx.has("sd") else ("sed",),
            "view": ("bat", "cat") if self.ctx.has("bat") else ("cat", "less"),
            "diff": ("delta", "diff") if self.ctx.has("delta") else ("diff",),
            "count lines": ("tokei", "wc -l") if self.ctx.has("tokei") else ("wc -l",),
            "benchmark": ("hyperfine",) if self.ctx.has("hyperfine") else ("time",),
            "disk usage": ("dust", "du") if self.ctx.has("dust") else ("du -sh",),
            "process": ("procs", "ps") if self.ctx.has("procs") else ("ps aux",),
            "fuzzy": ("fzf",) if self.ctx.has("fzf") else ("grep -i",),
        }

        recommendations = []
        for keyword, tools in tool_map.items():
            if keyword in task.lower():
                for t in tools:
                    base = t.split()[0]
                    available = "installed" if self.ctx.has(base) else "not found"
                    recommendations.append(f"{t} ({available})")

        return AgentResponse(
            intent=Intent.FIND_TOOL,
            explanation=(
                "Recommended tools:\n" + "\n".join(f"  - {r}" for r in recommendations)
                if recommendations
                else "No specific tool recommendation. Describe the task in more detail."
            ),
            alternatives=recommendations,
        )

    def run(self, command: str, *, confirm: bool = False) -> AgentResponse:
        """Execute a command (with safety checks)."""
        # Always explain first
        explanation_resp = self.explain(command)

        mode = ExecMode.EXECUTE if (confirm or not self.ctx.dry_run) else ExecMode.DRY_RUN
        result = execute(
            command,
            cwd=self.ctx.cwd,
            mode=mode,
            shell_path=self.ctx.shell,
        )

        return AgentResponse(
            intent=Intent.EXECUTE,
            pipeline=command,
            explanation=explanation_resp.explanation,
            warnings=explanation_resp.warnings,
            exec_result=result,
        )

    # ----- Container capabilities (toolbox & podman as tools) -----

    def container_create(self, name: str, *,
                         runtime: str = "toolbox",
                         image: Optional[str] = None,
                         sandbox: str = "toolbox",
                         distro: Optional[str] = None,
                         release: Optional[str] = None) -> AgentResponse:
        """Create a new container environment."""
        if runtime == "toolbox":
            result = ToolboxTool.create(name, image=image, distro=distro, release=release)
            return AgentResponse(
                intent=Intent.CONTAINER_CREATE,
                pipeline=result.command if hasattr(result, 'command') else f"toolbox create {name}",
                explanation=(
                    f"Created toolbox container '{name}'. "
                    f"Home directory auto-mounted, user identity preserved, network access enabled."
                ),
                exec_result=result,
            )
        else:
            level = SandboxLevel[sandbox.upper()] if sandbox != "toolbox" else SandboxLevel.WORKSPACE
            profile = SANDBOX_PROFILES[level]
            img = image or "ubuntu:latest"
            result = PodmanTool.create(name, img, profile=profile, command="sleep infinity")
            return AgentResponse(
                intent=Intent.CONTAINER_CREATE,
                pipeline=result.command if hasattr(result, 'command') else f"podman create --name {name} {img}",
                explanation=(
                    f"Created podman container '{name}' with {sandbox} sandbox profile.\n"
                    f"{self._sandbox.describe_sandbox(level) if hasattr(self, '_sandbox') else ''}"
                ),
                exec_result=result,
            )

    def container_exec(self, name: str, command: str, *,
                       runtime: str = "auto",
                       timeout_s: float = 30.0) -> AgentResponse:
        """Execute a command inside a container."""
        explanation_resp = self.explain(command)

        if runtime == "auto":
            # Prefer toolbox if the container is a toolbox container
            if ToolboxTool.exists(name):
                runtime = "toolbox"
            else:
                runtime = "podman"

        if runtime == "toolbox":
            result = ToolboxTool.run(name, command, timeout_s=timeout_s)
        else:
            result = PodmanTool.exec(name, command, timeout_s=timeout_s)

        return AgentResponse(
            intent=Intent.CONTAINER_EXEC,
            pipeline=command,
            explanation=f"Executed in container '{name}' ({runtime}):\n\n{explanation_resp.explanation}",
            warnings=explanation_resp.warnings,
            exec_result=result,
        )

    def container_sandbox_run(self, command: str, *,
                              sandbox: str = "restricted",
                              image: str = "ubuntu:latest",
                              volumes: Optional[list[str]] = None,
                              timeout_s: float = 30.0) -> AgentResponse:
        """Run a command in a one-shot sandboxed container."""
        level = SandboxLevel[sandbox.upper()]
        explanation_resp = self.explain(command)

        if not hasattr(self, '_sandbox'):
            self._sandbox = SandboxExecutor()

        result = self._sandbox.run(
            command,
            sandbox=level,
            image=image,
            volumes=volumes,
            timeout_s=timeout_s,
        )

        return AgentResponse(
            intent=Intent.CONTAINER_SANDBOX,
            pipeline=command,
            explanation=(
                f"Sandboxed execution ({sandbox} profile):\n"
                f"{self._sandbox.describe_sandbox(level)}\n\n"
                f"{explanation_resp.explanation}"
            ),
            warnings=explanation_resp.warnings,
            exec_result=result,
        )

    def container_state(self, name: Optional[str] = None) -> AgentResponse:
        """Get container state: inspect one, or list all."""
        if name:
            state = PodmanTool.state(name)
            inspect_result = PodmanTool.inspect(name)
            exit_code = PodmanTool.exit_code(name) if state == ContainerState.EXITED else None

            explanation = (
                f"Container '{name}':\n"
                f"  State: {state.value}\n"
            )
            if exit_code is not None:
                explanation += f"  Exit code: {exit_code}\n"

            return AgentResponse(
                intent=Intent.CONTAINER_STATE,
                explanation=explanation,
                exec_result=inspect_result,
            )
        else:
            # List all containers
            podman_result = PodmanTool.ps()
            toolbox_result = ToolboxTool.list_containers()

            parts = []
            if toolbox_result.ok and toolbox_result.stdout.strip():
                parts.append(f"Toolbox containers:\n{toolbox_result.stdout.strip()}")
            if podman_result.ok and podman_result.stdout.strip():
                parts.append(f"Podman containers:\n{podman_result.stdout.strip()}")
            if not parts:
                parts.append("No containers found.")

            return AgentResponse(
                intent=Intent.CONTAINER_STATE,
                explanation="\n\n".join(parts),
            )

    def container_lifecycle(self, name: str, action: str, *,
                            force: bool = False) -> AgentResponse:
        """Manage container lifecycle: start, stop, pause, unpause, remove."""
        actions = {
            "start": PodmanTool.start,
            "stop": PodmanTool.stop,
            "pause": PodmanTool.pause,
            "unpause": PodmanTool.unpause,
        }

        if action == "remove":
            # Try toolbox first, fall back to podman
            if ToolboxTool.exists(name):
                result = ToolboxTool.remove(name, force=force)
            else:
                result = PodmanTool.remove(name, force=force)
        elif action in actions:
            result = actions[action](name)
        else:
            return AgentResponse(
                intent=Intent.CONTAINER_LIFECYCLE,
                explanation=f"Unknown action: {action}. Valid: start, stop, pause, unpause, remove",
                warnings=[f"Unknown action: {action}"],
            )

        new_state = PodmanTool.state(name)
        return AgentResponse(
            intent=Intent.CONTAINER_LIFECYCLE,
            explanation=f"Container '{name}': {action} → state is now {new_state.value}",
            exec_result=result,
        )

    # ----- Internal helpers -----

    # ----- Dispatch capabilities (pipe ↔ MIME ↔ socket ↔ container) -----

    def dispatch_route(self, source: str, target: str, *,
                       container: Optional[str] = None) -> AgentResponse:
        """
        Plan how to route content from source to target.

        Source/target format: "type:detail"
        Examples:
            dispatch_route("pipe:json", "viewer:browser")
            dispatch_route("file:image.png", "viewer:desktop")
            dispatch_route("dbus:Notifications", "pipe:grep")
            dispatch_route("pipe:output", "clipboard")
            dispatch_route("clipboard", "pipe:input")
            dispatch_route("pipe:text", "notification")
        """
        plan = plan_dispatch(source, target, in_container=container)

        explanation_parts = [plan.explanation]
        if plan.shims_needed:
            explanation_parts.append("\nShims used:")
            for shim in plan.shims_needed:
                explanation_parts.append(f"  {shim.name}: {shim.description}")
        if plan.dispatch_types:
            chain = " → ".join(dt.name for dt in plan.dispatch_types)
            explanation_parts.append(f"\nDispatch chain: {chain}")

        return AgentResponse(
            intent=Intent.DISPATCH,
            pipeline=plan.command,
            explanation="\n".join(explanation_parts),
            knowledge_refs=[f"shim:{s.name}" for s in plan.shims_needed],
        )

    def mime_query(self, file_or_type: str) -> AgentResponse:
        """Query what handles a file or MIME type."""
        # Check if it's a file path or a MIME type string
        if "/" in file_or_type and not file_or_type.startswith("/"):
            # Looks like a MIME type (e.g. "image/png")
            mime_type = file_or_type
        else:
            # Looks like a file path
            mime_type = query_file_mime(file_or_type)
            if not mime_type:
                return AgentResponse(
                    intent=Intent.MIME_QUERY,
                    explanation=f"Could not determine MIME type for: {file_or_type}",
                )

        handler = query_mime_handler(mime_type)
        if handler:
            explanation = (
                f"MIME type: {handler.mime_type}\n"
                f"Handler:   {handler.desktop_file}\n"
                f"App:       {handler.app_name or '(unknown)'}\n"
                f"Command:   {handler.exec_cmd or '(unknown)'}\n"
                f"\nTo open:   xdg-open {file_or_type}"
            )
        else:
            explanation = f"No handler registered for MIME type: {mime_type}"

        return AgentResponse(
            intent=Intent.MIME_QUERY,
            pipeline=f"xdg-open {file_or_type}" if handler else None,
            explanation=explanation,
        )

    def dispatch_introspect(self) -> AgentResponse:
        """Full introspection of the dispatch system on this host."""
        report = introspect_dispatch_system()
        parts = []

        parts.append(f"MIME Handlers: {report['mime_handlers']['count']} registered")
        for h in report['mime_handlers']['handlers'][:10]:
            parts.append(f"  {h['mime']:<30} → {h['app']}")

        parts.append(f"\nUnix Sockets: {report['unix_sockets']['count']} active")
        for s in report['unix_sockets']['sockets'][:8]:
            parts.append(f"  {s['path']}")
            parts.append(f"    {s['service']}")

        display = report['display']
        parts.append(f"\nDisplay: {'X11' if display['x11'] else ''} {'Wayland' if display['wayland'] else ''}")
        parts.append(f"  DISPLAY={display['display_var']}")

        parts.append(f"\nDBus: {'active' if report['dbus']['available'] else 'not available'}")

        if report['clipboard']:
            parts.append(f"\nClipboard tools: {', '.join(report['clipboard'].keys())}")

        if report['containers']:
            parts.append(f"\nContainer runtimes: {', '.join(report['containers'].keys())}")

        parts.append(f"\nShims available: {report['shims']['count']}")
        for name in report['shims']['names']:
            shim = next(s for s in SHIMS if s.name == name)
            parts.append(f"  {name}: {shim.from_type.name} → {shim.to_type.name}")

        return AgentResponse(
            intent=Intent.INTROSPECT,
            explanation="\n".join(parts),
        )

    # ----- Internal helpers -----

    def _raw_container_cmd(self, runtime: str, subcommand: str) -> AgentResponse:
        """Execute a raw podman/toolbox command."""
        cmd = f"{runtime} {subcommand}"
        result = execute(cmd, timeout_s=60)
        return AgentResponse(
            intent=Intent.CONTAINER_STATE,
            pipeline=cmd,
            explanation=f"Raw {runtime} command",
            exec_result=result,
        )

    def _match_idioms(self, description: str) -> list:
        """Fuzzy-match a description against known pipe idioms."""
        words = set(description.lower().split())
        scored = []
        for idiom in PIPE_IDIOMS:
            searchable = f"{idiom.name} {idiom.explanation} {idiom.category.value}".lower()
            score = sum(1 for w in words if w in searchable)
            if score > 0 and idiom.for_shell(self.ctx.shell_enum):
                scored.append((score, idiom))
        scored.sort(key=lambda x: -x[0])
        return [idiom for _, idiom in scored]

    # ----- OpenClaw tool interface -----

    def as_tools(self) -> list[dict[str, Any]]:
        """
        Export the agent's capabilities as OpenClaw-compatible tool definitions.

        These are the tools that the local LLM can call when this skill is active.
        """
        return [
            {
                "name": "shell_compose",
                "description": (
                    "Compose a shell pipeline from a natural-language description. "
                    "Returns the best pipeline with explanation and alternatives."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "description": {
                            "type": "string",
                            "description": "What the pipeline should accomplish",
                        },
                    },
                    "required": ["description"],
                },
            },
            {
                "name": "shell_explain",
                "description": (
                    "Break down an existing shell pipeline into explained stages. "
                    "Identifies each tool, its purpose, and potential issues."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {
                            "type": "string",
                            "description": "The shell pipeline to explain",
                        },
                    },
                    "required": ["command"],
                },
            },
            {
                "name": "shell_fix_quoting",
                "description": (
                    "Analyze and fix quoting issues in a shell command. "
                    "Detects unquoted variables, mismatched quotes, and shlex problems."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {
                            "type": "string",
                            "description": "The command with suspected quoting issues",
                        },
                    },
                    "required": ["command"],
                },
            },
            {
                "name": "shell_translate",
                "description": (
                    "Translate a command between shell dialects (bash, zsh, fish, posix). "
                    "Identifies incompatible features and suggests alternatives."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {"type": "string"},
                        "from_shell": {"type": "string", "enum": ["bash", "zsh", "fish", "posix", "dash"]},
                        "to_shell": {"type": "string", "enum": ["bash", "zsh", "fish", "posix", "dash"]},
                    },
                    "required": ["command", "from_shell", "to_shell"],
                },
            },
            {
                "name": "shell_fd_help",
                "description": (
                    "Get help with file descriptor operations: redirections, swaps, "
                    "coprocs, named pipes, locks, and other fd tricks."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "description": {
                            "type": "string",
                            "description": "What fd operation you need help with",
                        },
                    },
                    "required": ["description"],
                },
            },
            {
                "name": "shell_find_tool",
                "description": (
                    "Recommend the best shell tool for a given task. "
                    "Prefers modern alternatives (rg over grep, fd over find) when available."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "task": {
                            "type": "string",
                            "description": "What you need to do",
                        },
                    },
                    "required": ["task"],
                },
            },
            {
                "name": "shell_run",
                "description": (
                    "Execute a shell command with safety checks. "
                    "Explains the pipeline, validates it, then runs it."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {"type": "string"},
                        "confirm": {
                            "type": "boolean",
                            "description": "Set to true to actually execute (default: dry run)",
                        },
                    },
                    "required": ["command"],
                },
            },
            # --- Container tools: toolbox & podman ---
            {
                "name": "container_create",
                "description": (
                    "Create a new container environment. Use runtime='toolbox' for a rich "
                    "dev environment (home dir mounted, user identity, network). Use "
                    "runtime='podman' with a sandbox level for isolated execution."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Container name",
                        },
                        "runtime": {
                            "type": "string",
                            "enum": ["toolbox", "podman"],
                            "description": "Container runtime to use (default: toolbox)",
                        },
                        "image": {
                            "type": "string",
                            "description": "OCI image (e.g. 'fedora:40', 'ubuntu:24.04', 'python:3.12-slim')",
                        },
                        "sandbox": {
                            "type": "string",
                            "enum": ["toolbox", "workspace", "restricted", "locked"],
                            "description": "Sandbox profile for podman containers",
                        },
                        "distro": {
                            "type": "string",
                            "description": "Distro for toolbox (e.g. 'fedora', 'ubuntu')",
                        },
                        "release": {
                            "type": "string",
                            "description": "Release version for toolbox (e.g. '40', '24.04')",
                        },
                    },
                    "required": ["name"],
                },
            },
            {
                "name": "container_exec",
                "description": (
                    "Execute a command inside a named container. Auto-detects whether "
                    "it's a toolbox or podman container. The container must already exist."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Container name to run in",
                        },
                        "command": {
                            "type": "string",
                            "description": "Command to execute inside the container",
                        },
                        "runtime": {
                            "type": "string",
                            "enum": ["auto", "toolbox", "podman"],
                            "description": "Force a specific runtime (default: auto-detect)",
                        },
                    },
                    "required": ["name", "command"],
                },
            },
            {
                "name": "container_sandbox_run",
                "description": (
                    "Run a command in a one-shot sandboxed container (auto-removed after). "
                    "Use this for untrusted code, network-isolated analysis, or safe "
                    "experimentation. Sandbox levels: workspace (network, rw), "
                    "restricted (no network, ro), locked (max isolation)."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {
                            "type": "string",
                            "description": "Command to run sandboxed",
                        },
                        "sandbox": {
                            "type": "string",
                            "enum": ["workspace", "restricted", "locked"],
                            "description": "Isolation level (default: restricted)",
                        },
                        "image": {
                            "type": "string",
                            "description": "OCI image to use (default: ubuntu:latest)",
                        },
                        "volumes": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Volume mounts (e.g. ['./code:/workspace:ro'])",
                        },
                    },
                    "required": ["command"],
                },
            },
            {
                "name": "container_state",
                "description": (
                    "Inspect a container's state, or list all containers. "
                    "Shows both toolbox and podman containers."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Container name to inspect (omit to list all)",
                        },
                    },
                },
            },
            {
                "name": "container_lifecycle",
                "description": (
                    "Manage container lifecycle: start, stop, pause, unpause, or remove. "
                    "Works with both toolbox and podman containers."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Container name",
                        },
                        "action": {
                            "type": "string",
                            "enum": ["start", "stop", "pause", "unpause", "remove"],
                            "description": "Lifecycle action",
                        },
                        "force": {
                            "type": "boolean",
                            "description": "Force the action (e.g. force remove a running container)",
                        },
                    },
                    "required": ["name", "action"],
                },
            },
            {
                "name": "podman_raw",
                "description": (
                    "Execute a raw podman command for advanced operations not covered by "
                    "other container tools: pod management, image operations, network "
                    "configuration, volume management, etc."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "subcommand": {
                            "type": "string",
                            "description": (
                                "The podman subcommand and arguments "
                                "(e.g. 'pod create --name mypod', 'images', 'network ls')"
                            ),
                        },
                    },
                    "required": ["subcommand"],
                },
            },
            {
                "name": "toolbox_raw",
                "description": (
                    "Execute a raw toolbox command for operations not covered by other "
                    "container tools: list images, enter interactive session, etc."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "subcommand": {
                            "type": "string",
                            "description": "The toolbox subcommand and arguments (e.g. 'list --images', 'rmi IMAGE')",
                        },
                    },
                    "required": ["subcommand"],
                },
            },
            # --- Dispatch tools: pipe ↔ MIME ↔ socket ↔ container ---
            {
                "name": "dispatch_route",
                "description": (
                    "Route content between different dispatch systems: pipe output to a desktop "
                    "viewer, DBus signals to a pipe, pipe to clipboard, pipe to notification, etc. "
                    "Uses shims to bridge pipe algebra and MIME/socket dispatch."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "source": {
                            "type": "string",
                            "description": (
                                "Source in 'type:detail' format. Types: pipe, file, dbus, clipboard. "
                                "Examples: 'pipe:json', 'file:image.png', 'dbus:Notifications', 'clipboard'"
                            ),
                        },
                        "target": {
                            "type": "string",
                            "description": (
                                "Target in 'type:detail' format. Types: viewer, pipe, clipboard, notification. "
                                "Examples: 'viewer:browser', 'pipe:grep', 'clipboard', 'notification'"
                            ),
                        },
                        "container": {
                            "type": "string",
                            "description": "Optional: run the dispatch inside this container",
                        },
                    },
                    "required": ["source", "target"],
                },
            },
            {
                "name": "mime_query",
                "description": (
                    "Query the MIME handler for a file or MIME type. Returns the registered "
                    "desktop application, command, and how to open it. Works with file paths "
                    "(e.g. 'image.png') or MIME types (e.g. 'application/pdf')."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "file_or_type": {
                            "type": "string",
                            "description": "File path or MIME type string",
                        },
                    },
                    "required": ["file_or_type"],
                },
            },
            {
                "name": "dispatch_introspect",
                "description": (
                    "Full introspection of the dispatch system: all MIME handlers, active unix "
                    "sockets, display server, DBus, clipboard tools, container runtimes, and "
                    "available shims. Use this to understand what dispatch routes are possible."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {},
                },
            },
        ]

    def handle_tool_call(self, tool_name: str, params: dict[str, Any]) -> dict[str, Any]:
        """
        Handle an incoming tool call from the OpenClaw LLM.
        This is the main dispatch method for the skill.
        """
        handlers = {
            # Shell tools
            "shell_compose": lambda p: self.compose_pipeline(p["description"]),
            "shell_explain": lambda p: self.explain(p["command"]),
            "shell_fix_quoting": lambda p: self.fix_quoting(p["command"]),
            "shell_translate": lambda p: self.translate(
                p["command"],
                Shell[p["from_shell"].upper()],
                Shell[p["to_shell"].upper()],
            ),
            "shell_fd_help": lambda p: self.fd_help(p["description"]),
            "shell_find_tool": lambda p: self.find_best_tool(p["task"]),
            "shell_run": lambda p: self.run(p["command"], confirm=p.get("confirm", False)),
            # Container tools
            "container_create": lambda p: self.container_create(
                p["name"],
                runtime=p.get("runtime", "toolbox"),
                image=p.get("image"),
                sandbox=p.get("sandbox", "toolbox"),
                distro=p.get("distro"),
                release=p.get("release"),
            ),
            "container_exec": lambda p: self.container_exec(
                p["name"], p["command"],
                runtime=p.get("runtime", "auto"),
            ),
            "container_sandbox_run": lambda p: self.container_sandbox_run(
                p["command"],
                sandbox=p.get("sandbox", "restricted"),
                image=p.get("image", "ubuntu:latest"),
                volumes=p.get("volumes"),
            ),
            "container_state": lambda p: self.container_state(
                name=p.get("name"),
            ),
            "container_lifecycle": lambda p: self.container_lifecycle(
                p["name"], p["action"],
                force=p.get("force", False),
            ),
            "podman_raw": lambda p: self._raw_container_cmd("podman", p["subcommand"]),
            "toolbox_raw": lambda p: self._raw_container_cmd("toolbox", p["subcommand"]),
            # Dispatch tools
            "dispatch_route": lambda p: self.dispatch_route(
                p["source"], p["target"],
                container=p.get("container"),
            ),
            "mime_query": lambda p: self.mime_query(p["file_or_type"]),
            "dispatch_introspect": lambda p: self.dispatch_introspect(),
        }

        handler = handlers.get(tool_name)
        if not handler:
            return {"error": f"Unknown tool: {tool_name}"}

        try:
            response = handler(params)
            return response.to_dict()
        except Exception as e:
            return {"error": str(e), "tool": tool_name}
