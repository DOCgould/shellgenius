//! Shell tool implementations: compose, explain, fix_quoting, translate, fd_help, find_tool, run.

use sg_core::corpus::*;
use sg_core::exec::{execute, ExecMode};
use sg_core::pipe::{explain_pipeline, Pipeline, PipeStage};
use sg_core::types::{AgentResponse, Intent};

use crate::context::ShellGeniusAgent;

impl ShellGeniusAgent {
    pub fn compose_pipeline(&self, description: &str) -> AgentResponse {
        let matching = self.match_idioms(description);
        let mut resp = AgentResponse::new(Intent::BuildPipeline);
        if let Some(first) = matching.first() {
            resp.pipeline = Some(first.pattern.clone());
            resp.explanation = Some(first.explanation.clone());
            resp.knowledge_refs = matching.iter().map(|m| format!("idiom:{}", m.name)).collect();
            if !first.gotchas.is_empty() {
                resp.warnings.push(first.gotchas.clone());
            }
            for m in matching.iter().skip(1).take(2) {
                resp.alternatives.push(format!("{}  # {}", m.pattern, m.explanation));
            }
        }
        resp
    }

    pub fn explain(&self, command: &str) -> AgentResponse {
        let stages = explain_pipeline(command);
        let explanation_parts: Vec<String> = stages.iter().enumerate().map(|(i, s)| {
            format!("Stage {}: {}\n  Command: {}\n  Purpose: {}", i + 1, s.tool, s.command, s.explanation)
        }).collect();

        let mut p = Pipeline::new();
        for s in &stages {
            let _ = p.add(PipeStage::new(&s.command));
        }
        let warnings = p.validate();

        AgentResponse {
            intent: Intent::ExplainPipeline,
            pipeline: Some(command.into()),
            explanation: Some(explanation_parts.join("\n\n")),
            warnings,
            ..AgentResponse::new(Intent::ExplainPipeline)
        }
    }

    pub fn fix_quoting(&self, broken_command: &str) -> AgentResponse {
        let mut issues = Vec::new();

        // Check for $() inside single quotes
        if broken_command.contains("$(") && broken_command.contains('\'') {
            let mut in_single = false;
            for (i, c) in broken_command.chars().enumerate() {
                if c == '\'' { in_single = !in_single; }
                if c == '$' && in_single && broken_command.get(i+1..i+2) == Some("(") {
                    issues.push(format!(
                        "Command substitution $() at position {} is inside single quotes — it won't expand. Use double quotes instead.", i
                    ));
                }
            }
        }

        // Check for unquoted variables
        let re = regex_lite::Regex::new(r#"(?<!")\$\w+"#).unwrap();
        for m in re.find_iter(broken_command) {
            issues.push(format!(
                "Variable {} may be unquoted — risk of word splitting. Use \"{}\" instead.",
                m.as_str(), m.as_str()
            ));
        }

        // Try shlex.split
        let mut suggestions = Vec::new();
        match shlex::split(broken_command) {
            Some(tokens) => suggestions.push(format!("shlex parses OK into {} tokens", tokens.len())),
            None => {
                issues.push("shlex parse error: unmatched quotes".into());
                suggestions.push("Fix unmatched quotes before proceeding".into());
            }
        }

        let refs: Vec<String> = QUOTING_RULES.iter().map(|r| format!("rule:{}", r.name)).collect();

        AgentResponse {
            intent: Intent::QuotingHelp,
            pipeline: Some(broken_command.into()),
            explanation: Some(if issues.is_empty() {
                "No obvious quoting issues found.".into()
            } else {
                issues.join("\n")
            }),
            warnings: issues,
            alternatives: suggestions,
            knowledge_refs: refs,
            ..AgentResponse::new(Intent::QuotingHelp)
        }
    }

    pub fn translate(&self, command: &str, from_shell: Shell, to_shell: Shell) -> AgentResponse {
        let mut notes = Vec::new();
        let from_name = format!("{:?}", from_shell).to_lowercase();
        let to_name = format!("{:?}", to_shell).to_lowercase();

        for (feature, compat) in COMPAT_NOTES.iter() {
            if let (Some(from_note), Some(to_note)) = (compat.get(&from_name), compat.get(&to_name)) {
                if to_note.contains("NOT SUPPORTED") {
                    notes.push(format!(
                        "Feature '{}' used in {:?} is NOT available in {:?}: {}",
                        feature, from_shell, to_shell, to_note
                    ));
                }
                let _ = from_note; // used for the check
            }
        }

        AgentResponse {
            intent: Intent::TranslateShell,
            pipeline: Some(command.into()),
            explanation: Some(format!("Translation from {:?} to {:?}", from_shell, to_shell)),
            warnings: notes,
            knowledge_refs: COMPAT_NOTES.keys().map(|k| format!("compat:{}", k)).collect(),
            ..AgentResponse::new(Intent::TranslateShell)
        }
    }

    pub fn fd_help(&self, description: &str) -> AgentResponse {
        let tricks = lookup_fd_tricks(Some(self.ctx.shell_enum));
        let lower = description.to_lowercase();
        let words: Vec<&str> = lower.split_whitespace().collect();

        let matching: Vec<_> = tricks.iter().filter(|t| {
            let name_lower = t.name.to_lowercase();
            let expl_lower = t.explanation.to_lowercase();
            words.iter().any(|w| name_lower.contains(w) || expl_lower.contains(w))
        }).collect();

        let items: Vec<&FdTrick> = if matching.is_empty() {
            tricks.iter().take(3).collect()
        } else {
            matching
        };

        let parts: Vec<String> = items.iter().map(|t| {
            format!("**{}**\n  Pattern: `{}`\n  {}", t.name, t.pattern, t.explanation)
        }).collect();

        AgentResponse {
            intent: Intent::FdRedirect,
            explanation: Some(parts.join("\n\n")),
            knowledge_refs: items.iter().map(|t| format!("fd:{}", t.name)).collect(),
            ..AgentResponse::new(Intent::FdRedirect)
        }
    }

    pub fn find_best_tool(&self, task: &str) -> AgentResponse {
        let lower = task.to_lowercase();
        let mut recommendations = Vec::new();

        let tool_map: Vec<(&str, Vec<&str>)> = vec![
            ("search", if self.ctx.has("rg") { vec!["rg", "grep"] } else { vec!["grep"] }),
            ("find file", if self.ctx.has("fd") { vec!["fd", "find"] } else { vec!["find"] }),
            ("json", vec!["jq"]),
            ("csv", vec!["awk", "cut"]),
            ("parallel", if self.ctx.has("parallel") { vec!["parallel", "xargs -P"] } else { vec!["xargs -P"] }),
            ("replace", if self.ctx.has("sd") { vec!["sd", "sed"] } else { vec!["sed"] }),
            ("view", if self.ctx.has("bat") { vec!["bat", "cat"] } else { vec!["cat", "less"] }),
            ("diff", if self.ctx.has("delta") { vec!["delta", "diff"] } else { vec!["diff"] }),
            ("count lines", if self.ctx.has("tokei") { vec!["tokei", "wc -l"] } else { vec!["wc -l"] }),
            ("benchmark", if self.ctx.has("hyperfine") { vec!["hyperfine"] } else { vec!["time"] }),
            ("disk usage", if self.ctx.has("dust") { vec!["dust", "du"] } else { vec!["du -sh"] }),
            ("process", if self.ctx.has("procs") { vec!["procs", "ps"] } else { vec!["ps aux"] }),
            ("fuzzy", if self.ctx.has("fzf") { vec!["fzf"] } else { vec!["grep -i"] }),
        ];

        for (keyword, tools) in &tool_map {
            if lower.contains(keyword) {
                for t in tools {
                    let base = t.split_whitespace().next().unwrap_or(t);
                    let available = if self.ctx.has(base) { "installed" } else { "not found" };
                    recommendations.push(format!("{} ({})", t, available));
                }
            }
        }

        AgentResponse {
            intent: Intent::FindTool,
            explanation: Some(if recommendations.is_empty() {
                "No specific tool recommendation. Describe the task in more detail.".into()
            } else {
                format!("Recommended tools:\n{}", recommendations.iter().map(|r| format!("  - {}", r)).collect::<Vec<_>>().join("\n"))
            }),
            alternatives: recommendations,
            ..AgentResponse::new(Intent::FindTool)
        }
    }

    pub fn run(&self, command: &str, confirm: bool) -> AgentResponse {
        let explanation_resp = self.explain(command);

        let mode = if confirm || !self.ctx.dry_run {
            ExecMode::Execute
        } else {
            ExecMode::DryRun
        };

        let result = execute(
            command,
            Some(self.ctx.cwd.as_path()),
            30.0,
            1_048_576,
            mode,
            &self.ctx.shell,
        );

        AgentResponse {
            intent: Intent::Execute,
            pipeline: Some(command.into()),
            explanation: explanation_resp.explanation,
            warnings: explanation_resp.warnings,
            exec_result: Some(result),
            ..AgentResponse::new(Intent::Execute)
        }
    }

    // --- Internal ---

    fn match_idioms(&self, description: &str) -> Vec<PipeIdiom> {
        let lower = description.to_lowercase();
        let words: std::collections::HashSet<&str> = lower.split_whitespace().collect();
        let idioms = &*PIPE_IDIOMS;
        let mut scored: Vec<(usize, &PipeIdiom)> = idioms.iter()
            .filter_map(|idiom| {
                let searchable = format!("{} {} {:?}", idiom.name, idiom.explanation, idiom.category).to_lowercase();
                let score = words.iter().filter(|w| searchable.contains(**w)).count();
                if score > 0 && idiom.for_shell(self.ctx.shell_enum) {
                    Some((score, idiom))
                } else {
                    None
                }
            })
            .collect();
        scored.sort_by(|a, b| b.0.cmp(&a.0));
        scored.into_iter().map(|(_, idiom)| idiom.clone()).collect()
    }
}
