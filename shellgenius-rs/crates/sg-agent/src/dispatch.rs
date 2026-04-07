//! Dispatch tool implementations.

use sg_core::dispatch::*;
use sg_core::types::{AgentResponse, Intent};

use crate::context::ShellGeniusAgent;

impl ShellGeniusAgent {
    pub fn dispatch_route(&self, source: &str, target: &str, container: Option<&str>) -> AgentResponse {
        let plan = plan_dispatch(source, target, container);
        let mut explanation_parts = vec![plan.explanation.clone()];

        // Find matching shims
        let matching_shims: Vec<&Shim> = SHIMS.iter().filter(|s| {
            plan.dispatch_types.contains(&s.from_type) || plan.dispatch_types.contains(&s.to_type)
        }).collect();

        if !matching_shims.is_empty() {
            explanation_parts.push("\nShims used:".into());
            for shim in &matching_shims {
                explanation_parts.push(format!("  {}: {}", shim.name, shim.description));
            }
        }

        if !plan.dispatch_types.is_empty() {
            let chain: Vec<String> = plan.dispatch_types.iter().map(|dt| format!("{:?}", dt)).collect();
            explanation_parts.push(format!("\nDispatch chain: {}", chain.join(" → ")));
        }

        AgentResponse {
            intent: Intent::Dispatch,
            pipeline: Some(plan.command),
            explanation: Some(explanation_parts.join("\n")),
            knowledge_refs: matching_shims.iter().map(|s| format!("shim:{}", s.name)).collect(),
            ..AgentResponse::new(Intent::Dispatch)
        }
    }

    pub fn mime_query(&self, file_or_type: &str) -> AgentResponse {
        // Determine if it's a MIME type or file path
        let mime_type = if file_or_type.contains('/') && !file_or_type.starts_with('/') {
            Some(file_or_type.to_string())
        } else {
            query_file_mime(file_or_type)
        };

        let Some(mime) = mime_type else {
            return AgentResponse {
                intent: Intent::MimeQuery,
                explanation: Some(format!("Could not determine MIME type for: {}", file_or_type)),
                ..AgentResponse::new(Intent::MimeQuery)
            };
        };

        if let Some(handler) = query_mime_handler(&mime) {
            AgentResponse {
                intent: Intent::MimeQuery,
                pipeline: Some(format!("xdg-open {}", file_or_type)),
                explanation: Some(format!(
                    "MIME type: {}\nHandler:   {}\nApp:       {}\nCommand:   {}\n\nTo open:   xdg-open {}",
                    handler.mime_type, handler.desktop_file,
                    if handler.app_name.is_empty() { "(unknown)" } else { &handler.app_name },
                    if handler.exec_cmd.is_empty() { "(unknown)" } else { &handler.exec_cmd },
                    file_or_type
                )),
                ..AgentResponse::new(Intent::MimeQuery)
            }
        } else {
            AgentResponse {
                intent: Intent::MimeQuery,
                explanation: Some(format!("No handler registered for MIME type: {}", mime)),
                ..AgentResponse::new(Intent::MimeQuery)
            }
        }
    }

    pub fn dispatch_introspect(&self) -> AgentResponse {
        let report = introspect_dispatch_system();
        AgentResponse {
            intent: Intent::Introspect,
            explanation: Some(serde_json::to_string_pretty(&report).unwrap_or_default()),
            ..AgentResponse::new(Intent::Introspect)
        }
    }
}
