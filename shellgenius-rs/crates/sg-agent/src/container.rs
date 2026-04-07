//! Container tool implementations.

use sg_core::containers::*;
use sg_core::types::{AgentResponse, Intent};

use crate::context::ShellGeniusAgent;

impl ShellGeniusAgent {
    pub fn container_create(
        &self, name: &str, runtime: &str, image: Option<&str>,
        sandbox: &str, distro: Option<&str>, release: Option<&str>,
    ) -> AgentResponse {
        if runtime == "toolbox" {
            let result = ToolboxTool::create(name, image, distro, release);
            AgentResponse {
                intent: Intent::ContainerCreate,
                pipeline: Some(result.command.clone()),
                explanation: Some(format!(
                    "Created toolbox container '{}'. Home directory auto-mounted, user identity preserved, network access enabled.", name
                )),
                exec_result: Some(result),
                ..AgentResponse::new(Intent::ContainerCreate)
            }
        } else {
            let level = match sandbox.to_lowercase().as_str() {
                "workspace" => SandboxLevel::Workspace,
                "restricted" => SandboxLevel::Restricted,
                "locked" => SandboxLevel::Locked,
                _ => SandboxLevel::Workspace,
            };
            let profile = SANDBOX_PROFILES.get(&level);
            let img = image.unwrap_or("ubuntu:latest");
            let result = PodmanTool::create(name, img, profile, Some("sleep infinity"));
            AgentResponse {
                intent: Intent::ContainerCreate,
                pipeline: Some(result.command.clone()),
                explanation: Some(format!(
                    "Created podman container '{}' with {} sandbox profile.\n{}",
                    name, sandbox, SandboxExecutor::describe_sandbox(level)
                )),
                exec_result: Some(result),
                ..AgentResponse::new(Intent::ContainerCreate)
            }
        }
    }

    pub fn container_exec(&self, name: &str, command: &str, runtime: &str) -> AgentResponse {
        let explanation_resp = self.explain(command);
        let actual_runtime = if runtime == "auto" {
            if ToolboxTool::exists(name) { "toolbox" } else { "podman" }
        } else {
            runtime
        };

        let result = if actual_runtime == "toolbox" {
            ToolboxTool::run(name, command, 30.0)
        } else {
            PodmanTool::exec_cmd(name, command, None, 30.0)
        };

        AgentResponse {
            intent: Intent::ContainerExec,
            pipeline: Some(command.into()),
            explanation: Some(format!(
                "Executed in container '{}' ({}):\n\n{}",
                name, actual_runtime, explanation_resp.explanation.unwrap_or_default()
            )),
            warnings: explanation_resp.warnings,
            exec_result: Some(result),
            ..AgentResponse::new(Intent::ContainerExec)
        }
    }

    pub fn container_sandbox_run(&self, command: &str, sandbox: &str, image: &str) -> AgentResponse {
        let level = match sandbox.to_lowercase().as_str() {
            "workspace" => SandboxLevel::Workspace,
            "restricted" => SandboxLevel::Restricted,
            "locked" => SandboxLevel::Locked,
            _ => SandboxLevel::Restricted,
        };
        let explanation_resp = self.explain(command);
        let executor = SandboxExecutor::new();
        let result = executor.run(command, level, None, image, 30.0);

        AgentResponse {
            intent: Intent::ContainerSandbox,
            pipeline: Some(command.into()),
            explanation: Some(format!(
                "Sandboxed execution ({} profile):\n{}\n\n{}",
                sandbox, SandboxExecutor::describe_sandbox(level),
                explanation_resp.explanation.unwrap_or_default()
            )),
            warnings: explanation_resp.warnings,
            exec_result: Some(result),
            ..AgentResponse::new(Intent::ContainerSandbox)
        }
    }

    pub fn container_state(&self, name: Option<&str>) -> AgentResponse {
        if let Some(name) = name {
            let state = PodmanTool::state(name);
            let inspect_result = PodmanTool::inspect(name);
            let mut explanation = format!("Container '{}':\n  State: {:?}\n", name, state);
            if state == ContainerState::Exited {
                if let Some(code) = PodmanTool::exit_code(name) {
                    explanation.push_str(&format!("  Exit code: {}\n", code));
                }
            }
            AgentResponse {
                intent: Intent::ContainerState,
                explanation: Some(explanation),
                exec_result: Some(inspect_result),
                ..AgentResponse::new(Intent::ContainerState)
            }
        } else {
            let podman_result = PodmanTool::ps(true);
            let toolbox_result = ToolboxTool::list_containers();
            let mut parts = Vec::new();
            if toolbox_result.ok() && !toolbox_result.stdout.trim().is_empty() {
                parts.push(format!("Toolbox containers:\n{}", toolbox_result.stdout.trim()));
            }
            if podman_result.ok() && !podman_result.stdout.trim().is_empty() {
                parts.push(format!("Podman containers:\n{}", podman_result.stdout.trim()));
            }
            if parts.is_empty() {
                parts.push("No containers found.".into());
            }
            AgentResponse {
                intent: Intent::ContainerState,
                explanation: Some(parts.join("\n\n")),
                ..AgentResponse::new(Intent::ContainerState)
            }
        }
    }

    pub fn container_lifecycle(&self, name: &str, action: &str, force: bool) -> AgentResponse {
        let result = match action {
            "start" => PodmanTool::start(name),
            "stop" => PodmanTool::stop(name, 10),
            "pause" => PodmanTool::pause(name),
            "unpause" => PodmanTool::unpause(name),
            "remove" => {
                if ToolboxTool::exists(name) {
                    ToolboxTool::remove(name, force)
                } else {
                    PodmanTool::remove(name, force)
                }
            }
            _ => {
                return AgentResponse {
                    intent: Intent::ContainerLifecycle,
                    explanation: Some(format!("Unknown action: {}. Valid: start, stop, pause, unpause, remove", action)),
                    warnings: vec![format!("Unknown action: {}", action)],
                    ..AgentResponse::new(Intent::ContainerLifecycle)
                };
            }
        };

        let new_state = PodmanTool::state(name);
        AgentResponse {
            intent: Intent::ContainerLifecycle,
            explanation: Some(format!("Container '{}': {} → state is now {:?}", name, action, new_state)),
            exec_result: Some(result),
            ..AgentResponse::new(Intent::ContainerLifecycle)
        }
    }

    pub fn raw_container_cmd(&self, runtime: &str, subcommand: &str) -> AgentResponse {
        let cmd = format!("{} {}", runtime, subcommand);
        let result = sg_core::exec::exec(&cmd);
        AgentResponse {
            intent: Intent::ContainerState,
            pipeline: Some(cmd),
            explanation: Some(format!("Raw {} command", runtime)),
            exec_result: Some(result),
            ..AgentResponse::new(Intent::ContainerState)
        }
    }
}
