use serde::{Deserialize, Serialize};

/// All errors in ShellGenius.
#[derive(Debug, thiserror::Error)]
pub enum SgError {
    #[error("pipeline error: {0}")]
    Pipeline(String),
    #[error("command blocked by safety filter: {0}")]
    Blocked(String),
    #[error("execution timeout after {0:.1}s")]
    Timeout(f64),
    #[error("shell execution failed: {0}")]
    Exec(#[from] std::io::Error),
    #[error("container error: {0}")]
    Container(String),
    #[error("dispatch error: {0}")]
    Dispatch(String),
    #[error("LLM API error: {0}")]
    Llm(String),
    #[error("unknown tool: {0}")]
    UnknownTool(String),
    #[error("knowledge unavailable: {0}")]
    Knowledge(String),
}

pub type SgResult<T> = Result<T, SgError>;

/// Result of executing a shell command.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ExecResult {
    pub command: String,
    pub exit_code: i32,
    pub stdout: String,
    pub stderr: String,
    pub elapsed_ms: f64,
    #[serde(default)]
    pub truncated: bool,
    #[serde(default)]
    pub dry_run: bool,
}

impl ExecResult {
    pub fn ok(&self) -> bool {
        self.exit_code == 0
    }

    pub fn summary(&self, max_lines: usize) -> String {
        let lines: Vec<&str> = self.stdout.trim().lines().collect();
        let mut out: Vec<&str> = lines.iter().take(max_lines).copied().collect();
        if lines.len() > max_lines {
            out.push("...");
        }
        let status = if self.ok() {
            "OK".to_string()
        } else {
            format!("FAIL (exit {})", self.exit_code)
        };
        format!(
            "[{} in {:.0}ms] {}\n{}",
            status,
            self.elapsed_ms,
            self.command,
            out.join("\n")
        )
    }
}

/// What the user/LLM is trying to do.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum Intent {
    BuildPipeline,
    ExplainPipeline,
    FixPipeline,
    OptimizePipeline,
    TranslateShell,
    QuotingHelp,
    FdRedirect,
    FindTool,
    Execute,
    ContainerCreate,
    ContainerExec,
    ContainerState,
    ContainerLifecycle,
    ContainerSandbox,
    Dispatch,
    MimeQuery,
    Introspect,
}

/// What the agent returns to the user/LLM.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AgentResponse {
    pub intent: Intent,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub pipeline: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub explanation: Option<String>,
    #[serde(skip_serializing_if = "Vec::is_empty")]
    pub warnings: Vec<String>,
    #[serde(skip_serializing_if = "Vec::is_empty")]
    pub alternatives: Vec<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub exec_result: Option<ExecResult>,
    #[serde(skip_serializing_if = "Vec::is_empty")]
    pub knowledge_refs: Vec<String>,
}

impl AgentResponse {
    pub fn new(intent: Intent) -> Self {
        Self {
            intent,
            pipeline: None,
            explanation: None,
            warnings: Vec::new(),
            alternatives: Vec::new(),
            exec_result: None,
            knowledge_refs: Vec::new(),
        }
    }
}
