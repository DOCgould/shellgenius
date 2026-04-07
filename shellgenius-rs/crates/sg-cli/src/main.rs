mod llm;
mod server;
mod chat;

use anyhow::Result;
use clap::{Parser, Subcommand};

use sg_agent::ShellGeniusAgent;
use sg_core::corpus::Shell;
use sg_core::pipe::explain_pipeline;
use sg_ui::ansi::*;
use sg_ui::output::*;
use sg_ui::spinner::Spinner;

#[derive(Parser)]
#[command(name = "shellgenius", about = "Expert shell agent for pipes, containers, and dispatch")]
struct Cli {
    #[command(subcommand)]
    command: Option<Commands>,
}

#[derive(Subcommand)]
enum Commands {
    /// Explain a shell pipeline stage-by-stage
    Explain { pipeline: String },
    /// Compose a pipeline from description
    Compose { description: String },
    /// Analyze and fix quoting issues
    FixQuoting { command: String },
    /// Translate between shell dialects
    Translate {
        command: String,
        #[arg(long, default_value = "bash")]
        from: String,
        #[arg(long, default_value = "posix")]
        to: String,
    },
    /// Get help with file descriptor operations
    FdHelp { description: String },
    /// Find the best tool for a task
    FindTool { task: String },
    /// Run a command with safety checks
    Run {
        cmd: String,
        #[arg(long)]
        confirm: bool,
    },
    /// List available shell tools
    Tools,
    /// Ingest files/directories into FAISS knowledge base
    Ingest {
        /// File or directory to ingest
        path: String,
        /// Custom output directory
        #[arg(long)]
        output: Option<String>,
    },
    /// Ingest man pages into FAISS knowledge base
    IngestMan {
        /// Specific page names (e.g., bash grep pipe fork)
        pages: Vec<String>,
        /// Man section(s) to ingest
        #[arg(long = "section", short = 's', action = clap::ArgAction::Append)]
        sections: Vec<String>,
        /// Ingest curated shell-relevant man pages (~80 pages)
        #[arg(long)]
        shell: bool,
        /// Ingest ALL pages from specified sections
        #[arg(long)]
        all: bool,
    },
    /// List all registered FAISS knowledge indices
    Indices,
    /// Interactive LLM chat
    Chat {
        #[arg(long, default_value = "http://localhost:8082")]
        url: String,
        #[arg(long, default_value = "claude-sonnet-4-20250514")]
        model: String,
        #[arg(long, default_value = "test")]
        key: String,
        #[arg(long)]
        ask: Option<String>,
    },
    /// Start OpenClaw skill server
    Serve {
        #[arg(long, default_value_t = 9747)]
        port: u16,
        #[arg(long, default_value = "127.0.0.1")]
        host: String,
    },
}

#[tokio::main]
async fn main() -> Result<()> {
    let cli = Cli::parse();

    let Some(command) = cli.command else {
        print_banner();
        // Print help by re-parsing with --help
        let _ = Cli::try_parse_from(["shellgenius", "--help"]);
        return Ok(());
    };

    match command {
        Commands::Explain { pipeline } => {
            let agent = ShellGeniusAgent::new();
            print_header("Pipeline Breakdown", &pipeline);
            let stages = explain_pipeline(&pipeline);
            println!();
            for (i, s) in stages.iter().enumerate() {
                println!("  {} {}  {}", dim(&format!("{}.", i + 1)), bold(&s.tool), s.command.trim());
                println!("     {}", dim(&s.explanation));
            }
            let resp = agent.explain(&pipeline);
            for w in &resp.warnings {
                print_warning(w);
            }
        }

        Commands::Compose { description } => {
            let agent = ShellGeniusAgent::new();
            let resp = agent.compose_pipeline(&description);
            if let Some(pipeline) = &resp.pipeline {
                print_header("Composed Pipeline", "");
                print_pipeline(pipeline);
                if let Some(expl) = &resp.explanation {
                    println!("  {}", expl);
                }
                for w in &resp.warnings { print_warning(w); }
                if !resp.alternatives.is_empty() {
                    print_header("Alternatives", "");
                    for a in &resp.alternatives { println!("  {} {}", dim("or:"), a); }
                }
            } else {
                print_warning("No matching idiom found. Try being more specific.");
            }
        }

        Commands::FixQuoting { command } => {
            let agent = ShellGeniusAgent::new();
            print_header("Quoting Analysis", &command);
            let resp = agent.fix_quoting(&command);
            println!();
            if resp.warnings.is_empty() {
                print_success("No obvious quoting issues found.");
            } else {
                for w in &resp.warnings { print_warning(w); }
            }
        }

        Commands::Translate { command, from, to } => {
            let agent = ShellGeniusAgent::new();
            print_header("Shell Translation", &format!("{} -> {}", from, to));
            let from_shell = parse_shell_arg(&from)?;
            let to_shell = parse_shell_arg(&to)?;
            let resp = agent.translate(&command, from_shell, to_shell);
            if let Some(expl) = &resp.explanation { println!("\n  {}", expl); }
            if resp.warnings.is_empty() {
                print_success("No compatibility issues detected.");
            } else {
                println!();
                for w in &resp.warnings { print_warning(w); }
            }
        }

        Commands::FdHelp { description } => {
            let agent = ShellGeniusAgent::new();
            print_header("File Descriptor Help", "");
            let resp = agent.fd_help(&description);
            if let Some(expl) = &resp.explanation { println!("\n{}", expl); }
        }

        Commands::FindTool { task } => {
            let agent = ShellGeniusAgent::new();
            print_header("Tool Recommendation", "");
            let resp = agent.find_best_tool(&task);
            if let Some(expl) = &resp.explanation { println!("\n{}", expl); }
        }

        Commands::Run { cmd, confirm } => {
            let agent = ShellGeniusAgent::new();
            print_header("Command", &cmd);
            let stages = explain_pipeline(&cmd);
            println!();
            for (i, s) in stages.iter().enumerate() {
                println!("  {} {}  {}", dim(&format!("{}.", i + 1)), bold(&s.tool), s.command.trim());
                println!("     {}", dim(&s.explanation));
            }
            let mode = if confirm { "executing" } else { "dry-run" };
            status(&format!("Mode: {}", mode));
            let resp = agent.run(&cmd, confirm);
            if let Some(r) = &resp.exec_result {
                print_exec_result(&r.command, r.exit_code, &r.stdout, &r.stderr, r.elapsed_ms, 20);
                if r.dry_run {
                    println!();
                    status_warn("Dry run — add --confirm to execute for real");
                }
            }
        }

        Commands::Tools => {
            print_banner();
            let mut agent = ShellGeniusAgent::new();
            let info = {
                let _s = Spinner::new("Detecting tools...");
                agent.setup()
            };
            print_env_info(&info);
            print_header("Available Tools", "");
            let mut rows = Vec::new();
            for (tool, path) in &agent.ctx.available_tools {
                rows.push(vec![tool.clone(), path.clone()]);
            }
            rows.sort();
            print_table(&["Tool", "Path"], &rows);
            println!();
            status_ok(&format!("{} tools detected", agent.ctx.available_tools.len()));
        }

        Commands::Ingest { path, output } => {
            print_header("Ingesting", &path);
            let quoted_path = shlex::try_quote(&path).unwrap_or(path.clone().into()).into_owned();
            let mut args = vec![quoted_path.as_str()];
            let quoted_out;
            if let Some(ref out) = output {
                quoted_out = shlex::try_quote(out).unwrap_or(out.clone().into()).into_owned();
                args.push(&quoted_out);
            }
            let result = run_python_ingest(&args, 600.0);
            if result.ok() {
                print!("{}", result.stdout);
                print_success("Ingestion complete");
            } else {
                print_error(&format!("Ingestion failed (exit {})", result.exit_code));
                if !result.stderr.is_empty() { eprintln!("{}", result.stderr); }
            }
        }

        Commands::IngestMan { pages, sections, shell, all } => {
            let mut args: Vec<String> = Vec::new();
            let label;

            if shell {
                args.push("--man-shell".into());
                label = "shell preset (~80 pages)";
            } else if all {
                args.push("--man-section".into());
                if sections.is_empty() {
                    args.extend(["1", "2", "3"].iter().map(|s| s.to_string()));
                } else {
                    args.extend(sections);
                }
                label = "all pages from sections";
            } else if !pages.is_empty() {
                args.push("--man".into());
                args.extend(pages);
                label = "specified pages";
            } else {
                args.push("--man".into());
                args.extend([
                    "bash", "grep", "sed", "awk", "find", "xargs", "sort", "uniq",
                    "pipe", "fork", "exec", "dup2", "socket", "signal",
                    "kill", "ps", "jq", "curl", "tmux",
                ].iter().map(|s| s.to_string()));
                label = "default set (~19 pages)";
            }

            print_header("Ingesting Man Pages", label);
            let arg_refs: Vec<&str> = args.iter().map(|s| s.as_str()).collect();
            let _spinner = Spinner::new("Rendering and embedding man pages...");
            let result = run_python_ingest(&arg_refs, 600.0);
            drop(_spinner);

            if result.ok() {
                print!("{}", result.stdout);
                print_success("Man page ingestion complete");
            } else {
                print_error(&format!("Ingestion failed (exit {})", result.exit_code));
                if !result.stderr.is_empty() { eprintln!("{}", result.stderr); }
            }
        }

        Commands::Indices => {
            print_header("Registered Knowledge Indices", "");
            // Read the registry file directly
            let registry_path = dirs_next::home_dir()
                .map(|h| h.join(".shellgenius/indices.json"))
                .unwrap_or_default();
            if let Ok(content) = std::fs::read_to_string(&registry_path) {
                if let Ok(registry) = serde_json::from_str::<serde_json::Value>(&content) {
                    if let Some(indices) = registry.get("indices").and_then(|v| v.as_array()) {
                        if indices.is_empty() {
                            status("No indices registered. Use 'shellgenius ingest <path>' to add one.");
                        }
                        for idx in indices {
                            let source = idx.get("source").and_then(|v| v.as_str()).unwrap_or("?");
                            let chunks = idx.get("chunks").and_then(|v| v.as_u64()).unwrap_or(0);
                            let path = idx.get("path").and_then(|v| v.as_str()).unwrap_or("?");
                            // Check if index file exists
                            let index_file = std::path::Path::new(path).join("index.faiss");
                            if index_file.exists() {
                                print_success(source);
                            } else {
                                print_warning(&format!("{} (missing)", source));
                            }
                            println!("      {} {}", dim("Chunks:"), chunks);
                            println!("      {} {}", dim("Path:"), path);
                        }
                    }
                }
            } else {
                status("No indices registered. Use 'shellgenius ingest <path>' or 'shellgenius ingest-man' to add one.");
            }
        }

        Commands::Chat { url, model, key, ask } => {
            // Run chat on a blocking thread to avoid reqwest::blocking vs tokio conflict
            let handle = tokio::task::spawn_blocking(move || {
                let config = llm::LLMConfig::new(&url, &key, &model);
                let mut agent = ShellGeniusAgent::new();
                agent.setup();
                let mut llm_agent = llm::ShellGeniusLLM::new(config, agent);

                if let Some(question) = ask {
                    match llm_agent.chat(&question) {
                        Ok(response) => println!("{}", response),
                        Err(e) => status_error(&format!("Error: {}", e)),
                    }
                } else {
                    chat::interactive_chat(&mut llm_agent);
                }
            });
            handle.await?;
        }

        Commands::Serve { host, port } => {
            let mut agent = ShellGeniusAgent::new();
            agent.setup();
            server::serve(agent, &host, port).await?;
        }
    }

    Ok(())
}

/// Find the Python shellgenius package root for PYTHONPATH.
fn find_python_root() -> Option<String> {
    // Check relative to this binary's location
    let exe = std::env::current_exe().ok()?;
    // Try: ../../../ (from target/debug/ or target/release/)
    for ancestor in exe.ancestors().skip(1).take(5) {
        let candidate = ancestor.join("shellgenius").join("__init__.py");
        if candidate.exists() {
            return Some(ancestor.to_string_lossy().into_owned());
        }
    }
    // Try the workspace_agent directory specifically
    let workspace = std::path::PathBuf::from("/home/cgould/Workspace/workspace_agent");
    if workspace.join("shellgenius/__init__.py").exists() {
        return Some(workspace.to_string_lossy().into_owned());
    }
    // Try current dir
    let cwd = std::env::current_dir().ok()?;
    if cwd.join("shellgenius/__init__.py").exists() {
        return Some(cwd.to_string_lossy().into_owned());
    }
    // Try parent of current dir
    let parent = cwd.parent()?;
    if parent.join("shellgenius/__init__.py").exists() {
        return Some(parent.to_string_lossy().into_owned());
    }
    None
}

/// Run a Python shellgenius module command with proper PYTHONPATH and venv.
fn run_python_ingest(args: &[&str], timeout_secs: f64) -> sg_core::types::ExecResult {
    let python_root = find_python_root();

    // Find the Python interpreter — prefer the venv alongside the shellgenius package
    let python = if let Some(ref root) = python_root {
        let venv_python = std::path::PathBuf::from(root).join(".venv/bin/python");
        if venv_python.exists() {
            venv_python.to_string_lossy().into_owned()
        } else {
            "python3".into()
        }
    } else {
        "python3".into()
    };

    let pythonpath_prefix = if let Some(ref root) = python_root {
        format!("PYTHONPATH={} ", shlex::try_quote(root).unwrap_or(root.clone().into()))
    } else {
        String::new()
    };

    let cmd = format!(
        "{}{} -m shellgenius.knowledge.ingest {}",
        pythonpath_prefix,
        shlex::try_quote(&python).unwrap_or(python.clone().into()),
        args.join(" "),
    );
    sg_core::exec::execute(&cmd, None, timeout_secs, 10_485_760, sg_core::exec::ExecMode::Execute, "/bin/bash")
}

fn parse_shell_arg(s: &str) -> Result<Shell> {
    match s.to_lowercase().as_str() {
        "bash" => Ok(Shell::Bash),
        "zsh" => Ok(Shell::Zsh),
        "fish" => Ok(Shell::Fish),
        "posix" | "sh" => Ok(Shell::Posix),
        "dash" => Ok(Shell::Dash),
        "ksh" => Ok(Shell::Ksh),
        _ => anyhow::bail!("Unknown shell: {}", s),
    }
}
