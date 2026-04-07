//! Unified Dispatch — connecting pipes, MIME routing, unix sockets, and shims.
//!
//! Unix has TWO dispatch systems:
//! 1. Pipe algebra (text streams): stdin → grep → awk → sort → stdout
//! 2. MIME dispatch (typed content): file.png → xdg-mime → .desktop handler → GUI app
//!
//! This module bridges them via shims.

use std::sync::LazyLock;

use serde::{Deserialize, Serialize};

use crate::exec::exec;

// ---------------------------------------------------------------------------
// DispatchType
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum DispatchType {
    Pipe,
    NamedPipe,
    UnixSocket,
    Dbus,
    XdgOpen,
    ContainerExec,
    File,
}

// ---------------------------------------------------------------------------
// MimeHandler, UnixSocket, Shim
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Serialize)]
pub struct MimeHandler {
    pub mime_type: String,
    pub desktop_file: String,
    pub app_name: String,
    pub exec_cmd: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct UnixSocket {
    pub path: String,
    pub service: String,
}

#[derive(Debug, Clone)]
pub struct Shim {
    pub name: String,
    pub description: String,
    pub from_type: DispatchType,
    pub to_type: DispatchType,
    pub pattern: String,
    pub explanation: String,
}

// ---------------------------------------------------------------------------
// Predefined shims
// ---------------------------------------------------------------------------

pub static SHIMS: LazyLock<Vec<Shim>> = LazyLock::new(|| {
    vec![
        Shim {
            name: "pipe_to_viewer".into(),
            description: "Display piped content in the appropriate desktop app".into(),
            from_type: DispatchType::Pipe,
            to_type: DispatchType::XdgOpen,
            pattern: "cmd | tee /tmp/output.EXT && xdg-open /tmp/output.EXT".into(),
            explanation: "Capture pipe output to a temp file with the right extension, then xdg-open routes it to the registered handler.".into(),
        },
        Shim {
            name: "pipe_to_browser".into(),
            description: "Render piped HTML in the browser".into(),
            from_type: DispatchType::Pipe,
            to_type: DispatchType::XdgOpen,
            pattern: "cmd | tee /tmp/output.html && xdg-open /tmp/output.html".into(),
            explanation: "Pipe generates HTML, browser renders it.".into(),
        },
        Shim {
            name: "clipboard_to_pipe".into(),
            description: "Read clipboard into a pipe".into(),
            from_type: DispatchType::UnixSocket,
            to_type: DispatchType::Pipe,
            pattern: "xclip -selection clipboard -o | cmd    # X11\nwl-paste | cmd                          # Wayland".into(),
            explanation: "Clipboard is accessed via X11 selection protocol (socket) or Wayland clipboard protocol (socket).".into(),
        },
        Shim {
            name: "pipe_to_clipboard".into(),
            description: "Send pipe output to clipboard".into(),
            from_type: DispatchType::Pipe,
            to_type: DispatchType::UnixSocket,
            pattern: "cmd | xclip -selection clipboard    # X11\ncmd | wl-copy                        # Wayland".into(),
            explanation: "Pipe→clipboard. The clipboard manager receives the data over the display server socket.".into(),
        },
        Shim {
            name: "dbus_to_pipe".into(),
            description: "Monitor DBus signals as a text stream".into(),
            from_type: DispatchType::Dbus,
            to_type: DispatchType::Pipe,
            pattern: "dbus-monitor --session \"type='signal'\" | grep --line-buffered 'member='".into(),
            explanation: "dbus-monitor connects to the session bus (unix socket) and emits text on stdout.".into(),
        },
        Shim {
            name: "pipe_to_notification".into(),
            description: "Send pipe output as a desktop notification".into(),
            from_type: DispatchType::Pipe,
            to_type: DispatchType::Dbus,
            pattern: "cmd | tail -1 | xargs -I{} notify-send 'ShellGenius' '{}'".into(),
            explanation: "notify-send calls org.freedesktop.Notifications.Notify over DBus. Pipe→DBus→desktop.".into(),
        },
        Shim {
            name: "named_pipe_bridge".into(),
            description: "Connect two unrelated processes via a named pipe".into(),
            from_type: DispatchType::Pipe,
            to_type: DispatchType::NamedPipe,
            pattern: "mkfifo /tmp/bridge; producer > /tmp/bridge & consumer < /tmp/bridge; rm /tmp/bridge".into(),
            explanation: "Named pipes (FIFOs) are filesystem-visible pipes. Any process can open them.".into(),
        },
        Shim {
            name: "container_socket_forward".into(),
            description: "Forward host unix sockets into a container for GUI/audio/DBus access".into(),
            from_type: DispatchType::ContainerExec,
            to_type: DispatchType::UnixSocket,
            pattern: "# Toolbox: auto-forwards all sockets\ntoolbox run -c dev xdg-open image.png".into(),
            explanation: "Toolbox auto-mounts all host sockets. Podman needs explicit -v mounts for X11, DBus, PipeWire.".into(),
        },
        Shim {
            name: "file_handoff".into(),
            description: "Use a temp file to bridge incompatible processes".into(),
            from_type: DispatchType::Pipe,
            to_type: DispatchType::File,
            pattern: "cmd > /tmp/data.json && jq '.' /tmp/data.json | next_cmd".into(),
            explanation: "Sometimes a pipe won't work (process needs seekable input). A temp file is the simplest bridge.".into(),
        },
    ]
});

// ---------------------------------------------------------------------------
// MIME queries — shell out to xdg-mime
// ---------------------------------------------------------------------------

pub fn query_mime_handler(mime_type: &str) -> Option<MimeHandler> {
    let quoted = shlex::try_quote(mime_type).ok()?;
    let result = exec(&format!("xdg-mime query default {}", quoted));
    if !result.ok() || result.stdout.trim().is_empty() {
        return None;
    }
    let desktop = result.stdout.trim().to_string();

    // Try to get app name from .desktop file
    let mut app_name = String::new();
    let mut exec_cmd = String::new();
    for dir in ["/usr/share/applications", &format!("{}/.local/share/applications", std::env::var("HOME").unwrap_or_default())] {
        let path = format!("{}/{}", dir, desktop);
        if let Ok(content) = std::fs::read_to_string(&path) {
            for line in content.lines() {
                if line.starts_with("Name=") && app_name.is_empty() {
                    app_name = line.splitn(2, '=').nth(1).unwrap_or("").into();
                }
                if line.starts_with("Exec=") && exec_cmd.is_empty() {
                    exec_cmd = line.splitn(2, '=').nth(1).unwrap_or("").into();
                }
            }
            break;
        }
    }

    Some(MimeHandler {
        mime_type: mime_type.into(),
        desktop_file: desktop,
        app_name,
        exec_cmd,
    })
}

pub fn query_file_mime(file_path: &str) -> Option<String> {
    let quoted = shlex::try_quote(file_path).ok()?;
    let result = exec(&format!("xdg-mime query filetype {}", quoted));
    if result.ok() {
        return Some(result.stdout.trim().to_string());
    }
    let result = exec(&format!("file --mime-type -b {}", quoted));
    if result.ok() {
        Some(result.stdout.trim().to_string())
    } else {
        None
    }
}

// ---------------------------------------------------------------------------
// Unix socket discovery
// ---------------------------------------------------------------------------

pub fn discover_sockets() -> Vec<UnixSocket> {
    let mut sockets = Vec::new();
    let uid = unsafe { libc::getuid() };
    let runtime_dir = format!("/run/user/{}", uid);

    let known: std::collections::HashMap<&str, &str> = std::collections::HashMap::from([
        ("bus", "DBus session bus — IPC backbone for desktop services"),
        ("pipewire-0", "PipeWire — audio/video routing"),
        ("pipewire-0-manager", "PipeWire session manager"),
    ]);

    if let Ok(entries) = std::fs::read_dir(&runtime_dir) {
        for entry in entries.flatten() {
            let path = entry.path();
            if is_socket(&path) {
                let name = entry.file_name().to_string_lossy().to_string();
                let service = known
                    .get(name.as_str())
                    .map(|s| s.to_string())
                    .unwrap_or_else(|| format!("Unknown service ({})", name));
                sockets.push(UnixSocket {
                    path: path.to_string_lossy().to_string(),
                    service,
                });
            }
        }
    }

    // X11 socket
    let x11_path = "/tmp/.X11-unix/X1";
    if is_socket(std::path::Path::new(x11_path)) {
        sockets.push(UnixSocket {
            path: x11_path.into(),
            service: "X11 display server — GUI rendering".into(),
        });
    }

    sockets
}

fn is_socket(path: &std::path::Path) -> bool {
    use std::os::unix::fs::FileTypeExt;
    path.symlink_metadata()
        .map(|m| m.file_type().is_socket())
        .unwrap_or(false)
}

// ---------------------------------------------------------------------------
// Dispatch planning
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Serialize)]
pub struct DispatchPlan {
    pub steps: Vec<(String, String)>, // (action, detail)
    pub dispatch_types: Vec<DispatchType>,
    pub command: String,
    pub explanation: String,
}

pub fn plan_dispatch(source: &str, target: &str, in_container: Option<&str>) -> DispatchPlan {
    let (src_type, src_detail) = source.split_once(':').unwrap_or((source, ""));
    let (tgt_type, _tgt_detail) = target.split_once(':').unwrap_or((target, ""));

    let mut plan = DispatchPlan {
        steps: Vec::new(),
        dispatch_types: Vec::new(),
        command: String::new(),
        explanation: String::new(),
    };

    match (src_type, tgt_type) {
        ("pipe", "viewer") => {
            let ext = guess_extension(src_detail);
            plan.command = format!("cmd | tee /tmp/output.{ext} && xdg-open /tmp/output.{ext}");
            plan.explanation = format!("Pipe output → temp file (.{ext}) → xdg-open → desktop handler");
            plan.dispatch_types = vec![DispatchType::Pipe, DispatchType::File, DispatchType::XdgOpen];
        }
        ("file", "viewer") => {
            let quoted = shlex::try_quote(src_detail).unwrap_or(src_detail.into());
            plan.command = format!("xdg-open {}", quoted);
            plan.explanation = format!("Direct MIME dispatch: xdg-open routes {} to registered handler", src_detail);
            plan.dispatch_types = vec![DispatchType::XdgOpen];
        }
        ("pipe", "clipboard") => {
            let has_wayland = std::env::var("WAYLAND_DISPLAY").is_ok();
            let tool = if has_wayland { "wl-copy" } else { "xclip -selection clipboard" };
            plan.command = format!("cmd | {}", tool);
            plan.explanation = format!("Pipe → {} clipboard via unix socket", if has_wayland { "Wayland" } else { "X11" });
            plan.dispatch_types = vec![DispatchType::Pipe, DispatchType::UnixSocket];
        }
        ("clipboard", "pipe") => {
            let has_wayland = std::env::var("WAYLAND_DISPLAY").is_ok();
            let tool = if has_wayland { "wl-paste" } else { "xclip -selection clipboard -o" };
            plan.command = format!("{} | cmd", tool);
            plan.explanation = format!("{} clipboard → pipe via unix socket", if has_wayland { "Wayland" } else { "X11" });
            plan.dispatch_types = vec![DispatchType::UnixSocket, DispatchType::Pipe];
        }
        ("pipe", "notification") => {
            plan.command = "cmd | tail -1 | xargs -I{} notify-send 'ShellGenius' '{}'".into();
            plan.explanation = "Pipe → notify-send → DBus Notifications → desktop notification".into();
            plan.dispatch_types = vec![DispatchType::Pipe, DispatchType::Dbus];
        }
        ("dbus", "pipe") => {
            plan.command = format!(
                "dbus-monitor --session \"interface='{}'\" | grep --line-buffered ''",
                src_detail
            );
            plan.explanation = "DBus signals → text stream → pipe filtering".into();
            plan.dispatch_types = vec![DispatchType::Dbus, DispatchType::Pipe];
        }
        _ => {
            plan.explanation = format!("Unknown dispatch route: {} → {}", source, target);
        }
    }

    // Container wrapping
    if let Some(container) = in_container {
        if !plan.command.is_empty() {
            let quoted_cmd = shlex::try_quote(&plan.command).unwrap_or(plan.command.clone().into());
            plan.command = format!("toolbox run -c {} bash -c {}", shlex::try_quote(container).unwrap_or(container.into()), quoted_cmd);
            plan.explanation = format!("[in container '{}'] {}", container, plan.explanation);
            plan.dispatch_types.insert(0, DispatchType::ContainerExec);
        }
    }

    plan
}

fn guess_extension(content_hint: &str) -> &str {
    match content_hint.to_lowercase().as_str() {
        s if s.contains("json") => "json",
        s if s.contains("html") => "html",
        s if s.contains("csv") => "csv",
        s if s.contains("svg") => "svg",
        s if s.contains("png") => "png",
        s if s.contains("pdf") => "pdf",
        s if s.contains("md") || s.contains("markdown") => "md",
        _ => "txt",
    }
}

// ---------------------------------------------------------------------------
// System introspection
// ---------------------------------------------------------------------------

pub fn introspect_dispatch_system() -> serde_json::Value {
    let sockets = discover_sockets();
    let has_display = std::env::var("DISPLAY").is_ok();
    let has_wayland = std::env::var("WAYLAND_DISPLAY").is_ok();
    let has_dbus = std::env::var("DBUS_SESSION_BUS_ADDRESS").is_ok();

    let mut clipboard_tools = serde_json::Map::new();
    for tool in ["xclip", "xsel", "wl-copy", "wl-paste"] {
        if let Some(path) = crate::exec::which(tool) {
            clipboard_tools.insert(tool.into(), serde_json::Value::String(path));
        }
    }

    serde_json::json!({
        "unix_sockets": {
            "count": sockets.len(),
            "sockets": sockets,
        },
        "display": {
            "x11": has_display,
            "wayland": has_wayland,
        },
        "dbus": {
            "available": has_dbus,
        },
        "clipboard": clipboard_tools,
        "shims": {
            "count": SHIMS.len(),
            "names": SHIMS.iter().map(|s| &s.name).collect::<Vec<_>>(),
        },
    })
}
