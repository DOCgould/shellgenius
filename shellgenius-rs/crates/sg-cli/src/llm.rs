//! LLM Bridge — connects ShellGenius tools to a local Anthropic API.

use std::time::Duration;

use anyhow::{Context, Result};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};

use sg_agent::ShellGeniusAgent;

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------

#[derive(Debug, Clone)]
pub struct LLMConfig {
    pub base_url: String,
    pub api_key: String,
    pub model: String,
    pub max_tokens: u32,
    pub thinking: bool,
    pub thinking_budget: u32,
    context_window: Option<u32>,
    backend_info: Option<BackendInfo>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BackendInfo {
    pub route_mode: String,
    pub local_model: String,
    pub local_url: String,
    pub gguf_file: String,
    pub n_ctx: u32,
    pub speculative: bool,
    pub thinking_supported: bool,
    pub thinking_mechanism: String,
    pub reasoning_formats: Vec<String>,
}

impl LLMConfig {
    pub fn new(base_url: &str, api_key: &str, model: &str) -> Self {
        Self {
            base_url: base_url.into(),
            api_key: api_key.into(),
            model: model.into(),
            max_tokens: 4096,
            thinking: false,
            thinking_budget: 1024,
            context_window: None,
            backend_info: None,
        }
    }

    pub fn context_window(&mut self) -> u32 {
        if let Some(cw) = self.context_window {
            return cw;
        }
        let cw = probe_context_window(&self.base_url);
        self.context_window = Some(cw);
        cw
    }

    pub fn backend_info(&mut self) -> &BackendInfo {
        if self.backend_info.is_none() {
            self.backend_info = Some(probe_backend_info(&self.base_url));
        }
        self.backend_info.as_ref().unwrap()
    }
}

impl Default for LLMConfig {
    fn default() -> Self {
        Self::new("http://localhost:8082", "test", "claude-sonnet-4-20250514")
    }
}

// ---------------------------------------------------------------------------
// Backend probing — get FACTS from the running llama.cpp instance
// ---------------------------------------------------------------------------

fn fetch_json(url: &str) -> Option<Value> {
    let client = reqwest::blocking::Client::builder()
        .timeout(Duration::from_secs(3))
        .build()
        .ok()?;
    let resp = client.get(url).send().ok()?;
    resp.json().ok()
}

fn probe_context_window(base_url: &str) -> u32 {
    // Step 1: router /health → local_url
    let health = fetch_json(&format!("{}/health", base_url));
    let backend_url = health.as_ref()
        .and_then(|h| h.get("local_url"))
        .and_then(|v| v.as_str())
        .map(|s| s.to_string());

    if let Some(ref url) = backend_url {
        // Step 2: backend /slots → n_ctx
        if let Some(slots) = fetch_json(&format!("{}/slots", url)) {
            if let Some(n_ctx) = slots.as_array()
                .and_then(|a| a.first())
                .and_then(|s| s.get("n_ctx"))
                .and_then(|v| v.as_u64())
            {
                if n_ctx > 0 { return n_ctx as u32; }
            }
        }
    }

    // Step 3: try base_url itself
    if let Some(slots) = fetch_json(&format!("{}/slots", base_url)) {
        if let Some(n_ctx) = slots.as_array()
            .and_then(|a| a.first())
            .and_then(|s| s.get("n_ctx"))
            .and_then(|v| v.as_u64())
        {
            if n_ctx > 0 { return n_ctx as u32; }
        }
    }

    32_768 // conservative fallback
}

fn probe_backend_info(base_url: &str) -> BackendInfo {
    let mut info = BackendInfo {
        route_mode: "unknown".into(),
        local_model: String::new(),
        local_url: String::new(),
        gguf_file: String::new(),
        n_ctx: 0,
        speculative: false,
        thinking_supported: false,
        thinking_mechanism: String::new(),
        reasoning_formats: vec![],
    };

    // Router health
    if let Some(health) = fetch_json(&format!("{}/health", base_url)) {
        info.route_mode = health.get("route_mode").and_then(|v| v.as_str()).unwrap_or("unknown").into();
        info.local_model = health.get("local_model").and_then(|v| v.as_str()).unwrap_or("").into();
        info.local_url = health.get("local_url").and_then(|v| v.as_str()).unwrap_or("").into();
    }

    if info.local_url.is_empty() { return info; }

    // Backend models
    if let Some(models) = fetch_json(&format!("{}/v1/models", info.local_url)) {
        info.gguf_file = models.get("models")
            .and_then(|v| v.as_array())
            .and_then(|a| a.first())
            .and_then(|m| m.get("model"))
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .into();
    }

    // Backend slots
    if let Some(slots) = fetch_json(&format!("{}/slots", info.local_url)) {
        if let Some(first) = slots.as_array().and_then(|a| a.first()) {
            info.n_ctx = first.get("n_ctx").and_then(|v| v.as_u64()).unwrap_or(0) as u32;
            info.speculative = first.get("speculative").and_then(|v| v.as_bool()).unwrap_or(false);
        }
    }

    // Backend props (thinking support)
    if let Some(props) = fetch_json(&format!("{}/props", info.local_url)) {
        let template = props.get("chat_template").and_then(|v| v.as_str()).unwrap_or("");
        if template.contains("enable_thinking") || template.contains("<|think|>") {
            info.thinking_supported = true;
            info.reasoning_formats = vec!["none".into(), "deepseek".into()];
            if template.contains("channel") {
                info.thinking_mechanism = "channel_tags".into();
            }
        }
    }

    info
}

// ---------------------------------------------------------------------------
// ShellGeniusLLM — the agent loop
// ---------------------------------------------------------------------------

pub struct ShellGeniusLLM {
    pub config: LLMConfig,
    pub agent: ShellGeniusAgent,
    pub conversation: Vec<Value>,
    pub total_input_tokens: u64,
    pub total_output_tokens: u64,
    pub turn_count: u32,
    client: reqwest::blocking::Client,
}

impl ShellGeniusLLM {
    pub fn new(config: LLMConfig, agent: ShellGeniusAgent) -> Self {
        let client = reqwest::blocking::Client::builder()
            .timeout(Duration::from_secs(120))
            .build()
            .expect("Failed to create HTTP client");
        Self {
            config,
            agent,
            conversation: Vec::new(),
            total_input_tokens: 0,
            total_output_tokens: 0,
            turn_count: 0,
            client,
        }
    }

    pub fn context_used(&self) -> u64 {
        self.total_input_tokens + self.total_output_tokens
    }

    pub fn context_remaining(&self) -> u64 {
        let window = self.config.context_window.unwrap_or(32768) as u64;
        window.saturating_sub(self.context_used())
    }

    pub fn context_pct(&self) -> f64 {
        let window = self.config.context_window.unwrap_or(32768) as f64;
        if window == 0.0 { return 0.0; }
        (self.context_used() as f64 / window) * 100.0
    }

    pub fn get_tools(&self) -> Vec<Value> {
        let mut tools: Vec<Value> = self.agent.as_tools().into_iter().map(|t| {
            json!({
                "name": t["name"],
                "description": t["description"],
                "input_schema": t["parameters"],
            })
        }).collect();

        // Knowledge tools
        tools.push(json!({
            "name": "knowledge_query",
            "description": "Search The Linux Programming Interface (Kerrisk) for deep syscall and kernel knowledge.",
            "input_schema": {"type":"object","properties":{"query":{"type":"string","description":"Natural language query about Linux internals"},"top_k":{"type":"integer","description":"Number of results (default: 3)"}},"required":["query"]},
        }));
        tools.push(json!({
            "name": "knowledge_ingest",
            "description": "Ingest a file or directory into the vector knowledge base.",
            "input_schema": {"type":"object","properties":{"path":{"type":"string","description":"Path to ingest"}},"required":["path"]},
        }));
        tools.push(json!({
            "name": "knowledge_search_all",
            "description": "Search ALL registered knowledge indices.",
            "input_schema": {"type":"object","properties":{"query":{"type":"string"},"top_k":{"type":"integer"}},"required":["query"]},
        }));
        tools.push(json!({
            "name": "knowledge_list_indices",
            "description": "List all registered vector knowledge indices.",
            "input_schema": {"type":"object","properties":{}},
        }));

        tools
    }

    pub fn handle_tool(&self, name: &str, input: &Value) -> String {
        // Knowledge tools handled here (outside agent dispatch)
        match name {
            "knowledge_query" | "knowledge_ingest" | "knowledge_search_all" | "knowledge_list_indices" => {
                return json!({"info": "Knowledge tools require sg-knowledge feature"}).to_string();
            }
            _ => {}
        }

        // Agent tools
        match self.agent.handle_tool_call(name, input) {
            Ok(result) => serde_json::to_string_pretty(&result).unwrap_or_default(),
            Err(e) => json!({"error": e.to_string()}).to_string(),
        }
    }

    pub fn chat(&mut self, user_message: &str) -> Result<String> {
        use sg_ui::output::*;
        use sg_ui::spinner::Spinner;
        use sg_ui::markdown::render_markdown;

        self.conversation.push(json!({
            "role": "user",
            "content": user_message,
        }));

        let tools = self.get_tools();

        for _turn in 0..10 {
            let think_label = if self.config.thinking { "Thinking deeply..." } else { "Thinking..." };
            let response = {
                let _spinner = Spinner::new(think_label);
                self.api_call(&tools)?
            };

            let content = response.get("content").and_then(|v| v.as_array()).cloned().unwrap_or_default();
            let stop_reason = response.get("stop_reason").and_then(|v| v.as_str()).unwrap_or("");

            // Track tokens
            if let Some(usage) = response.get("usage") {
                let in_tok = usage.get("input_tokens").and_then(|v| v.as_u64()).unwrap_or(0);
                let out_tok = usage.get("output_tokens").and_then(|v| v.as_u64()).unwrap_or(0);
                self.total_input_tokens = in_tok;
                self.total_output_tokens += out_tok;
                self.turn_count += 1;

                let pct = self.context_pct();
                let remaining = self.context_remaining();
                let remain_str = if remaining > 1000 { format!("{}k", remaining / 1000) } else { remaining.to_string() };
                if pct > 80.0 {
                    status_warn(&format!("context: {:.0}% used, {} remaining", pct, remain_str));
                } else {
                    status(&format!("tokens: {} in, {} out  context: {:.0}% ({} left)", in_tok, out_tok, pct, remain_str));
                }
            }

            self.conversation.push(json!({
                "role": "assistant",
                "content": content,
            }));

            // If no tool use, we're done
            if stop_reason != "tool_use" {
                let text_parts: Vec<&str> = content.iter()
                    .filter_map(|b| {
                        if b.get("type").and_then(|v| v.as_str()) == Some("text") {
                            b.get("text").and_then(|v| v.as_str())
                        } else {
                            None
                        }
                    })
                    .collect();
                let raw = if text_parts.is_empty() { "(no response)".to_string() } else { text_parts.join("\n") };
                return Ok(render_markdown(&raw));
            }

            // Handle tool calls
            let tool_calls: Vec<&Value> = content.iter()
                .filter(|b| b.get("type").and_then(|v| v.as_str()) == Some("tool_use"))
                .collect();

            let mut tool_results = Vec::new();
            for block in &tool_calls {
                let tool_name = block.get("name").and_then(|v| v.as_str()).unwrap_or("");
                let tool_input = block.get("input").cloned().unwrap_or(json!({}));
                let tool_id = block.get("id").and_then(|v| v.as_str()).unwrap_or("");

                // Watchdog
                if tool_name == "knowledge_query" {
                    let q = tool_input.get("query").and_then(|v| v.as_str()).unwrap_or("");
                    status_search(q);
                } else {
                    let detail = tool_input.as_object()
                        .and_then(|m| m.values().next())
                        .and_then(|v| v.as_str())
                        .unwrap_or("");
                    let short = if detail.len() > 60 { &detail[..60] } else { detail };
                    status_tool(tool_name, short);
                }

                let result_str = {
                    let _spinner = Spinner::new(&format!("Running {}...", tool_name));
                    self.handle_tool(tool_name, &tool_input)
                };

                tool_results.push(json!({
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "content": result_str,
                }));
            }

            self.conversation.push(json!({
                "role": "user",
                "content": tool_results,
            }));
        }

        Ok("(max tool-use turns reached)".into())
    }

    pub fn reset(&mut self) {
        self.conversation.clear();
        self.total_input_tokens = 0;
        self.total_output_tokens = 0;
        self.turn_count = 0;
    }

    fn api_call(&self, tools: &[Value]) -> Result<Value> {
        let mut body = json!({
            "model": self.config.model,
            "max_tokens": self.config.max_tokens,
            "messages": self.conversation,
            "system": self.system_prompt(),
            "tools": tools,
        });

        if self.config.thinking {
            body["thinking"] = json!({
                "type": "enabled",
                "budget_tokens": self.config.thinking_budget,
            });
        }

        let resp = self.client
            .post(&format!("{}/v1/messages", self.config.base_url))
            .header("Content-Type", "application/json")
            .header("x-api-key", &self.config.api_key)
            .header("anthropic-version", "2023-06-01")
            .json(&body)
            .send()
            .context("Failed to connect to LLM API")?;

        let result: Value = resp.json().context("Failed to parse API response")?;

        if let Some(err) = result.get("error") {
            anyhow::bail!("API error: {}", err);
        }

        Ok(result)
    }

    fn system_prompt(&self) -> String {
        let user = std::env::var("USER").unwrap_or_else(|_| "unknown".into());
        let shell = &self.agent.ctx.shell;
        let cwd = self.agent.ctx.cwd.to_string_lossy();

        indoc::formatdoc! {"
            # Identity
            You are ShellGenius, an expert-level shell agent that helps users compose, explain, debug, translate, and execute shell commands. You operate on a real Linux system and have tools that can actually run commands.

            # Environment
            - User: {user}
            - Shell: {shell}
            - Working directory: {cwd}

            # Your Tools
            You have 21 tools. Use them actively — don't just answer from memory when a tool would give a better answer.

            ## Shell Tools
            - `shell_compose` — Build pipelines from natural language. USE WHEN: \"how do I...\"
            - `shell_explain` — Break down an existing pipeline. ALWAYS use before running complex commands.
            - `shell_fix_quoting` — Detect quoting bugs. USE WHEN: quotes, variables, command substitution.
            - `shell_translate` — Convert between bash/zsh/fish/posix.
            - `shell_fd_help` — fd swaps, coprocs, named pipes, flock.
            - `shell_find_tool` — Recommend best tool for a job. Prefers rg over grep, fd over find.
            - `shell_run` — Execute with safety checks. Dry-run by default. Set confirm=true only when user approves.

            ## Container Tools
            - `container_create` — Create toolbox (rich dev env) or podman (sandbox).
            - `container_exec` — Run command in existing container.
            - `container_sandbox_run` — One-shot sandboxed execution. Levels: workspace, restricted, locked.
            - `container_state` — Inspect or list containers.
            - `container_lifecycle` — start/stop/pause/unpause/remove.
            - `podman_raw` / `toolbox_raw` — Direct CLI for advanced ops.

            ## Dispatch Tools
            - `dispatch_route` — Route content: pipe→viewer, dbus→pipe, pipe→clipboard, pipe→notification.
            - `mime_query` — What app handles this file/type?
            - `dispatch_introspect` — Full system introspection.

            ## Knowledge Tools
            - `knowledge_query` — Search TLPI for syscall-level precision. Don't guess — query.
            - `knowledge_search_all` — Search all ingested knowledge indices.
            - `knowledge_ingest` — Ingest files/dirs into the vector index.
            - `knowledge_list_indices` — List what's been ingested.

            # Rules
            1. NEVER run destructive commands without explicit user approval.
            2. Default to DRY RUN. Set confirm=true only when user says \"run it\".
            3. ALWAYS explain before executing. Call shell_explain first.
            4. Lead with the command. Explain each pipe stage.
            5. Warn about gotchas: unquoted vars, sort before uniq, UUOC.
            6. Don't guess at syscall semantics — use knowledge_query.
        "}
    }
}
