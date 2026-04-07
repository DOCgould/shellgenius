//! Terminal spinner — shows activity during async operations.

use indicatif::{ProgressBar, ProgressStyle};
use std::time::Duration;

use crate::ansi::supports_color;

/// A spinner that writes to stderr. Use as a guard — drop to clear.
pub struct Spinner {
    pb: Option<ProgressBar>,
}

impl Spinner {
    pub fn new(message: &str) -> Self {
        if !supports_color() || !std::io::IsTerminal::is_terminal(&std::io::stderr()) {
            // Non-TTY: just print the status once
            eprintln!("  {} {}", crate::ansi::sym_dot(), message);
            return Self { pb: None };
        }

        let pb = ProgressBar::new_spinner();
        pb.set_style(
            ProgressStyle::with_template("  {spinner:.cyan} {msg}")
                .unwrap()
                .tick_strings(&["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏", " "]),
        );
        pb.set_message(message.to_string());
        pb.enable_steady_tick(Duration::from_millis(80));
        Self { pb: Some(pb) }
    }

    pub fn update(&self, message: &str) {
        if let Some(pb) = &self.pb {
            pb.set_message(message.to_string());
        }
    }
}

impl Drop for Spinner {
    fn drop(&mut self) {
        if let Some(pb) = self.pb.take() {
            pb.finish_and_clear();
        }
    }
}
