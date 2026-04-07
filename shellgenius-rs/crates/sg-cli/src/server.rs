//! OpenClaw HTTP server — 5 endpoints on port 9747.

use std::sync::Arc;

use axum::{
    extract::State,
    routing::{get, post},
    Json, Router,
};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};

use sg_agent::ShellGeniusAgent;

type SharedAgent = Arc<std::sync::Mutex<ShellGeniusAgent>>;

pub fn router(agent: ShellGeniusAgent) -> Router {
    let shared: SharedAgent = Arc::new(std::sync::Mutex::new(agent));
    Router::new()
        .route("/health", get(health))
        .route("/manifest", get(manifest))
        .route("/tools", get(tools))
        .route("/tool-call", post(tool_call))
        .route("/setup", post(setup))
        .with_state(shared)
}

async fn health() -> Json<Value> {
    Json(json!({"status": "ok", "version": "0.1.0"}))
}

async fn manifest(State(agent): State<SharedAgent>) -> Json<Value> {
    let agent = agent.lock().unwrap();
    let tools = agent.as_tools();
    Json(json!({
        "id": "shellgenius",
        "name": "ShellGenius",
        "version": "0.1.0",
        "tools": tools,
    }))
}

async fn tools(State(agent): State<SharedAgent>) -> Json<Value> {
    let agent = agent.lock().unwrap();
    Json(json!({"tools": agent.as_tools()}))
}

#[derive(Deserialize)]
struct ToolCallRequest {
    name: String,
    parameters: Value,
}

#[derive(Serialize)]
struct ToolCallResponse {
    result: Value,
}

async fn tool_call(
    State(agent): State<SharedAgent>,
    Json(req): Json<ToolCallRequest>,
) -> Json<ToolCallResponse> {
    let agent = agent.lock().unwrap();
    let result = match agent.handle_tool_call(&req.name, &req.parameters) {
        Ok(v) => v,
        Err(e) => json!({"error": e.to_string()}),
    };
    Json(ToolCallResponse { result })
}

async fn setup(State(agent): State<SharedAgent>) -> Json<Value> {
    let mut agent = agent.lock().unwrap();
    let info = agent.setup();
    Json(json!({"result": info}))
}

pub async fn serve(agent: ShellGeniusAgent, host: &str, port: u16) -> anyhow::Result<()> {
    let app = router(agent);
    let addr = format!("{}:{}", host, port);
    sg_ui::output::print_banner();
    sg_ui::output::status_ok(&format!("Skill server listening on {}", addr));

    let listener = tokio::net::TcpListener::bind(&addr).await?;
    axum::serve(listener, app).await?;
    Ok(())
}
