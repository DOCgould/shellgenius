# ShellGenius

Expert shell agent for pipe composition, container sandboxing, MIME dispatch, and ScaNN-powered knowledge retrieval. Runs as a CLI tool and an OpenClaw skill server, connecting to local LLMs via the Anthropic API.

## Architecture

```
shellgenius/          Python source (~7,400 lines)
shellgenius-rs/       Rust port (~3,800 lines, 7.4MB static binary)
tests/                56 Python tests
```

### 21 Tools

| Category | Tools |
|----------|-------|
| **Shell** (7) | compose, explain, fix-quoting, translate, fd-help, find-tool, run |
| **Container** (7) | create, exec, sandbox-run, state, lifecycle, podman-raw, toolbox-raw |
| **Dispatch** (3) | route, mime-query, introspect |
| **Knowledge** (4) | query, search-all, ingest, list-indices |

### Key Features

- **Pipe Algebra**: Type-checked pipeline composition (TEXT, JSON, TSV, NULL_DELIM streams)
- **Container Sandboxing**: 5 profiles from NONE to LOCKED (read-only fs, no network, cap-drop ALL)
- **MIME Dispatch**: Bridge pipes to desktop apps via xdg-open, clipboard, DBus notifications
- **ScaNN Knowledge**: Adaptive vector search (brute-force for small corpora, tree+AH for million-scale codebases like the Linux kernel) over TLPI (1,322 chunks) and ingested man pages
- **Context Tracking**: Probes llama.cpp `/slots` for real `n_ctx`, shows usage in prompt
- **Thinking Mode**: Detects `enable_thinking` in chat template, toggleable via `/think`

## Quick Start (Python)

```bash
# Requires Linux x86_64 and Python 3.11 or 3.12 (ScaNN constraints).
python -m venv .venv && source .venv/bin/activate
pip install scann pymupdf sentence-transformers

# Explain a pipeline
python -m shellgenius explain "find . -name '*.log' -print0 | xargs -0 grep -l ERROR | sort"

# Detect available tools
python -m shellgenius tools

# Ingest man pages
python -m shellgenius ingest-man --shell

# Interactive chat (requires local LLM on localhost:8082)
python -m shellgenius chat
```

## Quick Start (Rust)

```bash
cd shellgenius-rs

# Development build
cargo run -- explain "grep ERROR | sort | uniq -c | head -20"
cargo run -- tools
cargo run -- chat --ask "how do named pipes work?"

# Static release build (zero runtime deps)
CC=gcc cargo build --release --target aarch64-unknown-linux-musl
# => 7.4MB static binary at target/aarch64-unknown-linux-musl/release/shellgenius
```

## Man Page Ingestion

```bash
# Specific pages
shellgenius ingest-man bash grep pipe fork dup2 socket

# Curated shell set (~93 pages: commands + syscalls + IPC)
shellgenius ingest-man --shell

# All syscall man pages (section 2)
shellgenius ingest-man --all -s 2

# List what's been ingested
shellgenius indices
```

## LLM Integration

Connects to any Anthropic-compatible API (local proxy to llama.cpp/vLLM):

- **21 tools** registered with the LLM via tool-use protocol
- **System prompt** (~1,500 words) with USE WHEN triggers for each tool
- **Context window** probed from live backend (`/health` -> `/slots` -> `n_ctx`)
- **Thinking mode** detected from chat template, toggled with `/think` and `/nothink`
- **Watchdog UI** shows tool calls, vector queries, and token usage on stderr

## License

APACHE 2.0
