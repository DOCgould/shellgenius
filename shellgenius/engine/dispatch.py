"""
Unified Dispatch — connecting pipes, MIME routing, unix sockets, and shims.

This is where ShellGenius's architecture comes together. The insight:

    Unix has TWO dispatch systems, both native:

    1. PIPE ALGEBRA (text streams)
       stdin → grep → awk → sort → stdout
       Typed: TEXT_LINES, JSON, TSV, NULL_DELIM, BINARY
       Transport: anonymous pipes, named pipes (mkfifo), coproc FDs

    2. MIME DISPATCH (typed content)
       file.png → xdg-mime → .desktop handler → GUI app
       Typed: image/png, application/pdf, text/html, audio/mpeg, ...
       Transport: DBus (unix socket), X11 socket, Wayland socket, PipeWire

    Both are rooted in unix sockets and file descriptors.
    Both are composable.
    Both are available from shell.

    The BRIDGE between them:

    ┌─────────────────────────────────────────────────────────┐
    │                   ShellGenius Agent                      │
    │                                                          │
    │  ┌──────────────┐    ┌──────────────┐    ┌────────────┐ │
    │  │ Pipe Algebra  │    │ MIME Dispatch │    │  Containers │ │
    │  │              │    │              │    │            │ │
    │  │ grep|awk|jq  │◄──►│ xdg-open     │◄──►│ toolbox    │ │
    │  │ stdin/stdout  │    │ gio open     │    │ podman     │ │
    │  │ named pipes   │    │ dbus-send    │    │            │ │
    │  └──────┬───────┘    └──────┬───────┘    └─────┬──────┘ │
    │         │                   │                   │        │
    │         └───────────┬───────┘                   │        │
    │                     │                           │        │
    │              Unix Sockets & FDs                  │        │
    │              (/run/user/UID/bus)                 │        │
    │              (/tmp/.X11-unix/X1)                │        │
    │              (anonymous pipe fds)               │        │
    └─────────────────────────────────────────────────────────┘

    SHIMS sit at the boundary: they intercept one dispatch type and
    translate it to the other. Examples:
    - `xdg-open` output → pipe (capture the launched PID, wait for result)
    - pipe output → `xdg-open` input (pipe generates a file, xdg-open displays it)
    - DBus signal → pipe event (monitor a dbus signal, emit lines on stdout)
    - Container socket forwarding (toolbox auto-mounts host sockets)
"""

from __future__ import annotations

import json
import os
import shlex
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import Any, Optional

from shellgenius.engine.shell_executor import ExecResult, execute


# ---------------------------------------------------------------------------
# MIME Dispatch — xdg-open, gio, xdg-mime as shell tools
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MimeHandler:
    """A registered MIME type handler on this system."""
    mime_type: str           # e.g. "image/png"
    desktop_file: str        # e.g. "org.gnome.eog.desktop"
    app_name: str = ""       # e.g. "Eye of GNOME"
    exec_cmd: str = ""       # e.g. "eog %U"


@dataclass(frozen=True)
class UnixSocket:
    """A unix socket discovered on the system."""
    path: str
    service: str             # what it's for
    pid: Optional[int] = None
    process: str = ""


class DispatchType(Enum):
    """How content gets routed."""
    PIPE = auto()            # anonymous pipe (stdin/stdout)
    NAMED_PIPE = auto()      # mkfifo — persistent pipe with path
    UNIX_SOCKET = auto()     # /run/user/UID/... — bidirectional IPC
    DBUS = auto()            # DBus method call / signal over unix socket
    XDG_OPEN = auto()        # MIME-routed dispatch to desktop app
    CONTAINER_EXEC = auto()  # podman exec / toolbox run
    FILE = auto()            # temp file handoff (pipe → file → app)


# ---------------------------------------------------------------------------
# MIME routing — query the system's handler registry
# ---------------------------------------------------------------------------

def query_mime_handler(mime_type: str) -> Optional[MimeHandler]:
    """Query what handles a given MIME type on this system."""
    result = execute(f"xdg-mime query default {shlex.quote(mime_type)}", timeout_s=5)
    if not result.ok or not result.stdout.strip():
        return None
    desktop = result.stdout.strip()

    # Try to get the app name and exec command from the .desktop file
    app_name = ""
    exec_cmd = ""
    for search_dir in ["/usr/share/applications", str(Path.home() / ".local/share/applications")]:
        desktop_path = Path(search_dir) / desktop
        if desktop_path.exists():
            try:
                content = desktop_path.read_text()
                for line in content.splitlines():
                    if line.startswith("Name=") and not app_name:
                        app_name = line.split("=", 1)[1]
                    if line.startswith("Exec=") and not exec_cmd:
                        exec_cmd = line.split("=", 1)[1]
            except OSError:
                pass
            break

    return MimeHandler(
        mime_type=mime_type,
        desktop_file=desktop,
        app_name=app_name,
        exec_cmd=exec_cmd,
    )


def query_file_mime(file_path: str) -> Optional[str]:
    """Detect the MIME type of a file."""
    result = execute(f"xdg-mime query filetype {shlex.quote(file_path)}", timeout_s=5)
    if result.ok:
        return result.stdout.strip()
    # Fallback to file command
    result = execute(f"file --mime-type -b {shlex.quote(file_path)}", timeout_s=5)
    return result.stdout.strip() if result.ok else None


def list_mime_handlers() -> list[MimeHandler]:
    """List all registered MIME handlers on this system."""
    handlers = []
    common_types = [
        "image/png", "image/jpeg", "image/svg+xml", "image/gif", "image/webp",
        "application/pdf", "application/json", "application/xml",
        "text/html", "text/plain", "text/csv", "text/markdown",
        "audio/mpeg", "audio/ogg", "audio/wav", "audio/flac",
        "video/mp4", "video/webm", "video/x-matroska",
        "application/x-shellscript", "application/x-python",
        "application/gzip", "application/zip", "application/x-tar",
        "inode/directory",
    ]
    for mime in common_types:
        handler = query_mime_handler(mime)
        if handler:
            handlers.append(handler)
    return handlers


# ---------------------------------------------------------------------------
# Unix Socket discovery — the wiring underneath
# ---------------------------------------------------------------------------

def discover_sockets() -> list[UnixSocket]:
    """Discover active unix sockets relevant to dispatch."""
    sockets = []
    uid = os.getuid()
    runtime_dir = f"/run/user/{uid}"

    # Known socket patterns
    known = {
        "bus": "DBus session bus — IPC backbone for desktop services",
        "pipewire-0": "PipeWire — audio/video routing",
        "pipewire-0-manager": "PipeWire session manager",
        "pulse/native": "PulseAudio (via PipeWire) — audio routing",
    }

    # Scan runtime directory
    if os.path.isdir(runtime_dir):
        for entry in os.listdir(runtime_dir):
            full = os.path.join(runtime_dir, entry)
            if os.path.islink(full):
                continue
            if _is_socket(full):
                service = known.get(entry, f"Unknown service ({entry})")
                sockets.append(UnixSocket(path=full, service=service))
            elif os.path.isdir(full):
                # Check for sockets inside subdirectories
                subdir = full
                try:
                    for sub in os.listdir(subdir):
                        subfull = os.path.join(subdir, sub)
                        if _is_socket(subfull):
                            key = f"{entry}/{sub}"
                            service = known.get(key, f"{entry} service ({sub})")
                            sockets.append(UnixSocket(path=subfull, service=service))
                except OSError:
                    pass

    # X11 socket
    x11_path = "/tmp/.X11-unix/X1"
    if _is_socket(x11_path):
        sockets.append(UnixSocket(
            path=x11_path,
            service="X11 display server — GUI rendering",
        ))

    return sockets


def _is_socket(path: str) -> bool:
    """Check if a path is a unix socket."""
    try:
        import stat
        return stat.S_ISSOCK(os.lstat(path).st_mode)
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Shims — bridging pipe algebra and MIME dispatch
# ---------------------------------------------------------------------------

@dataclass
class Shim:
    """
    A shim bridges two dispatch types.

    Example shims:
    - pipe_to_viewer: pipe output → temp file → xdg-open (display image from pipe)
    - dbus_to_pipe: dbus-monitor → grep/awk (filter DBus signals as text stream)
    - pipe_to_clipboard: pipe output → xclip/wl-copy (send to clipboard via socket)
    - clipboard_to_pipe: xclip -o/wl-paste → pipe input (read clipboard)
    - pipe_to_notification: pipe output → notify-send (DBus notification)
    """
    name: str
    description: str
    from_type: DispatchType
    to_type: DispatchType
    pattern: str              # the shell command pattern
    explanation: str


SHIMS: list[Shim] = [
    # === Pipe → MIME (content leaves the pipe world, enters GUI) ===
    Shim(
        name="pipe_to_viewer",
        description="Display piped content in the appropriate desktop app",
        from_type=DispatchType.PIPE,
        to_type=DispatchType.XDG_OPEN,
        pattern="cmd | tee /tmp/output.EXT && xdg-open /tmp/output.EXT",
        explanation=(
            "Capture pipe output to a temp file with the right extension, "
            "then xdg-open routes it to the registered handler. "
            "The extension tells xdg-mime which handler to use."
        ),
    ),
    Shim(
        name="pipe_to_browser",
        description="Render piped HTML in the browser",
        from_type=DispatchType.PIPE,
        to_type=DispatchType.XDG_OPEN,
        pattern="cmd | tee /tmp/output.html && xdg-open /tmp/output.html",
        explanation="Pipe generates HTML, browser renders it. Markdown→HTML: cmd | pandoc | tee /tmp/out.html && xdg-open /tmp/out.html",
    ),
    Shim(
        name="pipe_to_image_viewer",
        description="Display a generated image from a pipe",
        from_type=DispatchType.PIPE,
        to_type=DispatchType.XDG_OPEN,
        pattern="gnuplot -e 'set terminal png; set output \"/tmp/plot.png\"; plot sin(x)' && xdg-open /tmp/plot.png",
        explanation="Generate an image (gnuplot, graphviz, ImageMagick), then xdg-open displays it.",
    ),

    # === MIME → Pipe (content enters the pipe world from GUI/files) ===
    Shim(
        name="clipboard_to_pipe",
        description="Read clipboard into a pipe",
        from_type=DispatchType.UNIX_SOCKET,
        to_type=DispatchType.PIPE,
        pattern="xclip -selection clipboard -o | cmd    # X11\nwl-paste | cmd                          # Wayland",
        explanation=(
            "Clipboard is accessed via X11 selection protocol (socket) or Wayland clipboard protocol (socket). "
            "xclip/wl-paste bridge the socket→pipe gap."
        ),
    ),
    Shim(
        name="pipe_to_clipboard",
        description="Send pipe output to clipboard",
        from_type=DispatchType.PIPE,
        to_type=DispatchType.UNIX_SOCKET,
        pattern="cmd | xclip -selection clipboard    # X11\ncmd | wl-copy                        # Wayland",
        explanation="Pipe→clipboard. The clipboard manager receives the data over the display server socket.",
    ),

    # === DBus → Pipe (desktop events as text streams) ===
    Shim(
        name="dbus_to_pipe",
        description="Monitor DBus signals as a text stream",
        from_type=DispatchType.DBUS,
        to_type=DispatchType.PIPE,
        pattern="dbus-monitor --session \"type='signal'\" | grep --line-buffered 'member='",
        explanation=(
            "dbus-monitor connects to the session bus (unix socket) and emits text on stdout. "
            "You can grep/awk/jq this stream. Every desktop event is a DBus signal."
        ),
    ),
    Shim(
        name="dbus_notifications_to_pipe",
        description="Capture desktop notifications as a pipe stream",
        from_type=DispatchType.DBUS,
        to_type=DispatchType.PIPE,
        pattern="dbus-monitor --session \"interface='org.freedesktop.Notifications'\" | grep --line-buffered 'string'",
        explanation="Every notification (from any app) goes through DBus. Monitor + grep = notification pipe.",
    ),
    Shim(
        name="pipe_to_notification",
        description="Send pipe output as a desktop notification",
        from_type=DispatchType.PIPE,
        to_type=DispatchType.DBUS,
        pattern="cmd | tail -1 | xargs -I{} notify-send 'ShellGenius' '{}'",
        explanation="notify-send calls org.freedesktop.Notifications.Notify over DBus. Pipe→DBus→desktop.",
    ),

    # === Named Pipe bridges (persistent inter-process channels) ===
    Shim(
        name="named_pipe_bridge",
        description="Connect two unrelated processes via a named pipe",
        from_type=DispatchType.PIPE,
        to_type=DispatchType.NAMED_PIPE,
        pattern="mkfifo /tmp/bridge; producer > /tmp/bridge & consumer < /tmp/bridge; rm /tmp/bridge",
        explanation=(
            "Named pipes (FIFOs) are filesystem-visible pipes. Any process can open them. "
            "They bridge processes that don't share a parent — unlike anonymous pipes."
        ),
    ),

    # === Container socket forwarding ===
    Shim(
        name="container_socket_forward",
        description="Forward host unix sockets into a container for GUI/audio/DBus access",
        from_type=DispatchType.CONTAINER_EXEC,
        to_type=DispatchType.UNIX_SOCKET,
        pattern=(
            "# Toolbox: auto-forwards all sockets (DBus, X11, PipeWire, PulseAudio)\n"
            "toolbox run -c dev xdg-open image.png    # just works\n"
            "\n"
            "# Podman: explicit socket forwarding\n"
            "podman run --rm \\\n"
            "  -v /tmp/.X11-unix:/tmp/.X11-unix:ro \\\n"
            "  -v /run/user/1000/bus:/run/user/1000/bus:ro \\\n"
            "  -v /run/user/1000/pipewire-0:/run/user/1000/pipewire-0:ro \\\n"
            "  -e DISPLAY=$DISPLAY \\\n"
            "  -e DBUS_SESSION_BUS_ADDRESS=$DBUS_SESSION_BUS_ADDRESS \\\n"
            "  IMAGE xdg-open image.png"
        ),
        explanation=(
            "Toolbox auto-mounts all host sockets. Podman needs explicit -v mounts. "
            "The key sockets: DBus (/run/user/UID/bus), X11 (/tmp/.X11-unix/), "
            "PipeWire (/run/user/UID/pipewire-0). Forward these and GUI/audio works in containers."
        ),
    ),

    # === File as IPC (the simplest shim) ===
    Shim(
        name="file_handoff",
        description="Use a temp file to bridge incompatible processes",
        from_type=DispatchType.PIPE,
        to_type=DispatchType.FILE,
        pattern="cmd > /tmp/data.json && jq '.' /tmp/data.json | next_cmd",
        explanation=(
            "Sometimes a pipe won't work (process needs seekable input, or needs to read twice). "
            "A temp file is the simplest bridge. Use mktemp for safety: f=$(mktemp); cmd > $f; next_cmd < $f; rm $f"
        ),
    ),
]


# ---------------------------------------------------------------------------
# Dispatch planner — decide HOW to route content
# ---------------------------------------------------------------------------

@dataclass
class DispatchPlan:
    """A plan for routing content through the dispatch system."""
    steps: list[dict[str, str]] = field(default_factory=list)
    dispatch_types: list[DispatchType] = field(default_factory=list)
    shims_needed: list[Shim] = field(default_factory=list)
    command: str = ""
    explanation: str = ""


def plan_dispatch(
    source: str,
    target: str,
    *,
    in_container: Optional[str] = None,
) -> DispatchPlan:
    """
    Plan how to route content from source to target.

    Examples:
        plan_dispatch("pipe:json_data", "viewer:browser")
        plan_dispatch("file:image.png", "viewer:desktop")
        plan_dispatch("pipe:csv_data", "file:output.html")
        plan_dispatch("dbus:notifications", "pipe:grep")
    """
    plan = DispatchPlan()

    source_type, source_detail = source.split(":", 1) if ":" in source else (source, "")
    target_type, target_detail = target.split(":", 1) if ":" in target else (target, "")

    # Route: pipe → desktop viewer
    if source_type == "pipe" and target_type == "viewer":
        ext = _guess_extension(source_detail)
        plan.steps.append({"action": "capture", "detail": f"tee /tmp/output.{ext}"})
        plan.steps.append({"action": "dispatch", "detail": f"xdg-open /tmp/output.{ext}"})
        plan.dispatch_types = [DispatchType.PIPE, DispatchType.FILE, DispatchType.XDG_OPEN]
        plan.shims_needed = [s for s in SHIMS if s.name == "pipe_to_viewer"]
        plan.command = f"cmd | tee /tmp/output.{ext} && xdg-open /tmp/output.{ext}"
        plan.explanation = f"Pipe output → temp file (.{ext}) → xdg-open → desktop handler"

    # Route: file → desktop viewer
    elif source_type == "file" and target_type == "viewer":
        plan.steps.append({"action": "dispatch", "detail": f"xdg-open {source_detail}"})
        plan.dispatch_types = [DispatchType.XDG_OPEN]
        plan.command = f"xdg-open {shlex.quote(source_detail)}"
        plan.explanation = f"Direct MIME dispatch: xdg-open routes {source_detail} to registered handler"

    # Route: dbus → pipe
    elif source_type == "dbus" and target_type == "pipe":
        plan.steps.append({"action": "monitor", "detail": f"dbus-monitor {source_detail}"})
        plan.steps.append({"action": "filter", "detail": f"grep/awk on {target_detail}"})
        plan.dispatch_types = [DispatchType.DBUS, DispatchType.PIPE]
        plan.shims_needed = [s for s in SHIMS if s.name == "dbus_to_pipe"]
        plan.command = f"dbus-monitor --session \"interface='{source_detail}'\" | grep --line-buffered '{target_detail}'"
        plan.explanation = "DBus signals → text stream → pipe filtering"

    # Route: pipe → clipboard
    elif source_type == "pipe" and target_type == "clipboard":
        has_wayland = bool(os.environ.get("WAYLAND_DISPLAY"))
        tool = "wl-copy" if has_wayland else "xclip -selection clipboard"
        plan.steps.append({"action": "copy", "detail": tool})
        plan.dispatch_types = [DispatchType.PIPE, DispatchType.UNIX_SOCKET]
        plan.shims_needed = [s for s in SHIMS if s.name == "pipe_to_clipboard"]
        plan.command = f"cmd | {tool}"
        plan.explanation = f"Pipe → {'Wayland' if has_wayland else 'X11'} clipboard via unix socket"

    # Route: clipboard → pipe
    elif source_type == "clipboard" and target_type == "pipe":
        has_wayland = bool(os.environ.get("WAYLAND_DISPLAY"))
        tool = "wl-paste" if has_wayland else "xclip -selection clipboard -o"
        plan.steps.append({"action": "paste", "detail": tool})
        plan.dispatch_types = [DispatchType.UNIX_SOCKET, DispatchType.PIPE]
        plan.shims_needed = [s for s in SHIMS if s.name == "clipboard_to_pipe"]
        plan.command = f"{tool} | cmd"
        plan.explanation = f"{'Wayland' if has_wayland else 'X11'} clipboard → pipe via unix socket"

    # Route: pipe → notification
    elif source_type == "pipe" and target_type == "notification":
        plan.steps.append({"action": "notify", "detail": "notify-send"})
        plan.dispatch_types = [DispatchType.PIPE, DispatchType.DBUS]
        plan.shims_needed = [s for s in SHIMS if s.name == "pipe_to_notification"]
        plan.command = f"cmd | tail -1 | xargs -I{{}} notify-send 'ShellGenius' '{{}}'"
        plan.explanation = "Pipe → notify-send → DBus Notifications → desktop notification"

    # Container wrapping
    if in_container and plan.command:
        plan.steps.insert(0, {"action": "container", "detail": f"toolbox run -c {in_container}"})
        plan.dispatch_types.insert(0, DispatchType.CONTAINER_EXEC)
        plan.command = f"toolbox run -c {shlex.quote(in_container)} bash -c {shlex.quote(plan.command)}"
        plan.explanation = f"[in container '{in_container}'] {plan.explanation}"

    return plan


def _guess_extension(content_type: str) -> str:
    """Guess a file extension from a content type hint."""
    ext_map = {
        "json": "json", "html": "html", "csv": "csv", "xml": "xml",
        "svg": "svg", "png": "png", "jpg": "jpg", "jpeg": "jpg",
        "pdf": "pdf", "md": "md", "markdown": "md",
        "text": "txt", "log": "log",
    }
    lower = content_type.lower()
    for key, ext in ext_map.items():
        if key in lower:
            return ext
    return "txt"


# ---------------------------------------------------------------------------
# Full system introspection — what dispatch infrastructure exists?
# ---------------------------------------------------------------------------

def introspect_dispatch_system() -> dict[str, Any]:
    """
    Full introspection of the dispatch infrastructure on this system.
    Returns a structured report of all available dispatch mechanisms.
    """
    report: dict[str, Any] = {}

    # MIME handlers
    handlers = list_mime_handlers()
    report["mime_handlers"] = {
        "count": len(handlers),
        "handlers": [
            {"mime": h.mime_type, "app": h.app_name or h.desktop_file}
            for h in handlers
        ],
    }

    # Unix sockets
    sockets = discover_sockets()
    report["unix_sockets"] = {
        "count": len(sockets),
        "sockets": [
            {"path": s.path, "service": s.service}
            for s in sockets
        ],
    }

    # Display server
    report["display"] = {
        "x11": bool(os.environ.get("DISPLAY")),
        "wayland": bool(os.environ.get("WAYLAND_DISPLAY")),
        "display_var": os.environ.get("DISPLAY", ""),
        "wayland_var": os.environ.get("WAYLAND_DISPLAY", ""),
    }

    # DBus
    report["dbus"] = {
        "session_bus": os.environ.get("DBUS_SESSION_BUS_ADDRESS", ""),
        "available": bool(os.environ.get("DBUS_SESSION_BUS_ADDRESS")),
    }

    # Clipboard tools
    report["clipboard"] = {}
    for tool in ("xclip", "xsel", "wl-copy", "wl-paste"):
        result = execute(f"command -v {tool}", timeout_s=3)
        if result.ok:
            report["clipboard"][tool] = result.stdout.strip()

    # Container runtimes
    from shellgenius.engine.containers import detect_runtimes
    report["containers"] = {
        rt.value: path for rt, path in detect_runtimes().items()
    }

    # Shims available
    report["shims"] = {
        "count": len(SHIMS),
        "names": [s.name for s in SHIMS],
    }

    return report
