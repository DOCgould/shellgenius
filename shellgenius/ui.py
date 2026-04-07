"""
ShellGenius Terminal UI — human-first CLI output following clig.dev guidelines.

Principles (from clig.dev + Google Material + Claude Code patterns):
- Show something within 100ms — never appear frozen
- Watchdog: tell the user what you're doing as you do it
- Stderr for status, stdout for output (separation of concerns)
- Color intentionally: red=error, yellow=warning, dim=metadata, bold=important
- Respect NO_COLOR, TERM=dumb, non-TTY
- Lead with the answer, not the process
"""

from __future__ import annotations

import os
import sys
import time
import threading
from typing import Optional


# ---------------------------------------------------------------------------
# Color & style — respects NO_COLOR, TERM=dumb, non-TTY
# ---------------------------------------------------------------------------

def _supports_color() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("TERM") == "dumb":
        return False
    if not hasattr(sys.stderr, "isatty"):
        return False
    return sys.stderr.isatty()


_COLOR = _supports_color()


def _sgr(code: str, text: str) -> str:
    if not _COLOR:
        return text
    return f"\033[{code}m{text}\033[0m"


def bold(text: str) -> str:
    return _sgr("1", text)


def dim(text: str) -> str:
    return _sgr("2", text)


def italic(text: str) -> str:
    return _sgr("3", text)


def red(text: str) -> str:
    return _sgr("31", text)


def green(text: str) -> str:
    return _sgr("32", text)


def yellow(text: str) -> str:
    return _sgr("33", text)


def blue(text: str) -> str:
    return _sgr("34", text)


def magenta(text: str) -> str:
    return _sgr("35", text)


def cyan(text: str) -> str:
    return _sgr("36", text)


def bold_green(text: str) -> str:
    return _sgr("1;32", text)


def bold_red(text: str) -> str:
    return _sgr("1;31", text)


def bold_cyan(text: str) -> str:
    return _sgr("1;36", text)


def bold_yellow(text: str) -> str:
    return _sgr("1;33", text)


# ---------------------------------------------------------------------------
# Symbols — degrade gracefully for non-unicode terminals
# ---------------------------------------------------------------------------

def _supports_unicode() -> bool:
    return os.environ.get("LANG", "").endswith("UTF-8") or os.environ.get("TERM_PROGRAM") in ("iTerm.app", "WezTerm", "kitty")


_UNICODE = _supports_unicode()

SYM_ARROW = ">" if not _UNICODE else "›"
SYM_CHECK = "[ok]" if not _UNICODE else "✓"
SYM_CROSS = "[!!]" if not _UNICODE else "✗"
SYM_WARN = "[!]" if not _UNICODE else "⚠"
SYM_TOOL = "[*]" if not _UNICODE else "⚙"
SYM_SEARCH = "[?]" if not _UNICODE else "⊕"  # changed from magnifying glass for wider compat
SYM_PIPE = "|" if not _UNICODE else "│"
SYM_DOT = "." if not _UNICODE else "·"
SYM_BRAIN = "[~]" if not _UNICODE else "◆"


# ---------------------------------------------------------------------------
# Spinner — shows activity during async operations
# ---------------------------------------------------------------------------

class Spinner:
    """
    Animated spinner for long operations.
    Writes to stderr so it doesn't pollute stdout.

    Usage:
        with Spinner("Searching knowledge base"):
            do_slow_thing()
    """

    _FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"] if _UNICODE else ["|", "/", "-", "\\"]
    _INTERVAL = 0.08

    def __init__(self, message: str = ""):
        self.message = message
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def __enter__(self):
        if _COLOR and sys.stderr.isatty():
            self._thread = threading.Thread(target=self._spin, daemon=True)
            self._thread.start()
        else:
            # Non-TTY: just print the status once
            status(self.message)
        return self

    def __exit__(self, *args):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1)
            # Clear the spinner line
            sys.stderr.write("\r\033[K")
            sys.stderr.flush()

    def update(self, message: str):
        self.message = message

    def _spin(self):
        i = 0
        while not self._stop.is_set():
            frame = self._FRAMES[i % len(self._FRAMES)]
            line = f"\r  {cyan(frame)} {dim(self.message)}"
            sys.stderr.write(line + "\033[K")
            sys.stderr.flush()
            self._stop.wait(self._INTERVAL)
            i += 1


# ---------------------------------------------------------------------------
# Status messages — watchdog output to stderr
# ---------------------------------------------------------------------------

def status(msg: str):
    """Print a dim status line to stderr. Used for watchdog updates."""
    sys.stderr.write(f"  {dim(SYM_DOT)} {dim(msg)}\n")
    sys.stderr.flush()


def status_tool(tool_name: str, detail: str = ""):
    """Print a tool-call status to stderr."""
    detail_str = f" {dim(detail)}" if detail else ""
    sys.stderr.write(f"  {cyan(SYM_TOOL)} {bold(tool_name)}{detail_str}\n")
    sys.stderr.flush()


def status_search(query: str):
    """Print a knowledge query status to stderr."""
    short = query[:60] + "..." if len(query) > 60 else query
    sys.stderr.write(f"  {magenta(SYM_SEARCH)} {dim('searching:')} {italic(short)}\n")
    sys.stderr.flush()


def status_ok(msg: str):
    """Print a success status."""
    sys.stderr.write(f"  {green(SYM_CHECK)} {msg}\n")
    sys.stderr.flush()


def status_warn(msg: str):
    """Print a warning."""
    sys.stderr.write(f"  {yellow(SYM_WARN)} {yellow(msg)}\n")
    sys.stderr.flush()


def status_error(msg: str):
    """Print an error."""
    sys.stderr.write(f"  {red(SYM_CROSS)} {bold_red(msg)}\n")
    sys.stderr.flush()


# ---------------------------------------------------------------------------
# Formatted output blocks — stdout
# ---------------------------------------------------------------------------

def print_header(title: str, subtitle: str = ""):
    """Print a section header."""
    print(f"\n{bold(title)}")
    if subtitle:
        print(f"{dim(subtitle)}")


def print_pipeline(command: str):
    """Print a pipeline command prominently."""
    print(f"\n  {bold_green(command)}\n")


def print_explanation(stages: list[dict[str, str]]):
    """Print a stage-by-stage pipeline explanation."""
    for i, stage in enumerate(stages):
        num = dim(f"{i+1}.")
        tool = bold(stage.get("tool", "?"))
        cmd = stage.get("command", "").strip()
        explanation = dim(stage.get("explanation", ""))
        print(f"  {num} {tool}  {cmd}")
        print(f"     {explanation}")


def print_warning(msg: str):
    """Print a warning in the output."""
    print(f"  {yellow(SYM_WARN)} {yellow(msg)}")


def print_error(msg: str):
    """Print an error in the output."""
    print(f"  {red(SYM_CROSS)} {red(msg)}")


def print_success(msg: str):
    """Print a success message in the output."""
    print(f"  {green(SYM_CHECK)} {green(msg)}")


def print_alternative(cmd: str, note: str = ""):
    """Print an alternative command suggestion."""
    note_str = f"  {dim(note)}" if note else ""
    print(f"  {dim('or:')} {cmd}{note_str}")


def print_kv(key: str, value: str, indent: int = 2):
    """Print a key-value pair."""
    pad = " " * indent
    print(f"{pad}{dim(key + ':')} {value}")


def print_table(headers: list[str], rows: list[list[str]], indent: int = 2):
    """Print a simple aligned table."""
    if not rows:
        return
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            if i < len(widths):
                widths[i] = max(widths[i], len(cell))

    pad = " " * indent
    header_line = pad + "  ".join(bold(h.ljust(w)) for h, w in zip(headers, widths))
    print(header_line)
    print(pad + dim("─" * (sum(widths) + 2 * (len(widths) - 1))))
    for row in rows:
        cells = [cell.ljust(widths[i]) if i < len(widths) else cell for i, cell in enumerate(row)]
        print(pad + "  ".join(cells))


def print_exec_result(command: str, exit_code: int, stdout: str, stderr: str, elapsed_ms: float, max_lines: int = 20):
    """Print a command execution result."""
    if exit_code == 0:
        badge = green(f"exit 0")
    else:
        badge = red(f"exit {exit_code}")
    timing = dim(f"{elapsed_ms:.0f}ms")

    print(f"\n  {badge} {timing} {dim(command)}")

    if stdout.strip():
        lines = stdout.strip().splitlines()
        for line in lines[:max_lines]:
            print(f"  {SYM_PIPE} {line}")
        if len(lines) > max_lines:
            print(f"  {dim(f'... {len(lines) - max_lines} more lines')}")

    if stderr.strip() and exit_code != 0:
        print(f"  {red('stderr:')}")
        for line in stderr.strip().splitlines()[:5]:
            print(f"  {SYM_PIPE} {red(line)}")


# ---------------------------------------------------------------------------
# Banner & help — first impression matters
# ---------------------------------------------------------------------------

def print_banner(version: str = "0.1.0"):
    """Print the startup banner."""
    if _COLOR:
        sys.stderr.write(f"""
  {bold_cyan("ShellGenius")} {dim(f"v{version}")}
  {dim("Expert shell agent — pipes, containers, dispatch")}

""")
    else:
        sys.stderr.write(f"""
  ShellGenius v{version}
  Expert shell agent — pipes, containers, dispatch

""")
    sys.stderr.flush()


def print_env_info(info: dict):
    """Print environment info on startup."""
    shell_str = f"{info['shell']}"
    version_str = dim(f"({info['version']})")
    tools_str = f"{info['tools_available']} detected"
    modern = info.get("modern_tools", [])
    modern_str = ", ".join(modern) if modern else dim("none")
    containers = info.get("container_runtimes", {})
    container_str = ", ".join(f"{k} {dim(v)}" for k, v in containers.items()) if containers else dim("none")

    print_kv("Shell", f"{shell_str} {version_str}")
    print_kv("Tools", f"{tools_str}  modern: {modern_str}")
    print_kv("Containers", container_str)
    print_kv("CWD", info.get("cwd", ""))


def print_kb_info(stats: dict):
    """Print knowledge base info."""
    chunks = stats["total_chunks"]
    chapters = stats["chapters_covered"]
    top = ", ".join(f"{k}" for k in list(stats.get("top_tags", {}).keys())[:6])
    print_kv("Knowledge", f"{chunks} chunks across {chapters} chapters  {dim(f'[{top}]')}")


def print_chat_help():
    """Print help for the interactive chat."""
    print(f"""
  {bold("Commands:")}
    {dim("/think")}    Enable thinking mode (model reasons before answering)
    {dim("/think N")}  Enable thinking with N token budget (e.g. /think 2000)
    {dim("/nothink")}  Disable thinking mode
    {dim("/context")}  Show context window usage (with visual bar)
    {dim("/reset")}    Clear conversation and free context
    {dim("/edit")}     Open $EDITOR for multiline input (vim, nano, etc.)
    {dim("/tools")}    List available tools
    {dim("/env")}      Show environment info
    {dim("/help")}     Show this help
    {dim("quit")}      Exit

  {bold("Tips:")}
    {dim(SYM_ARROW)} Ask questions naturally: {italic('"how do I count unique IPs?"')}
    {dim(SYM_ARROW)} Paste commands to explain: {italic('"what does find . -print0 | xargs -0 grep do?"')}
    {dim(SYM_ARROW)} Ask for safe execution: {italic('"run ls -la in a locked sandbox"')}
    {dim(SYM_ARROW)} Use {bold("/edit")} to compose long prompts in your editor ($EDITOR)
    {dim(SYM_ARROW)} Context indicator appears in prompt after first turn
""")


# The branded prompt name
PROMPT_NAME = "sg"


def print_prompt() -> str:
    """Print the input prompt and return user input."""
    try:
        if _COLOR:
            return input(f"  {bold_cyan(PROMPT_NAME)} {dim(SYM_ARROW)} ").strip()
        else:
            return input(f"  {PROMPT_NAME}> ").strip()
    except (EOFError, KeyboardInterrupt):
        return ""


def editor_input(initial_text: str = "") -> str:
    """
    Open $EDITOR for multiline input, like vim or nano.

    Falls back to a simple multiline stdin reader if no editor is available.
    Returns the text the user wrote (empty string if cancelled).
    """
    import tempfile
    import subprocess

    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"

    # Create temp file with optional initial content
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".md", prefix="shellgenius-", delete=False
    ) as f:
        if initial_text:
            f.write(initial_text)
        else:
            f.write("# Type your prompt here. Save and quit to send.\n"
                    "# Lines starting with # are stripped.\n"
                    "# Empty file = cancel.\n\n")
        tmppath = f.name

    try:
        # Open the editor — this blocks until user saves and quits
        result = subprocess.run([editor, tmppath])
        if result.returncode != 0:
            return ""

        # Read back
        with open(tmppath, "r") as f:
            text = f.read()

        # Strip comment lines and trim
        lines = [l for l in text.splitlines() if not l.startswith("#")]
        cleaned = "\n".join(lines).strip()
        return cleaned

    except (OSError, FileNotFoundError):
        status_error(f"Could not open editor: {editor}")
        status(f"Set $EDITOR or $VISUAL to your preferred editor")
        return ""
    finally:
        try:
            os.unlink(tmppath)
        except OSError:
            pass


def multiline_input() -> str:
    """
    Simple multiline input reader for when no editor is available.
    Enter an empty line or Ctrl-D to finish.
    """
    status("Enter your prompt (empty line or Ctrl-D to send):")
    lines = []
    try:
        while True:
            if _COLOR:
                line = input(f"  {dim('...')} ")
            else:
                line = input("  ... ")
            if not line:
                break
            lines.append(line)
    except EOFError:
        pass
    return "\n".join(lines).strip()
