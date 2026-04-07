//! ANSI color and style helpers.
//!
//! Respects NO_COLOR, TERM=dumb, and non-TTY detection.

use std::io::IsTerminal;
use std::sync::LazyLock;

static COLOR_ENABLED: LazyLock<bool> = LazyLock::new(|| {
    if std::env::var("NO_COLOR").is_ok() {
        return false;
    }
    if std::env::var("TERM").as_deref() == Ok("dumb") {
        return false;
    }
    std::io::stderr().is_terminal()
});

pub fn supports_color() -> bool {
    *COLOR_ENABLED
}

fn sgr(code: &str, text: &str) -> String {
    if !supports_color() {
        return text.to_string();
    }
    format!("\x1b[{}m{}\x1b[0m", code, text)
}

pub fn bold(t: &str) -> String { sgr("1", t) }
pub fn dim(t: &str) -> String { sgr("2", t) }
pub fn italic(t: &str) -> String { sgr("3", t) }
pub fn red(t: &str) -> String { sgr("31", t) }
pub fn green(t: &str) -> String { sgr("32", t) }
pub fn yellow(t: &str) -> String { sgr("33", t) }
pub fn blue(t: &str) -> String { sgr("34", t) }
pub fn magenta(t: &str) -> String { sgr("35", t) }
pub fn cyan(t: &str) -> String { sgr("36", t) }
pub fn bold_green(t: &str) -> String { sgr("1;32", t) }
pub fn bold_red(t: &str) -> String { sgr("1;31", t) }
pub fn bold_cyan(t: &str) -> String { sgr("1;36", t) }
pub fn bold_yellow(t: &str) -> String { sgr("1;33", t) }
pub fn reverse(t: &str) -> String { sgr("7", t) }

// Unicode symbols with ASCII fallback
static UNICODE: LazyLock<bool> = LazyLock::new(|| {
    std::env::var("LANG").map(|l| l.contains("UTF-8")).unwrap_or(false)
        || std::env::var("TERM_PROGRAM").map(|t| ["iTerm.app", "WezTerm", "kitty"].contains(&t.as_str())).unwrap_or(false)
});

pub fn sym_arrow() -> &'static str { if *UNICODE { "›" } else { ">" } }
pub fn sym_check() -> &'static str { if *UNICODE { "✓" } else { "[ok]" } }
pub fn sym_cross() -> &'static str { if *UNICODE { "✗" } else { "[!!]" } }
pub fn sym_warn() -> &'static str { if *UNICODE { "⚠" } else { "[!]" } }
pub fn sym_tool() -> &'static str { if *UNICODE { "⚙" } else { "[*]" } }
pub fn sym_search() -> &'static str { if *UNICODE { "⊕" } else { "[?]" } }
pub fn sym_pipe() -> &'static str { if *UNICODE { "│" } else { "|" } }
pub fn sym_dot() -> &'static str { if *UNICODE { "·" } else { "." } }
pub fn sym_brain() -> &'static str { if *UNICODE { "◆" } else { "[~]" } }

pub const PROMPT_NAME: &str = "sg";
