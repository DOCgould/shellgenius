//! AgentContext and ShellGeniusAgent — the orchestrator.

use std::collections::HashMap;
use std::path::PathBuf;

use serde_json::Value;

use sg_core::containers::{detect_runtimes, podman_version, toolbox_version};
use sg_core::corpus::Shell;
use sg_core::exec::{detect_shell, shell_version, which};
use sg_core::types::{AgentResponse, ExecResult, Intent, SgError, SgResult};

// ---------------------------------------------------------------------------
// AgentContext
// ---------------------------------------------------------------------------

#[derive(Debug, Clone)]
pub struct AgentContext {
    pub shell: String,
    pub shell_enum: Shell,
    pub cwd: PathBuf,
    pub available_tools: HashMap<String, String>,
    pub container_runtimes: HashMap<String, String>,
    pub dry_run: bool,
}

impl AgentContext {
    pub fn new() -> Self {
        Self {
            shell: detect_shell(),
            shell_enum: Shell::Bash,
            cwd: std::env::current_dir().unwrap_or_else(|_| PathBuf::from(".")),
            available_tools: HashMap::new(),
            container_runtimes: HashMap::new(),
            dry_run: true,
        }
    }

    pub fn detect_tools(&mut self) {
        let tools_to_check = [
            "grep", "sed", "awk", "sort", "uniq", "cut", "tr", "head", "tail",
            "wc", "tee", "xargs", "find", "jq", "parallel", "pv", "comm",
            "paste", "join", "column", "rg", "fd", "fzf", "bat", "delta",
            "hyperfine", "sd", "choose", "procs", "dust", "tokei", "bottom",
        ];
        for tool in tools_to_check {
            if let Some(path) = which(tool) {
                self.available_tools.insert(tool.into(), path);
            }
        }
        for (rt, path) in detect_runtimes() {
            self.container_runtimes
                .insert(format!("{:?}", rt).to_lowercase(), path);
        }
    }

    pub fn has(&self, tool: &str) -> bool {
        self.available_tools.contains_key(tool)
    }

    pub fn has_runtime(&self, runtime: &str) -> bool {
        self.container_runtimes.contains_key(runtime)
    }
}

impl Default for AgentContext {
    fn default() -> Self {
        Self::new()
    }
}

// ---------------------------------------------------------------------------
// ShellGeniusAgent
// ---------------------------------------------------------------------------

pub struct ShellGeniusAgent {
    pub ctx: AgentContext,
}

impl ShellGeniusAgent {
    pub fn new() -> Self {
        Self {
            ctx: AgentContext::new(),
        }
    }

    pub fn with_context(ctx: AgentContext) -> Self {
        Self { ctx }
    }

    pub fn setup(&mut self) -> Value {
        self.ctx.detect_tools();
        let shell_ver = shell_version(&self.ctx.shell);
        let modern: Vec<&str> = ["rg", "fd", "fzf", "bat", "jq", "parallel"]
            .iter()
            .filter(|t| self.ctx.has(t))
            .copied()
            .collect();

        let mut container_runtimes = serde_json::Map::new();
        if self.ctx.has_runtime("podman") {
            container_runtimes.insert(
                "podman".into(),
                Value::String(podman_version().unwrap_or_else(|| "installed".into())),
            );
        }
        if self.ctx.has_runtime("toolbox") {
            container_runtimes.insert(
                "toolbox".into(),
                Value::String(toolbox_version().unwrap_or_else(|| "installed".into())),
            );
        }

        serde_json::json!({
            "shell": self.ctx.shell,
            "version": shell_ver,
            "tools_available": self.ctx.available_tools.len(),
            "modern_tools": modern,
            "cwd": self.ctx.cwd.to_string_lossy(),
            "container_runtimes": container_runtimes,
        })
    }

    /// Handle a tool call from the LLM. Main dispatch entry point.
    pub fn handle_tool_call(
        &self,
        tool_name: &str,
        params: &Value,
    ) -> Result<Value, SgError> {
        use crate::tools::ToolName;

        let tool = tool_name.parse::<ToolName>()
            .map_err(|_| SgError::UnknownTool(tool_name.into()))?;

        let response = match tool {
            // Shell tools
            ToolName::ShellCompose => {
                let desc = param_str(params, "description")?;
                self.compose_pipeline(&desc)
            }
            ToolName::ShellExplain => {
                let cmd = param_str(params, "command")?;
                self.explain(&cmd)
            }
            ToolName::ShellFixQuoting => {
                let cmd = param_str(params, "command")?;
                self.fix_quoting(&cmd)
            }
            ToolName::ShellTranslate => {
                let cmd = param_str(params, "command")?;
                let from = param_str(params, "from_shell")?;
                let to = param_str(params, "to_shell")?;
                let from_shell = parse_shell(&from)?;
                let to_shell = parse_shell(&to)?;
                self.translate(&cmd, from_shell, to_shell)
            }
            ToolName::ShellFdHelp => {
                let desc = param_str(params, "description")?;
                self.fd_help(&desc)
            }
            ToolName::ShellFindTool => {
                let task = param_str(params, "task")?;
                self.find_best_tool(&task)
            }
            ToolName::ShellRun => {
                let cmd = param_str(params, "command")?;
                let confirm = params.get("confirm").and_then(|v| v.as_bool()).unwrap_or(false);
                self.run(&cmd, confirm)
            }
            // Container tools
            ToolName::ContainerCreate => {
                let name = param_str(params, "name")?;
                let runtime = params.get("runtime").and_then(|v| v.as_str()).unwrap_or("toolbox");
                let image = params.get("image").and_then(|v| v.as_str());
                let sandbox = params.get("sandbox").and_then(|v| v.as_str()).unwrap_or("toolbox");
                let distro = params.get("distro").and_then(|v| v.as_str());
                let release = params.get("release").and_then(|v| v.as_str());
                self.container_create(&name, runtime, image, sandbox, distro, release)
            }
            ToolName::ContainerExec => {
                let name = param_str(params, "name")?;
                let cmd = param_str(params, "command")?;
                let runtime = params.get("runtime").and_then(|v| v.as_str()).unwrap_or("auto");
                self.container_exec(&name, &cmd, runtime)
            }
            ToolName::ContainerSandboxRun => {
                let cmd = param_str(params, "command")?;
                let sandbox = params.get("sandbox").and_then(|v| v.as_str()).unwrap_or("restricted");
                let image = params.get("image").and_then(|v| v.as_str()).unwrap_or("ubuntu:latest");
                self.container_sandbox_run(&cmd, sandbox, image)
            }
            ToolName::ContainerState => {
                let name = params.get("name").and_then(|v| v.as_str());
                self.container_state(name)
            }
            ToolName::ContainerLifecycle => {
                let name = param_str(params, "name")?;
                let action = param_str(params, "action")?;
                let force = params.get("force").and_then(|v| v.as_bool()).unwrap_or(false);
                self.container_lifecycle(&name, &action, force)
            }
            ToolName::PodmanRaw => {
                let sub = param_str(params, "subcommand")?;
                self.raw_container_cmd("podman", &sub)
            }
            ToolName::ToolboxRaw => {
                let sub = param_str(params, "subcommand")?;
                self.raw_container_cmd("toolbox", &sub)
            }
            // Dispatch tools
            ToolName::DispatchRoute => {
                let source = param_str(params, "source")?;
                let target = param_str(params, "target")?;
                let container = params.get("container").and_then(|v| v.as_str());
                self.dispatch_route(&source, &target, container)
            }
            ToolName::MimeQuery => {
                let file_or_type = param_str(params, "file_or_type")?;
                self.mime_query(&file_or_type)
            }
            ToolName::DispatchIntrospect => self.dispatch_introspect(),
        };

        Ok(serde_json::to_value(response).unwrap_or_default())
    }

    /// Export all tools as Anthropic-compatible JSON schemas.
    pub fn as_tools(&self) -> Vec<Value> {
        crate::tools::all_tool_schemas()
    }
}

impl Default for ShellGeniusAgent {
    fn default() -> Self {
        Self::new()
    }
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

fn param_str(params: &Value, key: &str) -> Result<String, SgError> {
    params
        .get(key)
        .and_then(|v| v.as_str())
        .map(|s| s.to_string())
        .ok_or_else(|| SgError::Pipeline(format!("missing required parameter: {}", key)))
}

fn parse_shell(s: &str) -> Result<Shell, SgError> {
    match s.to_lowercase().as_str() {
        "posix" | "sh" => Ok(Shell::Posix),
        "bash" => Ok(Shell::Bash),
        "zsh" => Ok(Shell::Zsh),
        "fish" => Ok(Shell::Fish),
        "dash" => Ok(Shell::Dash),
        "ksh" => Ok(Shell::Ksh),
        _ => Err(SgError::Pipeline(format!("unknown shell: {}", s))),
    }
}
