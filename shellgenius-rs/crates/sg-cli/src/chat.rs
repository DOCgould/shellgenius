//! Interactive chat loop with context tracking and slash commands.

use sg_ui::ansi::*;
use sg_ui::output::*;
use sg_ui::spinner::Spinner;

use crate::llm::ShellGeniusLLM;

pub fn interactive_chat(llm: &mut ShellGeniusLLM) {
    print_banner();

    let info = {
        let _s = Spinner::new("Detecting environment...");
        llm.agent.setup()
    };
    print_env_info(&info);

    // Probe backend
    let ctx_window = llm.config.context_window();
    let ctx_str = if ctx_window >= 1000 { format!("{}k", ctx_window / 1000) } else { ctx_window.to_string() };
    let backend = llm.config.backend_info().clone();

    if !backend.gguf_file.is_empty() {
        print_kv("API", &format!("{}  {}", llm.config.base_url, dim(&format!("gguf: {}", backend.gguf_file))));
    } else {
        print_kv("API", &llm.config.base_url);
    }
    print_kv("Context", &format!("{} tokens {}", ctx_str, dim("(from llama.cpp /slots)")));

    if backend.thinking_supported {
        let thinking_status = if llm.config.thinking {
            format!("on (budget: {})", llm.config.thinking_budget)
        } else {
            "off".into()
        };
        print_kv("Thinking", &format!("{}  {}", thinking_status,
            dim(&format!("supported via {} [{}]", backend.thinking_mechanism, backend.reasoning_formats.join(", ")))));
    }

    print_kv("Tools", &format!("{} registered", llm.get_tools().len()));
    status_ok("Ready");
    print_chat_help();

    loop {
        let user_input = prompt(llm);
        let input = user_input.trim();
        if input.is_empty() { continue; }

        match input {
            "quit" | "exit" | "q" => {
                eprintln!("\n  {}\n", dim("Bye."));
                break;
            }
            "/reset" => {
                llm.reset();
                status_ok("Conversation cleared — context freed");
                continue;
            }
            "/help" => {
                print_chat_help();
                continue;
            }
            "/tools" => {
                for t in llm.get_tools() {
                    let name = t.get("name").and_then(|v| v.as_str()).unwrap_or("?");
                    let desc = t.get("description").and_then(|v| v.as_str()).unwrap_or("");
                    let short = if desc.len() > 70 { &desc[..70] } else { desc };
                    println!("  {:<30} {}", bold(name), dim(short));
                }
                continue;
            }
            "/env" => {
                let info = llm.agent.setup();
                print_env_info(&info);
                continue;
            }
            "/think" => {
                llm.config.thinking = true;
                if backend.thinking_supported {
                    status_ok(&format!("Thinking enabled (budget: {} tokens)", llm.config.thinking_budget));
                } else {
                    status_warn("Thinking enabled, but backend may not support it");
                }
                continue;
            }
            "/nothink" => {
                llm.config.thinking = false;
                status_ok("Thinking disabled");
                continue;
            }
            "/context" => {
                let pct = llm.context_pct();
                let used = llm.context_used();
                let remaining = llm.context_remaining();
                print_kv("Context window", &format!("{} tokens", ctx_window));
                print_kv("Used", &format!("~{} tokens ({:.1}%)", used, pct));
                print_kv("Remaining", &format!("~{} tokens", remaining));
                print_kv("Turns", &llm.turn_count.to_string());
                // Visual bar
                let bar_width = 40;
                let filled = (bar_width as f64 * pct / 100.0) as usize;
                let empty = bar_width - filled;
                let bar = if pct > 80.0 {
                    format!("{}{}", bold_red(&"█".repeat(filled)), dim(&"░".repeat(empty)))
                } else if pct > 50.0 {
                    format!("{}{}", yellow(&"█".repeat(filled)), dim(&"░".repeat(empty)))
                } else {
                    format!("{}{}", green(&"█".repeat(filled)), dim(&"░".repeat(empty)))
                };
                println!("  {} {:.0}%", bar, pct);
                continue;
            }
            "/edit" => {
                if let Some(text) = sg_ui::output::editor_input() {
                    status_ok(&format!("Editor input: {} chars", text.len()));
                    match llm.chat(&text) {
                        Ok(response) => println!("\n{}\n", response),
                        Err(e) => status_error(&format!("Error: {}", e)),
                    }
                } else {
                    status("Cancelled");
                }
                continue;
            }
            _ => {}
        }

        // Check for /think N
        if input.starts_with("/think ") {
            if let Ok(budget) = input[7..].trim().parse::<u32>() {
                llm.config.thinking = true;
                llm.config.thinking_budget = budget;
                status_ok(&format!("Thinking enabled (budget: {} tokens)", budget));
            } else {
                status_error("Usage: /think [budget_tokens]  e.g. /think 2000");
            }
            continue;
        }

        match llm.chat(input) {
            Ok(response) => println!("\n{}\n", response),
            Err(e) => status_error(&format!("Error: {}", e)),
        }
    }
}

fn prompt(llm: &ShellGeniusLLM) -> String {
    use std::io::Write;
    if llm.turn_count == 0 {
        // Clean first-turn prompt
        if supports_color() {
            eprint!("  {} {} ", bold_cyan(PROMPT_NAME), dim(sym_arrow()));
        } else {
            eprint!("  {}> ", PROMPT_NAME);
        }
    } else {
        // With context indicator
        let pct = llm.context_pct();
        let remaining = llm.context_remaining();
        let remain_str = if remaining > 1000 { format!("{}k", remaining / 1000) } else { remaining.to_string() };
        let ctx_indicator = if pct > 80.0 {
            bold_red(&format!("[{:.0}% | {} left]", pct, remain_str))
        } else if pct > 50.0 {
            yellow(&format!("[{:.0}% | {} left]", pct, remain_str))
        } else {
            dim(&format!("[{:.0}% | {} left]", pct, remain_str))
        };

        if supports_color() {
            eprint!("  {} {} {} ", bold_cyan(PROMPT_NAME), ctx_indicator, dim(sym_arrow()));
        } else {
            eprint!("  {} [{:.0}%]> ", PROMPT_NAME, pct);
        }
    }
    let _ = std::io::stderr().flush();
    let mut line = String::new();
    std::io::stdin().read_line(&mut line).unwrap_or(0);
    line.trim().to_string()
}
