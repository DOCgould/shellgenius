"""
LLM Bridge — connects ShellGenius tools to a local Anthropic API.

This is the reasoning layer. ShellGenius has:
- 17 tools (shell, container, dispatch)
- FAISS knowledge base (1322 chunks from TLPI)
- Pipe algebra, MIME dispatch, socket routing

The LLM bridge lets Claude reason over all of these via tool use:

    [User question]
        ↓
    [Claude @ localhost:8082 with ShellGenius tools]
        ↓ tool_use: shell_explain, faiss_query, container_exec, etc.
    [ShellGenius agent handles the tool call]
        ↓ tool_result
    [Claude synthesizes the answer]
        ↓
    [Response to user]

This creates a fully local agent loop:
    User → Claude (local API) → ShellGenius tools → Claude → User
"""

from __future__ import annotations

import json
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from typing import Any, Optional

from shellgenius.agent import ShellGeniusAgent, AgentContext


@dataclass
class LLMConfig:
    """Configuration for the local Anthropic API."""
    base_url: str = "http://localhost:8082"
    api_key: str = "test"
    model: str = "claude-sonnet-4-20250514"
    max_tokens: int = 4096
    anthropic_version: str = "2023-06-01"
    thinking: bool = False                 # enable thinking/reasoning mode
    thinking_budget: int = 1024            # max tokens for thinking
    _context_window: int = 0               # 0 = not yet probed
    _backend_info: Optional[dict] = None   # cached probe results

    @property
    def context_window(self) -> int:
        if self._context_window > 0:
            return self._context_window
        self._context_window = _probe_context_window(self.base_url)
        return self._context_window

    @property
    def backend_info(self) -> dict:
        """Probe and cache backend information."""
        if self._backend_info is not None:
            return self._backend_info
        self._backend_info = _probe_backend_info(self.base_url)
        return self._backend_info


def _probe_context_window(base_url: str) -> int:
    """
    Probe the actual context window from the live backend.

    Checks (in order):
    1. Router /health → local_url → backend /slots → n_ctx (llama.cpp fact)
    2. Router /health → local_url → backend /props → n_ctx
    3. Fallback: 32768 (conservative default)

    This gives us the REAL context size from the running llama.cpp/vLLM instance,
    not a guess from a hardcoded table.
    """
    import urllib.request
    import urllib.error

    def _fetch_json(url: str, timeout: float = 3.0) -> dict | list | None:
        try:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read())
        except (urllib.error.URLError, json.JSONDecodeError, OSError):
            return None

    # Step 1: Ask the router where the backend is
    health = _fetch_json(f"{base_url}/health")
    backend_url = None
    if isinstance(health, dict):
        backend_url = health.get("local_url")

    # Step 2: Ask the backend for its actual context size
    if backend_url:
        # llama.cpp /slots endpoint — the definitive source
        slots = _fetch_json(f"{backend_url}/slots")
        if isinstance(slots, list) and slots:
            n_ctx = slots[0].get("n_ctx")
            if isinstance(n_ctx, int) and n_ctx > 0:
                return n_ctx

        # llama.cpp /props endpoint — fallback
        props = _fetch_json(f"{backend_url}/props")
        if isinstance(props, dict):
            gen = props.get("default_generation_settings", {})
            if isinstance(gen, dict):
                params = gen.get("params", gen)
                n_ctx = params.get("n_ctx")
                if isinstance(n_ctx, int) and n_ctx > 0:
                    return n_ctx

    # Step 3: Try probing the base_url itself (maybe it IS the backend)
    slots = _fetch_json(f"{base_url}/slots")
    if isinstance(slots, list) and slots:
        n_ctx = slots[0].get("n_ctx")
        if isinstance(n_ctx, int) and n_ctx > 0:
            return n_ctx

    # Fallback — conservative default
    return 32_768


def _probe_backend_info(base_url: str) -> dict:
    """
    Probe the backend for full runtime information.

    Returns dict with: model_name, gguf_file, n_ctx, speculative,
    reasoning_format, local_url, route_mode, etc.
    """
    import urllib.request
    import urllib.error

    def _fetch_json(url: str, timeout: float = 3.0):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                return json.loads(resp.read())
        except Exception:
            return None

    info: dict = {
        "router_url": base_url,
        "thinking_supported": False,
        "reasoning_formats": [],
    }

    # Router health
    health = _fetch_json(f"{base_url}/health")
    if isinstance(health, dict):
        info["route_mode"] = health.get("route_mode", "unknown")
        info["local_model"] = health.get("local_model", "")
        info["local_url"] = health.get("local_url", "")
        info["prompt_tool_calling"] = health.get("prompt_tool_calling", False)

    backend_url = info.get("local_url", "")
    if not backend_url:
        return info

    # Backend models
    models = _fetch_json(f"{backend_url}/v1/models")
    if isinstance(models, dict) and "models" in models:
        m = models["models"][0] if models["models"] else {}
        info["gguf_file"] = m.get("model", "")

    # Backend slots (context size, speculative decoding)
    slots = _fetch_json(f"{backend_url}/slots")
    if isinstance(slots, list) and slots:
        info["n_ctx"] = slots[0].get("n_ctx", 0)
        info["speculative"] = slots[0].get("speculative", False)

    # Backend props (reasoning support, chat template)
    props = _fetch_json(f"{backend_url}/props")
    if isinstance(props, dict):
        gen = props.get("default_generation_settings", {})
        params = gen.get("params", gen) if isinstance(gen, dict) else {}
        reasoning_fmt = params.get("reasoning_format", "none")
        info["reasoning_format_default"] = reasoning_fmt

        # Check chat template for thinking support
        template = props.get("chat_template", "")
        if "enable_thinking" in template or "<|think|>" in template:
            info["thinking_supported"] = True
            # Valid reasoning formats for llama.cpp
            info["reasoning_formats"] = ["none", "deepseek"]
            if "channel" in template:
                info["thinking_mechanism"] = "channel_tags"  # <|channel>thought<channel|>

    return info


@dataclass
class Message:
    role: str
    content: Any  # str or list of content blocks


@dataclass
class ToolResult:
    tool_use_id: str
    content: str


class ShellGeniusLLM:
    """
    LLM-powered ShellGenius agent using the local Anthropic API.

    Connects all 17 ShellGenius tools + FAISS knowledge base to Claude
    via the Anthropic Messages API with tool use.
    """

    def __init__(
        self,
        config: Optional[LLMConfig] = None,
        agent: Optional[ShellGeniusAgent] = None,
    ):
        self.config = config or LLMConfig()
        self.agent = agent or ShellGeniusAgent()
        self._kb = None  # lazy-loaded FAISS knowledge base
        self._conversation: list[dict] = []
        # Context window tracking
        self._total_input_tokens: int = 0
        self._total_output_tokens: int = 0
        self._turn_count: int = 0

    @property
    def context_used(self) -> int:
        """Approximate tokens used in context (input + output accumulate)."""
        return self._total_input_tokens + self._total_output_tokens

    @property
    def context_remaining(self) -> int:
        """Approximate tokens remaining in context window."""
        return max(0, self.config.context_window - self.context_used)

    @property
    def context_pct(self) -> float:
        """Percentage of context window used."""
        if self.config.context_window == 0:
            return 0.0
        return (self.context_used / self.config.context_window) * 100

    @property
    def kb(self):
        """Lazy-load the FAISS knowledge base."""
        if self._kb is None:
            try:
                from shellgenius.knowledge.faiss_index import FaissKnowledgeBase
                self._kb = FaissKnowledgeBase.load("data/faiss_index")
            except Exception:
                self._kb = False  # mark as unavailable
        return self._kb if self._kb is not False else None

    def get_tools(self) -> list[dict]:
        """Get all tools in Anthropic API format."""
        agent_tools = self.agent.as_tools()

        # Convert from OpenClaw format to Anthropic format
        anthropic_tools = []
        for tool in agent_tools:
            anthropic_tools.append({
                "name": tool["name"],
                "description": tool["description"],
                "input_schema": tool["parameters"],
            })

        # Add FAISS query tool
        if self.kb:
            anthropic_tools.append({
                "name": "knowledge_query",
                "description": (
                    "Search The Linux Programming Interface (Kerrisk) for deep syscall "
                    "and kernel knowledge. Returns relevant text chunks from the book. "
                    "Use this when you need precise details about: pipes, sockets, signals, "
                    "processes, file descriptors, IPC, terminals, pseudoterminals, epoll, "
                    "process groups, job control, or any Linux system call."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Natural language query about Linux internals",
                        },
                        "top_k": {
                            "type": "integer",
                            "description": "Number of results (default: 3)",
                        },
                        "tag": {
                            "type": "string",
                            "description": "Filter by tag: pipe, socket, signal, process, fd, ipc, terminal, thread, io, lock, job_control",
                        },
                    },
                    "required": ["query"],
                },
            })

        # Add knowledge ingest tool
        anthropic_tools.append({
            "name": "knowledge_ingest",
            "description": (
                "Ingest a file or directory into the FAISS knowledge base. "
                "Stores embeddings in /usr/share/embeddings/ grouped by source name. "
                "Supports: PDF, markdown, text, code files (Python, Bash, Go, Rust, JS, etc.), "
                "and YAML/JSON/TOML configs. After ingestion, the data is searchable via knowledge_search_all. "
                "Use this when the user says 'ingest', 'index', 'learn this', 'add to knowledge', etc."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute or relative path to a file or directory to ingest",
                    },
                },
                "required": ["path"],
            },
        })

        # Add cross-index search tool
        anthropic_tools.append({
            "name": "knowledge_search_all",
            "description": (
                "Search ALL registered knowledge indices (not just TLPI). "
                "This searches every .embeddings/ directory that has been ingested. "
                "Returns results from all sources with relevance scores. "
                "Use this when the user's question might span multiple knowledge sources, "
                "or when searching ingested project documentation, code, or custom data."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Natural language query",
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "Number of results (default: 5)",
                    },
                },
                "required": ["query"],
            },
        })

        # Add index listing tool
        anthropic_tools.append({
            "name": "knowledge_list_indices",
            "description": (
                "List all registered FAISS knowledge indices. Shows what data has been "
                "ingested, where the .embeddings/ directories are, and chunk counts."
            ),
            "input_schema": {
                "type": "object",
                "properties": {},
            },
        })

        return anthropic_tools

    def handle_tool(self, name: str, input_: dict) -> str:
        """Handle a tool call and return the result as a string."""
        # FAISS knowledge query
        if name == "knowledge_query":
            if not self.kb:
                return json.dumps({"error": "FAISS knowledge base not available"})
            results = self.kb.query(
                input_["query"],
                top_k=input_.get("top_k", 3),
                tag_filter=input_.get("tag"),
            )
            return json.dumps({
                "results": [
                    {
                        "chapter": chunk.chapter_num,
                        "title": chunk.chapter_title,
                        "page": chunk.page_num,
                        "score": round(score, 3),
                        "tags": chunk.tags,
                        "text": chunk.text[:600],
                    }
                    for chunk, score in results
                ]
            }, indent=2)

        # Knowledge ingestion
        if name == "knowledge_ingest":
            from shellgenius.knowledge.ingest import ingest as do_ingest
            try:
                manifest = do_ingest(input_["path"], show_progress=False)
                return json.dumps({
                    "status": "ok",
                    "files_ingested": manifest["files_ingested"],
                    "total_chunks": manifest["total_chunks"],
                    "source": manifest["source"],
                    "embed_dir": manifest.get("embed_dir", "unknown"),
                    "message": f"Ingested {manifest['files_ingested']} files into {manifest['total_chunks']} chunks. Index saved to {manifest.get('embed_dir', 'unknown')}.",
                })
            except Exception as e:
                return json.dumps({"error": str(e)})

        # Cross-index search (all ingested knowledge)
        if name == "knowledge_search_all":
            from shellgenius.knowledge.ingest import query_all_indices
            try:
                results = query_all_indices(
                    input_["query"],
                    top_k=input_.get("top_k", 5),
                )
                return json.dumps({
                    "results": [
                        {
                            "source_file": chunk.source_file,
                            "source_type": chunk.source_type,
                            "source_index": source,
                            "score": round(score, 3),
                            "line": chunk.line_start,
                            "tags": chunk.tags,
                            "text": chunk.text[:600],
                        }
                        for chunk, score, source in results
                    ]
                }, indent=2)
            except Exception as e:
                return json.dumps({"error": str(e)})

        # List all indices
        if name == "knowledge_list_indices":
            from shellgenius.knowledge.ingest import list_indices
            indices = list_indices()
            return json.dumps({
                "indices": indices,
                "total": len(indices),
            }, indent=2)

        # All other tools go through the agent
        result = self.agent.handle_tool_call(name, input_)
        return json.dumps(result, indent=2)

    def _api_call(self, messages: list[dict], *, tools: Optional[list] = None) -> dict:
        """Make a raw API call to the local Anthropic endpoint."""
        body: dict[str, Any] = {
            "model": self.config.model,
            "max_tokens": self.config.max_tokens,
            "messages": messages,
            "system": self._system_prompt(),
        }
        if tools:
            body["tools"] = tools
        if self.config.thinking:
            body["thinking"] = {
                "type": "enabled",
                "budget_tokens": self.config.thinking_budget,
            }

        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            f"{self.config.base_url}/v1/messages",
            data=data,
            headers={
                "Content-Type": "application/json",
                "x-api-key": self.config.api_key,
                "anthropic-version": self.config.anthropic_version,
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            error_body = e.read().decode("utf-8", errors="replace")
            return {"error": f"HTTP {e.code}: {error_body}"}
        except urllib.error.URLError as e:
            return {"error": f"Connection failed: {e.reason}"}

    def _system_prompt(self) -> str:
        """Build the system prompt for Claude."""
        # Detect runtime environment
        import os
        shell = os.environ.get("SHELL", "/bin/bash")
        user = os.environ.get("USER", "unknown")
        cwd = os.getcwd()
        has_display = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))

        kb_section = ""
        if self.kb:
            stats = self.kb.stats()
            top_tags = ", ".join(f"{k}({v})" for k, v in list(stats["top_tags"].items())[:8])
            kb_section = f"""
# Knowledge Base (FAISS)
You have a FAISS-indexed vector database of "The Linux Programming Interface" by Michael Kerrisk — the definitive 1500-page reference on Linux/UNIX system programming.
- {stats['total_chunks']} text chunks across all {stats['chapters_covered']} chapters
- 384-dimensional embeddings, cosine similarity search
- Top topic coverage: {top_tags}

Use the `knowledge_query` tool when you need:
- Precise syscall semantics (what does dup2 actually do? what happens to FDs across fork?)
- Kernel-level behavior (how does the pipe buffer work? what's PIPE_BUF?)
- IPC mechanism details (unix domain sockets vs named pipes vs shared memory)
- Signal handling rules (async-signal-safe functions, signal masks)
- Process model details (process groups, sessions, controlling terminals)
- Terminal and PTY internals

The knowledge base returns the actual book text with chapter, page, and relevance score. Cite the chapter when you use it: "According to TLPI Ch.44 (Pipes and FIFOs)..."

Do NOT guess at syscall semantics. If you're unsure about exact behavior (e.g., "does close() on a dup'd fd release the lock?"), query the knowledge base first.
"""

        container_info = ""
        for rt in ("podman", "toolbox"):
            from shellgenius.engine.shell_executor import which
            if which(rt):
                container_info += f"  - `{rt}` is installed\n"

        return f"""\
# Identity
You are ShellGenius, an expert-level shell agent that helps users compose, explain, debug, translate, and execute shell commands. You operate on a real Linux system and have tools that can actually run commands.

# Environment
- User: {user}
- Shell: {shell}
- Working directory: {cwd}
- Display server: {"available (GUI apps via xdg-open will work)" if has_display else "none (headless — terminal-only output)"}
- Container runtimes:
{container_info if container_info else "  - none detected"}

# Your Tools — When and How to Use Them

You have 18 tools organized into four categories. Use them actively — don't just answer from memory when a tool would give a better answer.

## Shell Tools (compose, analyze, execute)

- **`shell_compose`**: User describes what they want in natural language → you get back a pipeline with explanation.
  USE WHEN: User says "how do I...", "I want to...", "what's the best way to..."
  EXAMPLE: "count unique IP addresses in access.log, top 20" → returns `awk '{{print $1}}' access.log | sort | uniq -c | sort -rn | head -20`

- **`shell_explain`**: Break down an existing pipeline stage-by-stage.
  USE WHEN: User pastes a command and asks "what does this do?" or you want to explain your own suggestion.
  ALWAYS use this before running a complex command — explain it to the user first.

- **`shell_fix_quoting`**: Detect and fix quoting bugs, shlex issues, word-splitting risks.
  USE WHEN: User's command has quotes, variables, or command substitution that might be broken.
  This catches the #1 source of shell bugs: unquoted variables.

- **`shell_translate`**: Convert commands between bash, zsh, fish, POSIX sh, dash.
  USE WHEN: User needs portability or is switching shells. Identifies features that don't translate (process substitution, arrays, pipefail).

- **`shell_fd_help`**: Get patterns for file descriptor operations.
  USE WHEN: User needs fd swaps, coprocs, named pipes, flock, heredoc fds, or any fd manipulation.
  These are the "deep lore" patterns most people don't know.

- **`shell_find_tool`**: Recommend the best tool for a job, preferring modern alternatives.
  USE WHEN: User asks "what should I use for X?" Recommends rg over grep, fd over find, etc. when available.

- **`shell_run`**: Execute a command with safety checks. Dry-run by default.
  USE WHEN: User explicitly asks you to run something, or you need to check the system state.
  IMPORTANT: This defaults to DRY RUN. Set confirm=true only when the user has approved execution.
  NEVER run destructive commands (rm -rf, dd, mkfs) without explicit user confirmation.
  ALWAYS explain the command FIRST using shell_explain before running it.

## Container Tools (isolate, sandbox, manage)

Containers are first-class tools, not afterthoughts. Use them for:
1. **Isolation**: Run untrusted code in a sandbox
2. **Reproducibility**: Create environments with specific tools installed
3. **Safety**: Test destructive commands without affecting the host

- **`container_create`**: Create a new container.
  - `runtime="toolbox"`: Rich dev environment. Home dir auto-mounted, user identity preserved, network, GUI via socket forwarding. Use for dev work.
  - `runtime="podman"` + `sandbox="locked"`: Maximum isolation. Read-only fs, no network, no capabilities, PID/memory/CPU limits. Use for untrusted code.

- **`container_exec`**: Run a command inside an existing container. Auto-detects toolbox vs podman.
  USE WHEN: User has a container and wants to run something in it.

- **`container_sandbox_run`**: One-shot sandboxed execution (container auto-removed after).
  USE WHEN: User wants to safely run something without persistent state.
  Sandbox levels (progressive restriction):
    • `workspace`: network + read-write. Good for builds.
    • `restricted`: NO network, read-only mounts, caps dropped, 512MB/100 PIDs. Good for analysis.
    • `locked`: Read-only fs, no network, no caps, 256MB/50 PIDs/0.5 CPU. Only /tmp writable. Maximum isolation.

- **`container_state`**: Inspect one container or list all. Returns JSON from podman inspect.
- **`container_lifecycle`**: start/stop/pause/unpause/remove containers.
- **`podman_raw`** / **`toolbox_raw`**: Direct CLI access for operations not covered above (pods, images, networks, volumes).

WHEN TO SUGGEST CONTAINERS:
- User wants to install tools without polluting their system → toolbox
- User wants to run code they don't trust → container_sandbox_run with "locked"
- User wants reproducible environments → container_create with specific image
- User asks "can I try this without breaking anything?" → sandbox

## Dispatch Tools (route content between systems)

Unix has two dispatch systems: pipes (text streams) and MIME routing (typed content via xdg-open/DBus/unix sockets). These tools bridge them.

- **`dispatch_route`**: Plan how to route content between systems.
  Format: source and target as "type:detail"
  EXAMPLES:
    • `pipe:json` → `viewer:browser`: Pipe JSON output into the browser
    • `file:image.png` → `viewer:desktop`: Open image in desktop app
    • `clipboard` → `pipe:input`: Read clipboard into a pipeline
    • `pipe:output` → `clipboard`: Copy pipeline output to clipboard
    • `pipe:text` → `notification`: Send pipe output as desktop notification
    • `dbus:Notifications` → `pipe:grep`: Monitor desktop notifications as text

- **`mime_query`**: What app handles this file/type? Query the MIME handler registry.
  USE WHEN: User asks "what opens .pdf files?" or you need to know the handler before dispatching.

- **`dispatch_introspect`**: Full system introspection — all MIME handlers, active unix sockets, display server, clipboard tools, container runtimes.
  USE WHEN: You need to understand what dispatch routes are available on this system.

## Knowledge Base Tool (FAISS vector search)

- **`knowledge_query`**: Search TLPI for precise Linux internals knowledge.
  USE WHEN: You need syscall-level precision. Don't guess — query.
  ALWAYS use this for questions about:
    • File descriptor semantics (dup2, fcntl, open file descriptions vs descriptors)
    • Process lifecycle (fork/exec/wait, what's inherited across fork?)
    • Signal handling (async-signal-safety, signal masks, sigaction vs signal)
    • IPC mechanisms (pipes vs FIFOs vs unix sockets vs shared memory — tradeoffs)
    • Terminal/PTY behavior (controlling terminal, session leader, SIGHUP)
    • I/O models (select vs poll vs epoll — when to use which)
    • File locking (flock vs fcntl locks — different semantics!)
  You can filter by tag: pipe, socket, signal, process, fd, ipc, terminal, thread, io, lock, job_control
{kb_section}
# Behavioral Rules

## Execution Safety
1. NEVER run destructive commands without explicit user approval. This includes: rm -rf, dd, mkfs, chmod -R 777, and anything that modifies files outside the working directory.
2. Default to DRY RUN. When using shell_run, leave confirm=false unless the user said "run it", "execute", "go ahead", or similar.
3. ALWAYS explain before executing. Call shell_explain on any non-trivial command before shell_run.
4. For untrusted input or unknown scripts: suggest container_sandbox_run with "restricted" or "locked".
5. Quote all variables. If you construct a command with user input, use shlex-safe quoting.

## Response Style
1. Lead with the command/pipeline. The user wants to see the answer, not the reasoning.
2. Explain each pipe stage inline or immediately after. Use the `# comment` pattern for inline annotation.
3. Warn about gotchas: unquoted vars, sort before uniq, UUOC (useless use of cat), missing -print0/-0 for filenames with spaces.
4. Offer alternatives when relevant. "This works, but if you have ripgrep installed: ..."
5. Cite TLPI when you use the knowledge base: "Per TLPI Ch.44, PIPE_BUF is 4096 bytes on Linux, meaning writes up to this size are atomic."

## Tool Chaining Patterns
- **Explain → Run**: Always `shell_explain` before `shell_run` for anything non-trivial.
- **Query → Compose**: `knowledge_query` to get the right semantics, then `shell_compose` to build the pipeline.
- **Compose → Explain → Run**: Full pipeline: build it, explain it, then run it (with user approval).
- **Introspect → Route**: `dispatch_introspect` to see what's available, then `dispatch_route` to plan the routing.
- **Create → Exec**: `container_create` to set up an environment, then `container_exec` to use it.

## What NOT to Do
- Don't write scripts when a pipeline will do. One clean pipeline > a 20-line bash script.
- Don't suggest sudo unless the user's task genuinely requires root.
- Don't run commands to "explore" without the user asking. If you need system info, ask or use dry-run.
- Don't hallucinate syscall semantics. If you're not 100% sure, use knowledge_query.
- Don't suggest deprecated tools when modern alternatives exist (use shell_find_tool to check).
"""

    def chat(self, user_message: str, *, max_turns: int = 10) -> str:
        """
        Send a message and handle the full tool-use loop.

        Shows watchdog status on stderr so the user sees what's happening.
        Returns the final text response from Claude.
        """
        from shellgenius.ui import (
            Spinner, status, status_tool, status_search,
            status_ok, status_warn, status_error, dim, cyan, bold,
        )

        self._conversation.append({
            "role": "user",
            "content": user_message,
        })

        tools = self.get_tools()

        for turn in range(max_turns):
            # Show thinking status
            think_label = "Thinking deeply..." if self.config.thinking else "Thinking..."
            with Spinner(think_label) as spinner:
                response = self._api_call(self._conversation, tools=tools)

            if "error" in response:
                status_error(f"API error: {response['error']}")
                return f"API Error: {response['error']}"

            content = response.get("content", [])
            stop_reason = response.get("stop_reason", "")
            usage = response.get("usage", {})

            # Track and show token usage + context window
            in_tok = usage.get("input_tokens", 0)
            out_tok = usage.get("output_tokens", 0)
            cache_read = usage.get("cache_read_input_tokens", 0)
            self._total_input_tokens = in_tok  # input tokens = full context so far
            self._total_output_tokens += out_tok
            self._turn_count += 1

            if in_tok or out_tok:
                pct = self.context_pct
                remaining = self.context_remaining
                # Format remaining as human-readable
                if remaining > 1000:
                    remain_str = f"{remaining // 1000}k"
                else:
                    remain_str = str(remaining)
                # Color the context indicator based on usage
                if pct > 80:
                    ctx_color = lambda s: bold_red(s) if 'bold_red' in dir() else s
                    ctx_str = f"context: {pct:.0f}% used, {remain_str} remaining"
                    status_warn(ctx_str)
                elif pct > 50:
                    ctx_str = f"tokens: {in_tok} in, {out_tok} out  context: {pct:.0f}% ({remain_str} left)"
                    status(ctx_str)
                else:
                    cache_str = f"  cache: {cache_read}" if cache_read else ""
                    ctx_str = f"tokens: {in_tok} in, {out_tok} out  context: {pct:.0f}% ({remain_str} left){cache_str}"
                    status(ctx_str)

            # Add assistant message to conversation
            self._conversation.append({
                "role": "assistant",
                "content": content,
            })

            # If no tool use, we're done — render markdown for terminal
            if stop_reason != "tool_use":
                from shellgenius.ui_markdown import render_markdown
                text_parts = []
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text_parts.append(block["text"])
                raw = "\n".join(text_parts) if text_parts else "(no response)"
                return render_markdown(raw)

            # Handle tool calls — show each one as a watchdog status
            tool_calls = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_use"]
            if len(tool_calls) > 1:
                status(f"calling {len(tool_calls)} tools...")

            tool_results = []
            for block in tool_calls:
                tool_name = block["name"]
                tool_input = block["input"]
                tool_id = block["id"]

                # Watchdog: show what tool is being called
                if tool_name == "knowledge_query":
                    status_search(tool_input.get("query", ""))
                elif tool_name == "shell_run":
                    cmd = tool_input.get("command", "")
                    confirm = tool_input.get("confirm", False)
                    mode = "executing" if confirm else "dry-run"
                    status_tool(tool_name, f"{mode}: {cmd[:60]}")
                elif tool_name in ("container_create", "container_exec", "container_sandbox_run"):
                    detail = tool_input.get("name", tool_input.get("command", ""))
                    status_tool(tool_name, str(detail)[:60])
                elif tool_name == "shell_explain":
                    cmd = tool_input.get("command", "")
                    status_tool(tool_name, f"{cmd[:60]}")
                elif tool_name == "dispatch_route":
                    src = tool_input.get("source", "")
                    tgt = tool_input.get("target", "")
                    status_tool(tool_name, f"{src} -> {tgt}")
                else:
                    # Generic tool status
                    first_val = next(iter(tool_input.values()), "") if tool_input else ""
                    status_tool(tool_name, str(first_val)[:60])

                # Execute the tool
                with Spinner(f"Running {tool_name}..."):
                    result_str = self.handle_tool(tool_name, tool_input)

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "content": result_str,
                })

            # Add tool results to conversation
            self._conversation.append({
                "role": "user",
                "content": tool_results,
            })

        status_warn("Reached maximum tool-use turns")
        return "(max tool-use turns reached)"

    def reset(self):
        """Clear conversation history and context tracking."""
        self._conversation = []
        self._total_input_tokens = 0
        self._total_output_tokens = 0
        self._turn_count = 0


# ---------------------------------------------------------------------------
# CLI — interactive chat with ShellGenius
# ---------------------------------------------------------------------------

def interactive_chat(config: Optional[LLMConfig] = None):
    """Run an interactive chat session with ShellGenius."""
    from shellgenius.ui import (
        print_banner, print_env_info, print_kb_info, print_chat_help,
        print_prompt, print_kv, status, status_ok, status_error,
        bold, dim, green, cyan, Spinner, PROMPT_NAME, editor_input,
    )

    llm = ShellGeniusLLM(config=config)

    # Setup agent with status
    print_banner()
    with Spinner("Detecting environment..."):
        info = llm.agent.setup()

    print_env_info(info)
    ctx_window = llm.config.context_window
    ctx_str = f"{ctx_window // 1000}k" if ctx_window >= 1000 else str(ctx_window)
    # Probe the real backend info
    import urllib.request, urllib.error
    backend_info = ""
    try:
        with urllib.request.urlopen(f"{llm.config.base_url}/health", timeout=3) as resp:
            health = json.loads(resp.read())
            local_model = health.get("local_model", "")
            local_url = health.get("local_url", "")
            if local_model:
                backend_info = f"  {dim(f'backend: {local_model}')}"
            if local_url:
                # Get the real GGUF model name
                try:
                    with urllib.request.urlopen(f"{local_url}/v1/models", timeout=3) as mresp:
                        models = json.loads(mresp.read())
                        if isinstance(models, dict) and "models" in models:
                            gguf = models["models"][0].get("model", "")
                            if gguf:
                                backend_info = f"  {dim(f'gguf: {gguf}')}"
                except Exception:
                    pass
    except Exception:
        pass

    print_kv("API", f"{llm.config.base_url}{backend_info}")
    print_kv("Context", f"{ctx_str} tokens {dim('(from llama.cpp /slots)')}")
    backend = llm.config.backend_info
    if backend.get("thinking_supported"):
        mechanism = backend.get("thinking_mechanism", "unknown")
        formats = ", ".join(backend.get("reasoning_formats", []))
        thinking_status = "off" if not llm.config.thinking else f"on (budget: {llm.config.thinking_budget})"
        print_kv("Thinking", f"{thinking_status}  {dim(f'supported via {mechanism} [{formats}]')}")
    else:
        print_kv("Thinking", dim("not supported by this model"))
    print_kv("Tools", f"{len(llm.get_tools())} registered")

    if llm.kb:
        print_kb_info(llm.kb.stats())
    else:
        print_kv("Knowledge", dim("FAISS index not found (run: python -m shellgenius.knowledge.faiss_index)"))

    status_ok("Ready")
    print_chat_help()

    def _context_prompt() -> str:
        """Build prompt with context usage indicator."""
        if llm._turn_count == 0:
            return print_prompt()
        pct = llm.context_pct
        remaining = llm.context_remaining
        if remaining > 1000:
            remain_str = f"{remaining // 1000}k"
        else:
            remain_str = str(remaining)
        # Color based on usage
        if pct > 80:
            from shellgenius.ui import bold_red, bold_yellow
            ctx_indicator = bold_red(f"[{pct:.0f}% | {remain_str} left]")
        elif pct > 50:
            from shellgenius.ui import yellow
            ctx_indicator = yellow(f"[{pct:.0f}% | {remain_str} left]")
        else:
            ctx_indicator = dim(f"[{pct:.0f}% | {remain_str} left]")
        # Custom prompt with context
        try:
            from shellgenius.ui import _COLOR, bold_cyan, SYM_ARROW
            if _COLOR:
                return input(f"  {bold_cyan(PROMPT_NAME)} {ctx_indicator} {dim(SYM_ARROW)} ").strip()
            else:
                return input(f"  {PROMPT_NAME} [{pct:.0f}%]> ").strip()
        except (EOFError, KeyboardInterrupt):
            return ""

    while True:
        user_input = _context_prompt()

        if not user_input:
            continue
        if user_input.lower() in ("quit", "exit", "q"):
            print(f"\n  {dim('Bye.')}\n")
            break
        if user_input.lower() == "/reset":
            llm.reset()
            status_ok("Conversation cleared — context freed")
            continue
        if user_input.lower() == "/help":
            print_chat_help()
            continue
        if user_input.lower() == "/think":
            llm.config.thinking = True
            backend = llm.config.backend_info
            if backend.get("thinking_supported"):
                status_ok(f"Thinking enabled (budget: {llm.config.thinking_budget} tokens)")
                status(f"Backend supports thinking via {backend.get('thinking_mechanism', 'unknown')}")
            else:
                status_warn("Thinking enabled, but backend may not support it")
            continue
        if user_input.lower() == "/nothink":
            llm.config.thinking = False
            status_ok("Thinking disabled")
            continue
        if user_input.lower().startswith("/think "):
            # /think 2000 — set budget
            try:
                budget = int(user_input.split()[1])
                llm.config.thinking = True
                llm.config.thinking_budget = budget
                status_ok(f"Thinking enabled (budget: {budget} tokens)")
            except (ValueError, IndexError):
                status_error("Usage: /think [budget_tokens]  e.g. /think 2000")
            continue
        if user_input.lower() == "/edit":
            text = editor_input()
            if text:
                user_input = text
                status_ok(f"Editor input: {len(text)} chars")
            else:
                status(f"Cancelled")
                continue
        if user_input.lower() == "/context":
            pct = llm.context_pct
            used = llm.context_used
            total = llm.config.context_window
            remaining = llm.context_remaining
            turns = llm._turn_count
            print_kv("Context window", f"{total // 1000}k tokens")
            print_kv("Used", f"~{used:,} tokens ({pct:.1f}%)")
            print_kv("Remaining", f"~{remaining:,} tokens")
            print_kv("Turns", str(turns))
            # Visual bar
            bar_width = 40
            filled = int(bar_width * pct / 100)
            empty = bar_width - filled
            if pct > 80:
                from shellgenius.ui import bold_red
                bar = bold_red("█" * filled) + dim("░" * empty)
            elif pct > 50:
                from shellgenius.ui import yellow as yw
                bar = yw("█" * filled) + dim("░" * empty)
            else:
                bar = green("█" * filled) + dim("░" * empty)
            print(f"  {bar} {pct:.0f}%")
            continue
        if user_input.lower() == "/tools":
            tools = llm.get_tools()
            for t in tools:
                print(f"  {bold(t['name']):<30} {dim(t['description'][:70])}")
            continue
        if user_input.lower() == "/env":
            print_env_info(info)
            if llm.kb:
                print_kb_info(llm.kb.stats())
            continue

        response = llm.chat(user_input)
        print(f"\n{response}\n")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="ShellGenius LLM Chat")
    parser.add_argument("--url", default="http://localhost:8082", help="API base URL")
    parser.add_argument("--model", default="claude-sonnet-4-20250514", help="Model to use")
    parser.add_argument("--key", default="test", help="API key")
    parser.add_argument("--ask", help="Single question (non-interactive)")
    args = parser.parse_args()

    config = LLMConfig(base_url=args.url, model=args.model, api_key=args.key)

    if args.ask:
        llm = ShellGeniusLLM(config=config)
        llm.agent.setup()
        print(llm.chat(args.ask))
    else:
        interactive_chat(config)
