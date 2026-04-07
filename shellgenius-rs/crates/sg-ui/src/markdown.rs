//! Markdown-to-Terminal renderer using pulldown-cmark.
//!
//! Converts LLM markdown output into ANSI-styled terminal text.

use pulldown_cmark::{Event, Parser, Tag, TagEnd, CodeBlockKind};
use crate::ansi::*;

pub fn render_markdown(text: &str) -> String {
    let parser = Parser::new(text);
    let mut output = String::new();
    let mut in_code_block = false;
    let mut code_lang = String::new();
    let mut code_lines: Vec<String> = Vec::new();
    let mut list_depth: usize = 0;
    let mut ordered_index: Option<u64> = None;

    for event in parser {
        match event {
            Event::Start(Tag::Heading { level, .. }) => {
                output.push('\n');
                let _ = level; // used below in End
            }
            Event::End(TagEnd::Heading(level)) => {
                // The text was already pushed; we just add formatting context
                // We need to retroactively style — simpler approach: handle in Text
                let _ = level;
                output.push('\n');
            }
            Event::Start(Tag::CodeBlock(kind)) => {
                in_code_block = true;
                code_lines.clear();
                code_lang = match kind {
                    CodeBlockKind::Fenced(lang) => lang.to_string(),
                    _ => String::new(),
                };
            }
            Event::End(TagEnd::CodeBlock) => {
                in_code_block = false;
                // Render the code block with borders
                let lang_label = if code_lang.is_empty() { String::new() } else { format!(" {}", dim(&code_lang)) };
                output.push_str(&format!("\n  {}{}\n", dim("┌──"), lang_label));
                for line in &code_lines {
                    let colored = syntax_color_line(line, &code_lang);
                    output.push_str(&format!("  {} {}\n", dim("│"), colored));
                }
                output.push_str(&format!("  {}\n\n", dim("└──")));
                code_lines.clear();
            }
            Event::Start(Tag::List(start)) => {
                list_depth += 1;
                ordered_index = start;
            }
            Event::End(TagEnd::List(_)) => {
                list_depth = list_depth.saturating_sub(1);
                ordered_index = None;
            }
            Event::Start(Tag::Item) => {}
            Event::End(TagEnd::Item) => {
                output.push('\n');
            }
            Event::Start(Tag::BlockQuote(_)) => {}
            Event::End(TagEnd::BlockQuote(_)) => {}
            Event::Start(Tag::Emphasis) => {
                if supports_color() { output.push_str("\x1b[3m"); }
            }
            Event::End(TagEnd::Emphasis) => {
                if supports_color() { output.push_str("\x1b[0m"); }
            }
            Event::Start(Tag::Strong) => {
                if supports_color() { output.push_str("\x1b[1m"); }
            }
            Event::End(TagEnd::Strong) => {
                if supports_color() { output.push_str("\x1b[0m"); }
            }
            Event::Code(code) => {
                output.push_str(&reverse(&format!(" {} ", code)));
            }
            Event::Text(text) => {
                if in_code_block {
                    code_lines.push(text.to_string());
                } else {
                    output.push_str(&format!("  {}", text));
                }
            }
            Event::SoftBreak | Event::HardBreak => {
                output.push('\n');
            }
            Event::Rule => {
                output.push_str(&format!("  {}\n", dim(&"─".repeat(50))));
            }
            Event::Start(Tag::Paragraph) => {}
            Event::End(TagEnd::Paragraph) => {
                output.push('\n');
            }
            Event::Start(Tag::Link { dest_url, .. }) => {
                let _ = dest_url; // we'll show it in End
            }
            _ => {}
        }
    }

    output
}

fn syntax_color_line(line: &str, lang: &str) -> String {
    if !supports_color() { return line.to_string(); }
    if !matches!(lang, "bash" | "sh" | "shell" | "zsh" | "") {
        return line.to_string();
    }

    let trimmed = line.trim_start();

    // Comments
    if trimmed.starts_with('#') {
        return dim(line);
    }

    let mut result = line.to_string();

    // Pipe operators
    result = result.replace(" | ", &bold_yellow(" | "));

    // Simple string coloring (double quotes)
    // Just bold the first word as command name for readability
    if let Some(first_non_space) = result.find(|c: char| !c.is_whitespace()) {
        let prefix = &result[..first_non_space];
        let rest = &result[first_non_space..];
        if let Some(space_pos) = rest.find(|c: char| c.is_whitespace()) {
            let cmd = &rest[..space_pos];
            let args = &rest[space_pos..];
            result = format!("{}{}{}", prefix, bold(cmd), args);
        }
    }

    result
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_render_basic_markdown() {
        let md = "# Hello\n\nSome text with **bold** and `code`.";
        let rendered = render_markdown(md);
        assert!(rendered.contains("Hello"));
        assert!(rendered.contains("text"));
    }

    #[test]
    fn test_render_code_block() {
        let md = "```bash\necho hello\n```";
        let rendered = render_markdown(md);
        assert!(rendered.contains("echo"));
        assert!(rendered.contains("┌") || rendered.contains("["));
    }

    #[test]
    fn test_render_empty() {
        let rendered = render_markdown("");
        assert!(rendered.is_empty() || rendered.trim().is_empty());
    }
}
