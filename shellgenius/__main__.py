"""
ShellGenius CLI — direct command-line interface.

Usage:
    python -m shellgenius explain "find . -name '*.py' | xargs grep TODO | sort"
    python -m shellgenius compose "count errors per log file, top 10"
    python -m shellgenius fix-quoting "echo $HOME isn't safe"
    python -m shellgenius chat                        # interactive LLM chat
    python -m shellgenius chat --ask "how do pipes work?"
    python -m shellgenius serve                       # start OpenClaw skill server
    python -m shellgenius tools                       # list available shell tools
"""

from __future__ import annotations

import argparse
import json
import sys

from shellgenius.agent import ShellGeniusAgent, AgentContext
from shellgenius.knowledge.corpus import Shell
from shellgenius.ui import (
    print_banner, print_header, print_pipeline, print_explanation,
    print_warning, print_error, print_success, print_alternative,
    print_kv, print_table, print_exec_result, print_env_info,
    status, status_ok, status_warn, status_error,
    bold, dim, green, yellow, red, cyan, Spinner,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="shellgenius",
        description="ShellGenius — expert shell agent for pipes, containers, and dispatch",
    )
    sub = parser.add_subparsers(dest="command")

    # explain
    p_explain = sub.add_parser("explain", help="Explain a shell pipeline stage-by-stage")
    p_explain.add_argument("pipeline", help="The pipeline to explain")

    # compose
    p_compose = sub.add_parser("compose", help="Compose a pipeline from description")
    p_compose.add_argument("description", help="Natural-language description of what you want")

    # fix-quoting
    p_fix = sub.add_parser("fix-quoting", help="Analyze and fix quoting issues")
    p_fix.add_argument("cmd_to_fix", help="The command to fix")

    # translate
    p_trans = sub.add_parser("translate", help="Translate between shell dialects")
    p_trans.add_argument("command", help="The command to translate")
    p_trans.add_argument("--from", dest="from_shell", default="bash")
    p_trans.add_argument("--to", dest="to_shell", default="posix")

    # fd-help
    p_fd = sub.add_parser("fd-help", help="Get help with file descriptor operations")
    p_fd.add_argument("description", help="What fd operation you need")

    # find-tool
    p_tool = sub.add_parser("find-tool", help="Find the best tool for a task")
    p_tool.add_argument("task", help="What you need to accomplish")

    # run
    p_run = sub.add_parser("run", help="Run a command with safety checks")
    p_run.add_argument("cmd", help="The command to run")
    p_run.add_argument("--confirm", action="store_true", help="Actually execute (default: dry run)")

    # tools
    sub.add_parser("tools", help="List available shell tools on this system")

    # chat
    p_chat = sub.add_parser("chat", help="Interactive LLM chat with ShellGenius tools")
    p_chat.add_argument("--provider", choices=["anthropic", "openai", "local"], default="",
                        help="LLM provider: anthropic, openai, or local (auto-detected)")
    p_chat.add_argument("--url", default="http://localhost:8082", help="API base URL")
    p_chat.add_argument("--model", default="", help="Model name (auto per provider if omitted)")
    p_chat.add_argument("--key", default="", help="API key (or set ANTHROPIC_API_KEY / OPENAI_API_KEY env var)")
    p_chat.add_argument("--ask", help="Single question (non-interactive)")

    # ingest
    p_ingest = sub.add_parser("ingest", help="Ingest files/directories into vector knowledge base")
    p_ingest.add_argument("path", help="File or directory to ingest")
    p_ingest.add_argument("--output", help="Custom output directory (overrides default /usr/share/embeddings/)")

    # ingest-man
    p_man = sub.add_parser("ingest-man", help="Ingest man pages into vector knowledge base")
    p_man.add_argument("pages", nargs="*", help="Specific pages (e.g., bash grep pipe fork). Omit for --shell preset.")
    p_man.add_argument("--section", "-s", action="append", help="Man section(s) to ingest (e.g., -s 1 -s 2)")
    p_man.add_argument("--shell", action="store_true", help="Ingest curated shell-relevant man pages (~80 pages)")
    p_man.add_argument("--all", action="store_true", help="Ingest ALL pages from specified sections")

    # indices
    sub.add_parser("indices", help="List all registered vector knowledge indices")

    # serve
    p_serve = sub.add_parser("serve", help="Start the OpenClaw skill server")
    p_serve.add_argument("--port", type=int, default=9747)
    p_serve.add_argument("--host", default="127.0.0.1")

    # manifest
    p_manifest = sub.add_parser("manifest", help="Generate OpenClaw skill manifest")
    p_manifest.add_argument("--output", default=".")

    args = parser.parse_args()

    if not args.command:
        print_banner()
        parser.print_help()
        return 0

    # --- Chat mode (LLM-powered) ---
    if args.command == "chat":
        from shellgenius.engine.llm_bridge import ShellGeniusLLM, interactive_chat, _resolve_config
        config = _resolve_config(args)
        if args.ask:
            llm = ShellGeniusLLM(config=config)
            llm.agent.setup()
            print(llm.chat(args.ask))
        else:
            interactive_chat(config)
        return 0

    # --- Static tools (no LLM needed) ---

    agent = ShellGeniusAgent()

    if args.command == "explain":
        from shellgenius.engine.pipe_algebra import explain_pipeline
        print_header("Pipeline Breakdown", args.pipeline)

        stages = explain_pipeline(args.pipeline)
        print()
        for i, s in enumerate(stages):
            num = dim(f"{i+1}.")
            tool_name = bold(s["tool"])
            cmd = s["command"].strip()
            expl = dim(s["explanation"])
            print(f"  {num} {tool_name}  {cmd}")
            print(f"     {expl}")

        resp = agent.explain(args.pipeline)
        if resp.warnings:
            print()
            for w in resp.warnings:
                print_warning(w)

    elif args.command == "compose":
        resp = agent.compose_pipeline(args.description)
        if resp.pipeline:
            print_header("Composed Pipeline")
            print_pipeline(resp.pipeline)
            print(f"  {resp.explanation}")
            if resp.warnings:
                print()
                for w in resp.warnings:
                    print_warning(w)
            if resp.alternatives:
                print_header("Alternatives")
                for a in resp.alternatives:
                    print(f"  {dim('or:')} {a}")
        else:
            print_warning("No matching idiom found. Try being more specific.")

    elif args.command == "fix-quoting":
        print_header("Quoting Analysis", args.cmd_to_fix)
        resp = agent.fix_quoting(args.cmd_to_fix)
        print()
        if resp.warnings:
            for w in resp.warnings:
                print_warning(w)
        else:
            print_success("No obvious quoting issues found.")
        if resp.alternatives:
            print()
            for a in resp.alternatives:
                print(f"  {dim(a)}")

    elif args.command == "translate":
        print_header("Shell Translation", f"{args.from_shell} -> {args.to_shell}")
        resp = agent.translate(
            args.command,
            Shell[args.from_shell.upper()],
            Shell[args.to_shell.upper()],
        )
        print(f"\n  {resp.explanation}")
        if resp.warnings:
            print()
            for w in resp.warnings:
                print_warning(w)
        else:
            print_success("No compatibility issues detected.")

    elif args.command == "fd-help":
        print_header("File Descriptor Help")
        resp = agent.fd_help(args.description)
        print(f"\n{resp.explanation}")

    elif args.command == "find-tool":
        print_header("Tool Recommendation")
        resp = agent.find_best_tool(args.task)
        print(f"\n{resp.explanation}")

    elif args.command == "run":
        # Explain first
        print_header("Command", args.cmd)
        resp_explain = agent.explain(args.cmd)
        from shellgenius.engine.pipe_algebra import explain_pipeline
        stages = explain_pipeline(args.cmd)
        print()
        for i, s in enumerate(stages):
            num = dim(f"{i+1}.")
            tool_name = bold(s["tool"])
            expl = dim(s["explanation"])
            print(f"  {num} {tool_name}  {s['command'].strip()}")
            print(f"     {expl}")

        if resp_explain.warnings:
            print()
            for w in resp_explain.warnings:
                print_warning(w)

        # Execute
        mode = "executing" if args.confirm else "dry-run"
        status(f"Mode: {mode}")
        resp = agent.run(args.cmd, confirm=args.confirm)
        if resp.exec_result:
            r = resp.exec_result
            print_exec_result(r.command, r.exit_code, r.stdout, r.stderr, r.elapsed_ms)
            if r.dry_run:
                print()
                status_warn("Dry run — add --confirm to execute for real")

    elif args.command == "tools":
        print_banner()
        with Spinner("Detecting tools..."):
            info = agent.setup()
        print_env_info(info)

        print_header("Available Tools")
        rows = []
        for tool, path in sorted(agent.ctx.available_tools.items()):
            rows.append([tool, path])
        print_table(["Tool", "Path"], rows)
        print()
        status_ok(f"{info['tools_available']} tools detected")

    elif args.command == "ingest":
        from shellgenius.knowledge.ingest import ingest as do_ingest
        print_header("Ingesting", args.path)
        try:
            manifest = do_ingest(args.path, output_dir=args.output)
            print()
            print_success(f"Ingested {manifest['files_ingested']} files, {manifest['total_chunks']} chunks")
            status(f"Index saved to {manifest.get('embed_dir', 'unknown')}")
            status(f"Index registered in ~/.shellgenius/indices.json")
        except (FileNotFoundError, ValueError) as e:
            print_error(str(e))

    elif args.command == "ingest-man":
        from shellgenius.knowledge.ingest import ingest_manpages
        try:
            if args.shell:
                # Curated shell-relevant set
                print_header("Ingesting Shell Man Pages", "~80 curated pages")
                shell_pages = [
                    "bash", "zsh", "dash", "sh",
                    "grep", "sed", "awk", "cut", "tr", "sort", "uniq", "wc", "head", "tail",
                    "tee", "paste", "join", "comm", "column", "fmt", "fold", "nl", "rev", "tac",
                    "find", "xargs", "ls", "cp", "mv", "rm", "mkdir", "chmod", "chown", "ln", "stat",
                    "file", "diff", "patch", "tar", "gzip",
                    "kill", "ps", "top", "nice", "nohup", "timeout", "wait", "jobs",
                    "mkfifo", "flock",
                    "pipe", "fork", "exec", "execve", "dup", "dup2", "fcntl", "open", "close",
                    "read", "write", "socket", "bind", "listen", "accept", "connect",
                    "select", "poll", "epoll_create", "signal", "sigaction", "mmap",
                    "curl", "wget", "ssh", "nc", "socat", "jq", "parallel", "tmux",
                    "podman", "toolbox", "systemctl", "journalctl",
                ]
                manifest = ingest_manpages(pages=shell_pages, sections=["1", "2", "3", "7", "8"])
            elif args.all:
                sections = args.section or ["1", "2", "3"]
                print_header("Ingesting All Man Pages", f"sections: {', '.join(sections)}")
                manifest = ingest_manpages(sections=sections)
            elif args.pages:
                print_header("Ingesting Man Pages", " ".join(args.pages))
                sections = args.section or ["1", "2", "3", "7", "8"]
                manifest = ingest_manpages(pages=args.pages, sections=sections)
            else:
                # No args — default to --shell
                print_header("Ingesting Shell Man Pages", "~80 curated pages (use --help for options)")
                shell_pages = [
                    "bash", "grep", "sed", "awk", "find", "xargs", "sort", "uniq",
                    "pipe", "fork", "exec", "dup2", "socket", "signal",
                    "kill", "ps", "jq", "curl", "tmux",
                ]
                manifest = ingest_manpages(pages=shell_pages, sections=["1", "2", "3"])
            print()
            print_success(f"Ingested {manifest['files_ingested']} man pages, {manifest['total_chunks']} chunks")
        except (FileNotFoundError, ValueError) as e:
            print_error(str(e))

    elif args.command == "indices":
        from shellgenius.knowledge.ingest import list_indices
        print_header("Registered Knowledge Indices")
        indices = list_indices()
        if not indices:
            status("No indices registered. Use 'shellgenius ingest <path>' to add one.")
        else:
            for idx in indices:
                ok = idx.get("status") == "ok"
                if ok:
                    print_success(f"{idx['source']}")
                else:
                    print_warning(f"{idx['source']} (missing)")
                print_kv("Chunks", str(idx["chunks"]), indent=6)
                print_kv("Path", idx["path"], indent=6)

    elif args.command == "serve":
        from shellgenius.openclaw.server import serve
        print_banner()
        serve(port=args.port, host=args.host)

    elif args.command == "manifest":
        from shellgenius.openclaw.skill import write_manifest
        with Spinner("Generating manifest..."):
            path = write_manifest(args.output)
        print_success(f"Manifest written to: {path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
