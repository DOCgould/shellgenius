//! Tool definitions — ToolName enum, schema generation, dispatch routing.

use std::str::FromStr;

use serde_json::{json, Value};

/// All 17 tool names the agent exposes.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum ToolName {
    // Shell (7)
    ShellCompose,
    ShellExplain,
    ShellFixQuoting,
    ShellTranslate,
    ShellFdHelp,
    ShellFindTool,
    ShellRun,
    // Container (7)
    ContainerCreate,
    ContainerExec,
    ContainerSandboxRun,
    ContainerState,
    ContainerLifecycle,
    PodmanRaw,
    ToolboxRaw,
    // Dispatch (3)
    DispatchRoute,
    MimeQuery,
    DispatchIntrospect,
}

impl FromStr for ToolName {
    type Err = ();

    fn from_str(s: &str) -> Result<Self, Self::Err> {
        match s {
            "shell_compose" => Ok(Self::ShellCompose),
            "shell_explain" => Ok(Self::ShellExplain),
            "shell_fix_quoting" => Ok(Self::ShellFixQuoting),
            "shell_translate" => Ok(Self::ShellTranslate),
            "shell_fd_help" => Ok(Self::ShellFdHelp),
            "shell_find_tool" => Ok(Self::ShellFindTool),
            "shell_run" => Ok(Self::ShellRun),
            "container_create" => Ok(Self::ContainerCreate),
            "container_exec" => Ok(Self::ContainerExec),
            "container_sandbox_run" => Ok(Self::ContainerSandboxRun),
            "container_state" => Ok(Self::ContainerState),
            "container_lifecycle" => Ok(Self::ContainerLifecycle),
            "podman_raw" => Ok(Self::PodmanRaw),
            "toolbox_raw" => Ok(Self::ToolboxRaw),
            "dispatch_route" => Ok(Self::DispatchRoute),
            "mime_query" => Ok(Self::MimeQuery),
            "dispatch_introspect" => Ok(Self::DispatchIntrospect),
            _ => Err(()),
        }
    }
}

/// Generate all 17 tool schemas in Anthropic-compatible format.
pub fn all_tool_schemas() -> Vec<Value> {
    vec![
        // --- Shell tools ---
        tool("shell_compose",
            "Compose a shell pipeline from a natural-language description. Returns the best pipeline with explanation and alternatives.",
            json!({"type":"object","properties":{"description":{"type":"string","description":"What the pipeline should accomplish"}},"required":["description"]})),
        tool("shell_explain",
            "Break down an existing shell pipeline into explained stages. Identifies each tool, its purpose, and potential issues.",
            json!({"type":"object","properties":{"command":{"type":"string","description":"The shell pipeline to explain"}},"required":["command"]})),
        tool("shell_fix_quoting",
            "Analyze and fix quoting issues in a shell command. Detects unquoted variables, mismatched quotes, and shlex problems.",
            json!({"type":"object","properties":{"command":{"type":"string","description":"The command with suspected quoting issues"}},"required":["command"]})),
        tool("shell_translate",
            "Translate a command between shell dialects (bash, zsh, fish, posix). Identifies incompatible features and suggests alternatives.",
            json!({"type":"object","properties":{"command":{"type":"string"},"from_shell":{"type":"string","enum":["bash","zsh","fish","posix","dash"]},"to_shell":{"type":"string","enum":["bash","zsh","fish","posix","dash"]}},"required":["command","from_shell","to_shell"]})),
        tool("shell_fd_help",
            "Get help with file descriptor operations: redirections, swaps, coprocs, named pipes, locks, and other fd tricks.",
            json!({"type":"object","properties":{"description":{"type":"string","description":"What fd operation you need help with"}},"required":["description"]})),
        tool("shell_find_tool",
            "Recommend the best shell tool for a given task. Prefers modern alternatives (rg over grep, fd over find) when available.",
            json!({"type":"object","properties":{"task":{"type":"string","description":"What you need to do"}},"required":["task"]})),
        tool("shell_run",
            "Execute a shell command with safety checks. Explains the pipeline, validates it, then runs it.",
            json!({"type":"object","properties":{"command":{"type":"string"},"confirm":{"type":"boolean","description":"Set to true to actually execute (default: dry run)"}},"required":["command"]})),
        // --- Container tools ---
        tool("container_create",
            "Create a new container environment. Use runtime='toolbox' for a rich dev environment, runtime='podman' with a sandbox level for isolated execution.",
            json!({"type":"object","properties":{"name":{"type":"string","description":"Container name"},"runtime":{"type":"string","enum":["toolbox","podman"],"description":"Container runtime (default: toolbox)"},"image":{"type":"string","description":"OCI image"},"sandbox":{"type":"string","enum":["toolbox","workspace","restricted","locked"],"description":"Sandbox profile for podman"},"distro":{"type":"string"},"release":{"type":"string"}},"required":["name"]})),
        tool("container_exec",
            "Execute a command inside a named container. Auto-detects whether it's a toolbox or podman container.",
            json!({"type":"object","properties":{"name":{"type":"string","description":"Container name"},"command":{"type":"string","description":"Command to execute inside the container"},"runtime":{"type":"string","enum":["auto","toolbox","podman"]}},"required":["name","command"]})),
        tool("container_sandbox_run",
            "Run a command in a one-shot sandboxed container (auto-removed after). Sandbox levels: workspace, restricted, locked.",
            json!({"type":"object","properties":{"command":{"type":"string","description":"Command to run sandboxed"},"sandbox":{"type":"string","enum":["workspace","restricted","locked"],"description":"Isolation level (default: restricted)"},"image":{"type":"string","description":"OCI image (default: ubuntu:latest)"}},"required":["command"]})),
        tool("container_state",
            "Inspect a container's state, or list all containers. Shows both toolbox and podman containers.",
            json!({"type":"object","properties":{"name":{"type":"string","description":"Container name to inspect (omit to list all)"}}})),
        tool("container_lifecycle",
            "Manage container lifecycle: start, stop, pause, unpause, or remove.",
            json!({"type":"object","properties":{"name":{"type":"string","description":"Container name"},"action":{"type":"string","enum":["start","stop","pause","unpause","remove"],"description":"Lifecycle action"},"force":{"type":"boolean","description":"Force the action"}},"required":["name","action"]})),
        tool("podman_raw",
            "Execute a raw podman command for advanced operations: pod management, image operations, network configuration, volume management.",
            json!({"type":"object","properties":{"subcommand":{"type":"string","description":"The podman subcommand and arguments"}},"required":["subcommand"]})),
        tool("toolbox_raw",
            "Execute a raw toolbox command for operations not covered by other container tools.",
            json!({"type":"object","properties":{"subcommand":{"type":"string","description":"The toolbox subcommand and arguments"}},"required":["subcommand"]})),
        // --- Dispatch tools ---
        tool("dispatch_route",
            "Route content between different dispatch systems: pipe output to a desktop viewer, DBus signals to a pipe, pipe to clipboard, pipe to notification, etc.",
            json!({"type":"object","properties":{"source":{"type":"string","description":"Source in 'type:detail' format (e.g. 'pipe:json', 'file:image.png', 'clipboard')"},"target":{"type":"string","description":"Target in 'type:detail' format (e.g. 'viewer:browser', 'clipboard', 'notification')"},"container":{"type":"string","description":"Optional: run inside this container"}},"required":["source","target"]})),
        tool("mime_query",
            "Query the MIME handler for a file or MIME type. Returns the registered desktop application, command, and how to open it.",
            json!({"type":"object","properties":{"file_or_type":{"type":"string","description":"File path or MIME type string"}},"required":["file_or_type"]})),
        tool("dispatch_introspect",
            "Full introspection of the dispatch system: all MIME handlers, active unix sockets, display server, DBus, clipboard tools, container runtimes, and available shims.",
            json!({"type":"object","properties":{}})),
    ]
}

fn tool(name: &str, description: &str, parameters: Value) -> Value {
    json!({
        "name": name,
        "description": description,
        "parameters": parameters,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_tool_count() {
        assert_eq!(all_tool_schemas().len(), 17);
    }

    #[test]
    fn test_all_tools_have_required_fields() {
        for tool in all_tool_schemas() {
            assert!(tool.get("name").is_some());
            assert!(tool.get("description").is_some());
            assert!(tool.get("parameters").is_some());
        }
    }

    #[test]
    fn test_tool_name_from_str() {
        assert_eq!("shell_compose".parse::<ToolName>().unwrap(), ToolName::ShellCompose);
        assert_eq!("container_create".parse::<ToolName>().unwrap(), ToolName::ContainerCreate);
        assert_eq!("dispatch_introspect".parse::<ToolName>().unwrap(), ToolName::DispatchIntrospect);
        assert!("nonexistent".parse::<ToolName>().is_err());
    }
}
