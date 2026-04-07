//! Formatted output functions — status messages, tables, banners.

use crate::ansi::*;

// ---------------------------------------------------------------------------
// Status messages — to stderr (watchdog)
// ---------------------------------------------------------------------------

pub fn status(msg: &str) {
    eprintln!("  {} {}", dim(sym_dot()), dim(msg));
}

pub fn status_tool(tool_name: &str, detail: &str) {
    let detail_str = if detail.is_empty() { String::new() } else { format!(" {}", dim(detail)) };
    eprintln!("  {} {}{}", cyan(sym_tool()), bold(tool_name), detail_str);
}

pub fn status_search(query: &str) {
    let short = if query.len() > 60 { format!("{}...", &query[..60]) } else { query.to_string() };
    eprintln!("  {} {} {}", magenta(sym_search()), dim("searching:"), italic(&short));
}

pub fn status_ok(msg: &str) {
    eprintln!("  {} {}", green(sym_check()), msg);
}

pub fn status_warn(msg: &str) {
    eprintln!("  {} {}", yellow(sym_warn()), yellow(msg));
}

pub fn status_error(msg: &str) {
    eprintln!("  {} {}", red(sym_cross()), bold_red(msg));
}

// ---------------------------------------------------------------------------
// Formatted output — to stdout
// ---------------------------------------------------------------------------

pub fn print_header(title: &str, subtitle: &str) {
    println!("\n{}", bold(title));
    if !subtitle.is_empty() {
        println!("{}", dim(subtitle));
    }
}

pub fn print_pipeline(command: &str) {
    println!("\n  {}\n", bold_green(command));
}

pub fn print_warning(msg: &str) {
    println!("  {} {}", yellow(sym_warn()), yellow(msg));
}

pub fn print_error(msg: &str) {
    println!("  {} {}", red(sym_cross()), red(msg));
}

pub fn print_success(msg: &str) {
    println!("  {} {}", green(sym_check()), green(msg));
}

pub fn print_kv(key: &str, value: &str) {
    println!("  {} {}", dim(&format!("{}:", key)), value);
}

pub fn print_table(headers: &[&str], rows: &[Vec<String>]) {
    if rows.is_empty() { return; }
    let mut widths: Vec<usize> = headers.iter().map(|h| h.len()).collect();
    for row in rows {
        for (i, cell) in row.iter().enumerate() {
            if i < widths.len() {
                widths[i] = widths[i].max(cell.len());
            }
        }
    }
    let header_line: String = headers.iter().zip(&widths)
        .map(|(h, w)| bold(&format!("{:<width$}", h, width = w)))
        .collect::<Vec<_>>().join("  ");
    println!("  {}", header_line);
    let rule: String = widths.iter().map(|w| "─".repeat(*w)).collect::<Vec<_>>().join("──");
    println!("  {}", dim(&rule));
    for row in rows {
        let cells: String = row.iter().enumerate()
            .map(|(i, cell)| {
                let w = widths.get(i).copied().unwrap_or(cell.len());
                format!("{:<width$}", cell, width = w)
            })
            .collect::<Vec<_>>().join("  ");
        println!("  {}", cells);
    }
}

pub fn print_exec_result(command: &str, exit_code: i32, stdout: &str, stderr: &str, elapsed_ms: f64, max_lines: usize) {
    let badge = if exit_code == 0 { green("exit 0") } else { red(&format!("exit {}", exit_code)) };
    let timing = dim(&format!("{:.0}ms", elapsed_ms));
    println!("\n  {} {} {}", badge, timing, dim(command));

    if !stdout.trim().is_empty() {
        for line in stdout.trim().lines().take(max_lines) {
            println!("  {} {}", sym_pipe(), line);
        }
        let total = stdout.trim().lines().count();
        if total > max_lines {
            println!("  {}", dim(&format!("... {} more lines", total - max_lines)));
        }
    }
    if !stderr.trim().is_empty() && exit_code != 0 {
        println!("  {}", red("stderr:"));
        for line in stderr.trim().lines().take(5) {
            println!("  {} {}", sym_pipe(), red(line));
        }
    }
}

// ---------------------------------------------------------------------------
// Banner
// ---------------------------------------------------------------------------

pub fn print_banner() {
    if supports_color() {
        eprintln!("\n  {} {}", bold_cyan("ShellGenius"), dim("v0.1.0"));
        eprintln!("  {}\n", dim("Expert shell agent — pipes, containers, dispatch"));
    } else {
        eprintln!("\n  ShellGenius v0.1.0");
        eprintln!("  Expert shell agent — pipes, containers, dispatch\n");
    }
}

pub fn print_env_info(info: &serde_json::Value) {
    let shell = info["shell"].as_str().unwrap_or("unknown");
    let version = info["version"].as_str().unwrap_or("");
    let tools_count = info["tools_available"].as_u64().unwrap_or(0);
    let modern: Vec<&str> = info["modern_tools"].as_array()
        .map(|a| a.iter().filter_map(|v| v.as_str()).collect())
        .unwrap_or_default();
    let cwd = info["cwd"].as_str().unwrap_or(".");

    print_kv("Shell", &format!("{} {}", shell, dim(&format!("({})", version))));
    print_kv("Tools", &format!("{} detected  modern: {}",
        tools_count,
        if modern.is_empty() { dim("none").to_string() } else { modern.join(", ") }
    ));
    print_kv("CWD", cwd);
}

pub fn print_chat_help() {
    println!(r#"
  {}
    {}  Enable thinking mode (model reasons before answering)
    {}  Enable thinking with N token budget
    {}  Disable thinking mode
    {}  Show context window usage (with visual bar)
    {}  Clear conversation and free context
    {}  Open $EDITOR for multiline input
    {}  List available tools
    {}  Show environment info
    {}  Show this help
    {}      Exit

  {}
    {} Ask questions naturally: {}
    {} Paste commands to explain: {}
    {} Ask for safe execution: {}
    {} Use {} to compose long prompts in your editor
    {} Context indicator appears in prompt after first turn
"#,
        bold("Commands:"),
        dim("/think"), dim("/think N"), dim("/nothink"),
        dim("/context"), dim("/reset"), dim("/edit"),
        dim("/tools"), dim("/env"), dim("/help"), dim("quit"),
        bold("Tips:"),
        dim(sym_arrow()), italic("\"how do I count unique IPs?\""),
        dim(sym_arrow()), italic("\"what does find . -print0 | xargs -0 grep do?\""),
        dim(sym_arrow()), italic("\"run ls -la in a locked sandbox\""),
        dim(sym_arrow()), bold("/edit"),
        dim(sym_arrow()),
    );
}

/// Editor input: open $EDITOR on a temp file.
pub fn editor_input() -> Option<String> {
    use std::io::Write;
    let editor = std::env::var("VISUAL")
        .or_else(|_| std::env::var("EDITOR"))
        .unwrap_or_else(|_| "vi".into());

    let mut tmpfile = tempfile::NamedTempFile::new().ok()?;
    write!(tmpfile, "# Type your prompt here. Save and quit to send.\n# Lines starting with # are stripped.\n# Empty file = cancel.\n\n").ok()?;
    let path = tmpfile.path().to_path_buf();

    let status = std::process::Command::new(&editor).arg(&path).status().ok()?;
    if !status.success() { return None; }

    let text = std::fs::read_to_string(&path).ok()?;
    let cleaned: Vec<&str> = text.lines().filter(|l| !l.starts_with('#')).collect();
    let result = cleaned.join("\n").trim().to_string();
    if result.is_empty() { None } else { Some(result) }
}
