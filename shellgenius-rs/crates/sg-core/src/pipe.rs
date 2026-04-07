//! Pipe Algebra — composing shell pipelines programmatically.
//!
//! A Unix pipeline is a directed acyclic graph of transforms.
//! Each stage has typed stdin/stdout (text lines, JSON, binary, TSV, etc.)
//! and the algebra ensures type-compatible composition.

use std::collections::HashMap;
use std::sync::LazyLock;

use serde::{Deserialize, Serialize};

use crate::types::{SgError, SgResult};

// ---------------------------------------------------------------------------
// StreamType — what flows through the pipe
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum StreamType {
    TextLines,
    Tsv,
    Csv,
    Json,
    JsonLines,
    Binary,
    NullDelim,
    KeyValue,
}

// ---------------------------------------------------------------------------
// PipeStage — a single stage in a pipeline
// ---------------------------------------------------------------------------

#[derive(Debug, Clone)]
pub struct PipeStage {
    pub command: String,
    pub args: Vec<String>,
    pub input_type: StreamType,
    pub output_type: StreamType,
    pub description: String,
    pub safe: bool,
}

impl PipeStage {
    pub fn new(command: &str) -> Self {
        Self {
            command: command.into(),
            args: Vec::new(),
            input_type: StreamType::TextLines,
            output_type: StreamType::TextLines,
            description: String::new(),
            safe: true,
        }
    }

    pub fn with_types(mut self, input: StreamType, output: StreamType) -> Self {
        self.input_type = input;
        self.output_type = output;
        self
    }

    pub fn with_args(mut self, args: Vec<String>) -> Self {
        self.args = args;
        self
    }

    pub fn full_command(&self) -> String {
        if self.args.is_empty() {
            self.command.clone()
        } else {
            let quoted: Vec<String> = self.args.iter().map(|a| shlex::try_quote(a).unwrap_or_else(|_| a.clone().into()).into_owned()).collect();
            format!("{} {}", self.command, quoted.join(" "))
        }
    }
}

// ---------------------------------------------------------------------------
// Pipeline — ordered sequence of PipeStages with type-checked composition
// ---------------------------------------------------------------------------

#[derive(Debug, Clone)]
pub struct Pipeline {
    pub stages: Vec<PipeStage>,
    pub description: String,
}

impl Pipeline {
    pub fn new() -> Self {
        Self {
            stages: Vec::new(),
            description: String::new(),
        }
    }

    pub fn add(&mut self, stage: PipeStage) -> SgResult<&mut Self> {
        if let Some(prev) = self.stages.last() {
            if !types_compatible(prev.output_type, stage.input_type) {
                return Err(SgError::Pipeline(format!(
                    "Type mismatch: {:?} outputs {:?} but {:?} expects {:?}. \
                     Insert a converter stage (e.g., jq -r '.[]' for JSON→TEXT).",
                    prev.full_command(),
                    prev.output_type,
                    stage.full_command(),
                    stage.input_type,
                )));
            }
        }
        self.stages.push(stage);
        Ok(self)
    }

    pub fn render(&self) -> String {
        self.stages
            .iter()
            .map(|s| s.full_command())
            .collect::<Vec<_>>()
            .join(" | ")
    }

    pub fn render_explained(&self) -> String {
        self.stages
            .iter()
            .enumerate()
            .map(|(i, s)| {
                let sep = if i == 0 { "" } else { "  | " };
                let comment = if s.description.is_empty() {
                    String::new()
                } else {
                    format!("  # {}", s.description)
                };
                format!("{}{}{}", sep, s.full_command(), comment)
            })
            .collect::<Vec<_>>()
            .join(" \\\n")
    }

    pub fn has_side_effects(&self) -> bool {
        self.stages.iter().any(|s| !s.safe)
    }

    pub fn output_type(&self) -> Option<StreamType> {
        self.stages.last().map(|s| s.output_type)
    }

    pub fn validate(&self) -> Vec<String> {
        let mut warnings = Vec::new();
        for (i, stage) in self.stages.iter().enumerate() {
            let cmd_base = stage.command.split_whitespace().next().unwrap_or("");

            // UUOC: cat at start of multi-stage pipeline
            if cmd_base == "cat" && i == 0 && self.stages.len() > 1 {
                warnings.push(format!(
                    "Stage 0: Useless Use of Cat (UUOC). \
                     Feed the file directly to the next command: < file {}",
                    self.stages[1].full_command()
                ));
            }

            // Multiple greps can often be combined
            if cmd_base == "grep" {
                let grep_count = self
                    .stages
                    .iter()
                    .filter(|s| s.command.split_whitespace().next() == Some("grep"))
                    .count();
                if grep_count >= 3 {
                    warnings.push(format!(
                        "Pipeline has {} grep stages. \
                         Consider combining with: grep -E 'pat1|pat2|pat3' \
                         or using awk for complex filtering.",
                        grep_count
                    ));
                }
            }

            // Double sort
            if cmd_base == "sort" && i > 0 {
                let prev_base = self.stages[i - 1]
                    .command
                    .split_whitespace()
                    .next()
                    .unwrap_or("");
                if prev_base == "sort" {
                    warnings.push(format!(
                        "Stage {}: Double sort detected. This is likely redundant.",
                        i
                    ));
                }
            }
        }
        warnings
    }
}

impl Default for Pipeline {
    fn default() -> Self {
        Self::new()
    }
}

// ---------------------------------------------------------------------------
// Type compatibility
// ---------------------------------------------------------------------------

fn types_compatible(output: StreamType, input: StreamType) -> bool {
    if input == StreamType::TextLines {
        return true; // universal receiver
    }
    if output == input {
        return true;
    }
    if output == StreamType::NullDelim && input == StreamType::NullDelim {
        return true;
    }
    if output == StreamType::JsonLines
        && (input == StreamType::Json || input == StreamType::TextLines)
    {
        return true;
    }
    false
}

// ---------------------------------------------------------------------------
// chain() helper
// ---------------------------------------------------------------------------

pub fn chain(stages: Vec<PipeStage>) -> SgResult<Pipeline> {
    let mut p = Pipeline::new();
    for s in stages {
        p.add(s)?;
    }
    Ok(p)
}

// ---------------------------------------------------------------------------
// Tool explanations
// ---------------------------------------------------------------------------

pub static TOOL_EXPLANATIONS: LazyLock<HashMap<&'static str, &'static str>> = LazyLock::new(|| {
    HashMap::from([
        ("grep", "Filter lines matching a pattern"),
        ("sed", "Stream editor — find and replace in-flight"),
        ("awk", "Pattern-action language for columnar data"),
        ("sort", "Sort lines (lexicographic by default, -n for numeric)"),
        ("uniq", "Deduplicate adjacent identical lines (sort first!)"),
        ("cut", "Extract fields/columns by delimiter"),
        ("tr", "Transliterate or delete characters"),
        ("head", "Take first N lines"),
        ("tail", "Take last N lines (or follow with -f)"),
        ("wc", "Count lines (-l), words (-w), or bytes (-c)"),
        ("tee", "Duplicate stream: one to file, one continues in pipe"),
        ("xargs", "Convert stdin lines into command arguments"),
        ("find", "Recursively search for files by criteria"),
        ("jq", "JSON processor — the awk of structured data"),
        ("cat", "Concatenate files (often useless at pipe start — see UUOC)"),
        ("parallel", "GNU parallel — run commands concurrently"),
        ("pv", "Pipe viewer — show throughput and progress"),
        ("comm", "Set operations on sorted files (intersection, difference)"),
        ("paste", "Merge lines from multiple inputs side-by-side"),
        ("join", "Database-style join on sorted files by key field"),
        ("column", "Format stdin into aligned columns"),
        ("rev", "Reverse each line character-by-character"),
        ("tac", "Reverse line order (cat backwards)"),
        ("nl", "Number lines"),
        ("fmt", "Reflow text to a given width"),
        ("fold", "Wrap lines to a given width (byte-level)"),
        ("expand", "Convert tabs to spaces"),
        ("unexpand", "Convert spaces to tabs"),
        ("xdg-open", "Open file/URL with registered desktop app (MIME-routed)"),
        ("gio", "GNOME I/O — open, trash, mount, copy with MIME dispatch"),
        ("xdg-mime", "Query or set MIME type handlers"),
        ("toolbox", "Toolbox — run commands in a rich containerized dev environment"),
        ("podman", "Podman — rootless container engine (create, run, exec, stop, inspect)"),
    ])
});

// ---------------------------------------------------------------------------
// explain_pipeline — break a raw pipeline into explained stages
// ---------------------------------------------------------------------------

pub fn explain_pipeline(cmd_string: &str) -> Vec<ExplainedStage> {
    let stages_raw = split_pipeline(cmd_string);
    stages_raw
        .iter()
        .map(|raw| {
            let raw = raw.trim();
            let base = raw.split_whitespace().next().unwrap_or("");
            ExplainedStage {
                command: raw.to_string(),
                tool: base.to_string(),
                explanation: explain_tool(base, raw),
            }
        })
        .collect()
}

#[derive(Debug, Clone, Serialize)]
pub struct ExplainedStage {
    pub command: String,
    pub tool: String,
    pub explanation: String,
}

fn explain_tool(base: &str, full_cmd: &str) -> String {
    let base_explanation = TOOL_EXPLANATIONS
        .get(base)
        .map(|s| s.to_string())
        .unwrap_or_else(|| format!("Run {}", base));

    if base == "sort" && full_cmd.contains("-rn") {
        return format!("{} (descending numeric order)", base_explanation);
    }
    if base == "sort" && full_cmd.contains("-n") {
        return format!("{} (numeric order)", base_explanation);
    }
    if base == "grep" && full_cmd.contains("-v") {
        return "Invert match — exclude lines matching pattern".into();
    }
    if base == "grep" && full_cmd.contains("-c") {
        return "Count matching lines".into();
    }
    if base == "grep" && full_cmd.contains("-l") {
        return "List files containing matches".into();
    }
    if base == "xargs" && full_cmd.contains("-P") {
        return format!("{} (parallel execution)", base_explanation);
    }
    if base == "xargs" && full_cmd.contains("-0") {
        return format!("{} (null-delimited input)", base_explanation);
    }

    base_explanation
}

// ---------------------------------------------------------------------------
// split_pipeline — split on pipes, respecting quotes
// ---------------------------------------------------------------------------

pub fn split_pipeline(cmd: &str) -> Vec<String> {
    let mut stages = Vec::new();
    let mut current = String::new();
    let mut in_single = false;
    let mut in_double = false;
    let chars: Vec<char> = cmd.chars().collect();
    let mut i = 0;

    while i < chars.len() {
        let c = chars[i];
        if c == '\'' && !in_double {
            in_single = !in_single;
        } else if c == '"' && !in_single {
            in_double = !in_double;
        } else if c == '|' && !in_single && !in_double {
            // Check for || (logical OR) — don't split on that
            if i + 1 < chars.len() && chars[i + 1] == '|' {
                current.push('|');
                current.push('|');
                i += 2;
                continue;
            }
            stages.push(current.clone());
            current.clear();
            i += 1;
            continue;
        }
        current.push(c);
        i += 1;
    }
    if !current.is_empty() {
        stages.push(current);
    }
    stages
}

// ---------------------------------------------------------------------------
// safely_quote_command
// ---------------------------------------------------------------------------

pub fn safely_quote_command(cmd: &str) -> SgResult<String> {
    let tokens = shlex::split(cmd)
        .ok_or_else(|| SgError::Pipeline(format!("Malformed command string: {}", cmd)))?;
    Ok(tokens
        .iter()
        .map(|t| shlex::try_quote(t).unwrap_or_else(|_| t.clone().into()).into_owned())
        .collect::<Vec<_>>()
        .join(" "))
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_basic_pipeline_render() {
        let mut p = Pipeline::new();
        p.add(PipeStage::new("grep ERROR")).unwrap();
        p.add(PipeStage::new("wc -l")).unwrap();
        assert_eq!(p.render(), "grep ERROR | wc -l");
    }

    #[test]
    fn test_type_checked_composition() {
        let mut p = Pipeline::new();
        p.add(
            PipeStage::new("find . -print0")
                .with_types(StreamType::TextLines, StreamType::NullDelim),
        )
        .unwrap();
        p.add(
            PipeStage::new("xargs -0 grep ERROR")
                .with_types(StreamType::NullDelim, StreamType::TextLines),
        )
        .unwrap();
        assert!(p.render().contains("xargs -0"));
    }

    #[test]
    fn test_type_mismatch_raises() {
        let mut p = Pipeline::new();
        p.add(
            PipeStage::new("cat data.json")
                .with_types(StreamType::TextLines, StreamType::Json),
        )
        .unwrap();
        let result = p.add(
            PipeStage::new("xargs -0 echo")
                .with_types(StreamType::NullDelim, StreamType::TextLines),
        );
        assert!(result.is_err());
    }

    #[test]
    fn test_chain_helper() {
        let p = chain(vec![
            PipeStage::new("ls -la"),
            PipeStage::new("grep py"),
            PipeStage::new("wc -l"),
        ])
        .unwrap();
        assert_eq!(p.stages.len(), 3);
        assert_eq!(p.render(), "ls -la | grep py | wc -l");
    }

    #[test]
    fn test_uuoc_warning() {
        let mut p = Pipeline::new();
        p.add(PipeStage::new("cat file.txt")).unwrap();
        p.add(PipeStage::new("grep ERROR")).unwrap();
        let warnings = p.validate();
        assert!(warnings.iter().any(|w| w.contains("UUOC")));
    }

    #[test]
    fn test_explain_pipeline() {
        let stages = explain_pipeline("grep ERROR | sort | uniq -c | head -10");
        assert_eq!(stages.len(), 4);
        assert_eq!(stages[0].tool, "grep");
        assert_eq!(stages[3].tool, "head");
    }

    #[test]
    fn test_split_pipeline_respects_quotes() {
        let stages = split_pipeline("echo 'hello | world' | grep hello");
        assert_eq!(stages.len(), 2);
    }

    #[test]
    fn test_safely_quote_command() {
        let safe = safely_quote_command("echo hello world").unwrap();
        assert_eq!(safe, "echo hello world");
    }

    #[test]
    fn test_safely_quote_catches_bad_quoting() {
        let result = safely_quote_command("echo 'unterminated");
        assert!(result.is_err());
    }

    #[test]
    fn test_args_quoting() {
        let stage = PipeStage::new("grep").with_args(vec![
            "-r".into(),
            "hello world".into(),
            ".".into(),
        ]);
        let cmd = stage.full_command();
        assert!(cmd.contains("'hello world'") || cmd.contains("\"hello world\""));
    }
}
