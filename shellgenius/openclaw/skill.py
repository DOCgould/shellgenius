"""
OpenClaw Skill Integration — wiring ShellGenius into the OpenClaw agent framework.

OpenClaw uses a skill system where:
1. Skills are registered via a manifest (JSON config)
2. Skills expose tools that the local LLM can call
3. Skills communicate via OpenAI-compatible tool-call protocol
4. Skills run locally on RTX GPUs / DGX Spark alongside the LLM

This module provides:
- The skill manifest generator
- The HTTP server that OpenClaw calls into (lightweight, no deps beyond stdlib)
- The system prompt fragment that teaches the LLM about shell expertise

Architecture:
    [User] → [OpenClaw Gateway] → [Local LLM on RTX/DGX Spark]
                                        ↓ (tool call)
                                   [ShellGenius Skill Server]
                                        ↓
                                   [Shell Executor + Knowledge Corpus]
                                        ↓ (result)
                                   [Local LLM] → [User]
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from shellgenius.agent import ShellGeniusAgent, AgentContext


SKILL_ID = "shellgenius"
SKILL_VERSION = "0.1.0"
SKILL_NAME = "ShellGenius"
SKILL_DESCRIPTION = (
    "Expert shell agent: pipe composition, shlex parsing, fd tricks, "
    "cross-shell translation, container-backed sandboxing via toolbox/podman, "
    "and safe command execution. The shell wizard you wish you had."
)

# The system prompt fragment injected when this skill is active.
# This teaches the LLM *when* and *how* to use the ShellGenius tools.
SYSTEM_PROMPT_FRAGMENT = """\
You have access to ShellGenius, an expert shell toolkit. Use it when the user needs help with:

- **Composing pipelines**: Describe what you want → get the best pipeline with explanation.
  Tool: `shell_compose` — pass a natural-language description, get a pipeline back.

- **Explaining commands**: Paste a pipeline → get a stage-by-stage breakdown.
  Tool: `shell_explain` — breaks any pipeline into annotated stages.

- **Fixing quoting issues**: Broken command → diagnosed and fixed.
  Tool: `shell_fix_quoting` — catches unquoted vars, mismatched quotes, shlex issues.

- **Translating between shells**: Bash→Fish, Zsh→POSIX, etc.
  Tool: `shell_translate` — identifies incompatible features and suggests alternatives.

- **File descriptor operations**: Redirections, swaps, coprocs, named pipes.
  Tool: `shell_fd_help` — deep fd expertise.

- **Finding the right tool**: "What's the best way to search files?"
  Tool: `shell_find_tool` — recommends tools, prefers modern alternatives when available.

- **Running commands safely**: Execute with explanation, validation, and dry-run support.
  Tool: `shell_run` — always explains before executing.

- **Container environments (Toolbox)**: Create rich dev environments with home dir, identity, and network.
  Tool: `container_create` with runtime="toolbox" — full dev environment in one command.
  Tool: `container_exec` — run commands inside any named container.
  Tool: `toolbox_raw` — direct toolbox CLI access for advanced operations.

- **Sandboxed execution (Podman)**: Run untrusted code or experiments in isolated containers.
  Tool: `container_sandbox_run` — one-shot sandboxed execution with profiles:
    • `workspace`: network access, read-write workspace mount
    • `restricted`: NO network, read-only mounts, capability drops, memory limits
    • `locked`: read-only filesystem, no network, no capabilities, PID + memory + CPU limits
  Tool: `container_lifecycle` — start/stop/pause/unpause/remove containers.
  Tool: `container_state` — inspect one container or list all.
  Tool: `podman_raw` — direct podman CLI access for pods, images, networks, volumes.

**Philosophy**: Prefer one clean pipeline over a script. Explain every stage. Safety first.
**Pipe Algebra**: Pipelines have typed streams (TEXT, JSON, TSV, NULL_DELIM). ShellGenius
type-checks the composition to catch mismatches before execution.
**Container model**: Toolbox for rich envs (dev work). Podman for isolation (sandboxing).
Both are first-class tools, not afterthoughts.
"""


def generate_manifest(install_path: str = ".") -> dict[str, Any]:
    """
    Generate the OpenClaw skill manifest.

    This is the JSON file that tells OpenClaw:
    - What this skill does
    - What tools it exposes
    - How to start the skill server
    - What resources it needs
    """
    agent = ShellGeniusAgent()
    tools = agent.as_tools()

    return {
        "id": SKILL_ID,
        "name": SKILL_NAME,
        "version": SKILL_VERSION,
        "description": SKILL_DESCRIPTION,
        "author": "shellgenius",
        "license": "MIT",

        # How OpenClaw starts this skill
        "runtime": {
            "type": "python",
            "entry": "shellgenius.openclaw.server",
            "command": "python -m shellgenius.openclaw.server",
            "port": 9747,  # "SHEL" on a phone keypad, roughly
        },

        # Tools exposed to the LLM
        "tools": tools,

        # System prompt injected when skill is active
        "system_prompt": SYSTEM_PROMPT_FRAGMENT,

        # Hardware preferences for DGX Spark / RTX optimization
        "hardware": {
            "gpu_required": False,      # Shell ops don't need GPU
            "gpu_beneficial": False,    # Pure CPU workload
            "memory_min_mb": 128,       # Very lightweight
            "notes": (
                "ShellGenius is CPU-only. The GPU is reserved for the LLM backend. "
                "On DGX Spark, this skill runs alongside large local models without "
                "competing for GPU memory."
            ),
        },

        # Categories for the OpenClaw skill marketplace (ClawHub)
        "categories": ["developer-tools", "shell", "productivity", "system"],
        "tags": [
            "shell", "bash", "zsh", "fish", "pipes", "unix",
            "shlex", "quoting", "file-descriptors", "pipeline",
        ],
    }


def write_manifest(output_dir: str | Path = ".") -> Path:
    """Write the skill manifest to disk."""
    output = Path(output_dir) / "openclaw-skill.json"
    manifest = generate_manifest(str(output_dir))
    output.write_text(json.dumps(manifest, indent=2) + "\n")
    return output


def get_system_prompt() -> str:
    """Return the system prompt fragment for LLM injection."""
    return SYSTEM_PROMPT_FRAGMENT
