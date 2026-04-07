"""
Codex ↔ Shell Equivalence Matrix

Maps every OpenAI Codex CLI tool to its native bash/unix equivalent,
plus how toolbox/podman elevate the shell equivalents with container-backed
isolation, state management, and sandboxing.

Key insight: 20/29 tools have FULL native shell equivalents.
With toolbox/podman, the PARTIAL count drops from 7 to 2.
The unix process model IS an agent orchestration system — it predates
the "agent" terminology by 50 years. Containers finish the job.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto


class Equivalence(Enum):
    FULL = auto()       # shell can do this natively, no gaps
    PARTIAL = auto()    # shell covers most of it, some gaps
    NONE = auto()       # no native shell equivalent, needs external service


@dataclass(frozen=True)
class ToolMapping:
    codex_tool: str
    codex_description: str
    bash_native: str              # simplest bash way
    advanced_shell: str           # deeper unix technique
    modern_cli: str               # modern alternative tools
    equivalence: Equivalence
    key_insight: str              # the "aha" for shell users
    # Container enhancement fields
    container_tool: str = ""      # toolbox/podman equivalent
    container_upgrade: str = ""   # what containers add beyond bare shell
    equivalence_with_containers: Equivalence = Equivalence.FULL  # re-rated with containers


MATRIX: list[ToolMapping] = [

    # =========================================================================
    # SHELL EXECUTION (4 tools)
    # =========================================================================

    ToolMapping(
        codex_tool="shell / shell_command",
        codex_description="Execute a command, capture stdout/stderr/exit code",
        bash_native="cmd  |  $(cmd)  |  bash -c 'cmd'",
        advanced_shell="exec (replace process), coproc (bidirectional), subshell ( cmd )",
        modern_cli="timeout, script (PTY alloc), env -i (clean env)",
        equivalence=Equivalence.FULL,
        key_insight="The shell IS the command executor. Codex just wraps what bash already does.",
        container_tool="toolbox run -c NAME cmd  |  podman exec NAME cmd",
        container_upgrade="Container gives you: isolated PID/net/mount namespace, sandbox_permissions via profiles, reproducible env.",
    ),
    ToolMapping(
        codex_tool="exec_command",
        codex_description="Run in PTY, return session ID for ongoing interaction",
        bash_native="coproc NAME { cmd; }  — bidirectional pipe with PID",
        advanced_shell="script -qc 'cmd' /dev/null (force PTY), expect (PTY automation)",
        modern_cli="tmux new-window, zellij",
        equivalence=Equivalence.FULL,
        key_insight="coproc gives you a persistent session with stdin/stdout FDs and a PID. That IS a session ID.",
    ),
    ToolMapping(
        codex_tool="write_stdin",
        codex_description="Write to a running process's stdin, poll for output",
        bash_native="echo data >&\"${COPROC[1]}\"  |  echo > /tmp/fifo",
        advanced_shell="mkfifo (named pipe), /proc/$PID/fd/0 (direct fd write), coproc FDs",
        modern_cli="tmux send-keys, expect, socat",
        equivalence=Equivalence.FULL,
        key_insight="coproc[1] IS write_stdin. coproc[0] IS read_stdout. Named pipes decouple the lifetime.",
    ),

    # =========================================================================
    # FILE OPERATIONS (3 tools)
    # =========================================================================

    ToolMapping(
        codex_tool="apply_patch",
        codex_description="Apply structured file edits (add/delete/update hunks)",
        bash_native="patch -p1 < diff.patch",
        advanced_shell="ed (scriptable line editor), diff -e (outputs ed scripts), sed -i",
        modern_cli="delta (viewer), sd (sed alt), comby (structural), ast-grep",
        equivalence=Equivalence.FULL,
        key_insight="patch(1) is the ORIGINAL implementation of this concept. ed is Turing-complete for text transforms.",
    ),
    ToolMapping(
        codex_tool="apply_patch (freeform)",
        codex_description="Custom Lark grammar for file edits (not standard unified diff)",
        bash_native="printf '/pattern/c\\nnew line\\n.\\nw\\n' | ed -s file",
        advanced_shell="ed scripts, awk structural transforms, perl -pi -e, m4 macros",
        modern_cli="comby (structural search/replace), ast-grep, fastmod",
        equivalence=Equivalence.PARTIAL,
        key_insight="ed can do anything the freeform grammar can, but translating the grammar requires a parser.",
        container_tool="toolbox run -c dev comby 'PATTERN' 'REPLACEMENT' -i .py",
        container_upgrade="Install comby/ast-grep in a toolbox without polluting host. Reproducible tool environment.",
        equivalence_with_containers=Equivalence.FULL,
    ),
    ToolMapping(
        codex_tool="list_dir",
        codex_description="List directory with offset, limit, and depth control",
        bash_native="find . -maxdepth N | sort | tail -n +OFFSET | head -n LIMIT",
        advanced_shell="find -printf for custom format, shopt -s globstar; printf '%s\\n' **/*",
        modern_cli="fd --max-depth N, eza --tree --level=N, broot",
        equivalence=Equivalence.FULL,
        key_insight="find + head/tail gives you pagination. -maxdepth gives you depth. Done.",
    ),

    # =========================================================================
    # AGENT / SUB-AGENT SYSTEM (9 tools) — the big one
    # =========================================================================

    ToolMapping(
        codex_tool="spawn_agent",
        codex_description="Launch an independent sub-task with its own context",
        bash_native="cmd &  then  PID=$!",
        advanced_shell="coproc AGENT { cmd; } (named, with I/O pipes), setsid (new session), process groups",
        modern_cli="GNU parallel, xargs -P, tmux new-window",
        equivalence=Equivalence.FULL,
        key_insight="& is spawn_agent. $! is agent_id. Process groups are agent trees. This is the original.",
        container_tool="podman run -d --name agent-1 IMAGE cmd  |  toolbox create agent-1 && toolbox run -c agent-1 cmd &",
        container_upgrade="Each agent gets full PID/net/mount isolation. No process tree leakage. Clean kill via podman stop. State survives shell exit.",
    ),
    ToolMapping(
        codex_tool="send_input / send_message",
        codex_description="Send data/message to a running agent",
        bash_native="echo msg >&\"${AGENT[1]}\"  |  kill -SIGUSR1 $PID",
        advanced_shell="mkfifo /tmp/agent_inbox (named pipe protocol), /proc/$PID/fd/0, unix domain sockets",
        modern_cli="tmux send-keys, socat UNIX-CONNECT, dbus-send",
        equivalence=Equivalence.FULL,
        key_insight="Coproc FDs = typed message channel. Signals = interrupt. Named pipes = durable mailbox.",
    ),
    ToolMapping(
        codex_tool="followup_task",
        codex_description="Message an agent AND trigger a new turn",
        bash_native="echo msg >&\"${AGENT[1]}\" && kill -SIGUSR1 $PID",
        advanced_shell="Write to named pipe + send signal (data + interrupt combo)",
        modern_cli="expect (send + wait for prompt)",
        equivalence=Equivalence.FULL,
        key_insight="send_message + signal is followup_task. The signal is the 'trigger a turn' part.",
    ),
    ToolMapping(
        codex_tool="wait_agent",
        codex_description="Wait for agent(s) to reach final status",
        bash_native="wait $PID  |  wait -n (any child, bash 4.3+)  |  wait -n -p VAR (which one, bash 5.1+)",
        advanced_shell="trap 'handler' CHLD (async notification), pidfd_open + poll (race-free, Linux 5.3+)",
        modern_cli="GNU parallel --wait",
        equivalence=Equivalence.FULL,
        key_insight="wait -n -p VAR (bash 5.1) is the exact equivalent: wait for ANY agent, get back WHICH one finished.",
    ),
    ToolMapping(
        codex_tool="close_agent",
        codex_description="Kill an agent and its entire subtree",
        bash_native="kill -- -$PGID  (kill entire process group)",
        advanced_shell="setsid at spawn → clean tree kill. pkill -P (children). Cgroup kill via systemd-run --scope",
        modern_cli="timeout --signal=TERM 30s cmd",
        equivalence=Equivalence.FULL,
        key_insight="setsid at spawn time + kill -PGID = clean tree kill. The key is setsid at SPAWN, not at kill time.",
        container_tool="podman stop NAME  |  podman kill NAME  |  podman rm -f NAME",
        container_upgrade="Container stop kills ENTIRE process tree guaranteed. No orphans. No leaked processes. Clean.",
    ),
    ToolMapping(
        codex_tool="resume_agent",
        codex_description="Resume a previously stopped agent",
        bash_native="kill -CONT $PID  |  fg %job  |  bg %job",
        advanced_shell="SIGSTOP/SIGCONT pair. Cgroup freezer (freeze/thaw entire tree). ptrace(PTRACE_CONT)",
        modern_cli="reptyr (reattach to different terminal)",
        equivalence=Equivalence.FULL,
        key_insight="SIGSTOP/SIGCONT is the kernel primitive. It predates every agent framework by decades.",
        container_tool="podman unpause NAME  |  podman start NAME",
        container_upgrade="podman pause/unpause freezes the ENTIRE cgroup. podman start resumes an exited container with all state.",
    ),
    ToolMapping(
        codex_tool="list_agents",
        codex_description="List running agents in the thread tree",
        bash_native="jobs -l  (with PIDs and status)",
        advanced_shell="ps --ppid $$ -o pid,stat,cmd  |  pstree -p $$  |  /proc/$$/task/*/children",
        modern_cli="procs --tree, htop -p",
        equivalence=Equivalence.FULL,
        key_insight="jobs is list_agents. ps --ppid $$ is the programmatic version. /proc is the kernel truth.",
        container_tool="podman ps --format json  |  toolbox list",
        container_upgrade="Structured JSON output with state, image, ports, mounts. Better than parsing ps output.",
    ),
    ToolMapping(
        codex_tool="spawn_agents_on_csv",
        codex_description="Fan out: one worker per CSV row, with concurrency control",
        bash_native="while IFS=, read -r c1 c2; do process \"$c1\" \"$c2\" & done < data.csv",
        advanced_shell="GNU parallel --csv -a data.csv 'cmd {1} {2}'  |  xargs -P N with job pool + wait -n",
        modern_cli="GNU parallel --csv, miller (mlr), xsv + parallel, duckdb piped to parallel",
        equivalence=Equivalence.FULL,
        key_insight="GNU parallel --csv is PURPOSE-BUILT for this exact pattern. It even handles CSV quoting correctly.",
    ),
    ToolMapping(
        codex_tool="report_agent_job_result",
        codex_description="Worker reports result back to coordinator",
        bash_native="exit $status (exit code) | result=$(cmd) (stdout capture)",
        advanced_shell="Named pipe: echo result > /tmp/results_fifo. Atomic append (< PIPE_BUF). /dev/shm for shared mem",
        modern_cli="GNU parallel --results dir/ (auto-collects per-job stdout/stderr/exitcode)",
        equivalence=Equivalence.FULL,
        key_insight="Exit code IS the simplest result. stdout IS the data channel. parallel --results is the structured version.",
    ),

    # =========================================================================
    # PLANNING & INTERACTION (3 tools)
    # =========================================================================

    ToolMapping(
        codex_tool="update_plan",
        codex_description="Structured task plan with step + status tracking",
        bash_native="declare -A PLAN; PLAN[step1]='done'  (associative array)",
        advanced_shell="jq '.tasks[0].status = \"done\"' plan.json | sponge plan.json  +  flock for concurrency",
        modern_cli="jq, taskwarrior, todo.txt-cli, sqlite3",
        equivalence=Equivalence.PARTIAL,
        key_insight="Shell can track state but lacks structured task management. JSON + jq + flock gets 80% there.",
        container_tool="podman ps --format '{{.Names}} {{.State}}'  — containers ARE the plan state",
        container_upgrade="Each task IS a container. Its state (created/running/exited) IS the plan status. podman inspect gives exit code, duration, logs. No separate state file needed.",
        equivalence_with_containers=Equivalence.FULL,
    ),
    ToolMapping(
        codex_tool="request_user_input",
        codex_description="Prompt user with multiple-choice questions",
        bash_native="select opt in opt1 opt2 opt3; do ...; done  |  read -p 'Prompt: ' var",
        advanced_shell="exec 3</dev/tty (read from terminal even when stdin is piped). dialog/whiptail for TUI",
        modern_cli="gum choose, gum input, fzf, dialog, whiptail",
        equivalence=Equivalence.FULL,
        key_insight="select is the built-in menu. /dev/tty lets you prompt even inside a pipeline. gum adds polish.",
    ),
    ToolMapping(
        codex_tool="request_permissions",
        codex_description="Ask for filesystem/network access grants",
        bash_native="read -p 'Allow access to /path? [y/n] ' — manual consent prompt",
        advanced_shell="setfacl (POSIX ACLs), capabilities(7), landlock (Linux 5.13+), seccomp, bubblewrap",
        modern_cli="firejail, bubblewrap (bwrap), podman --cap-drop=ALL",
        equivalence=Equivalence.PARTIAL,
        key_insight="Unix has powerful permission primitives but they're OS-level, not application-level consent. The gap is the UX.",
        container_tool="podman run --network=none --read-only --cap-drop=ALL -v /path:/path:ro IMAGE cmd",
        container_upgrade="Sandbox profiles ARE permission grants. LOCKED = deny all. RESTRICTED = read-only + no network. WORKSPACE = rw + network. The container boundary enforces the permission — no trust required.",
        equivalence_with_containers=Equivalence.FULL,
    ),

    # =========================================================================
    # CODE & REPL (4 tools)
    # =========================================================================

    ToolMapping(
        codex_tool="js_repl",
        codex_description="Persistent JavaScript REPL with state between evaluations",
        bash_native="coproc NODE { node -i 2>&1; }  — persistent REPL with FD access",
        advanced_shell="mkfifo pair + node -i (named pipe REPL). expect (PTY-based REPL driving). script -qc 'node -i'",
        modern_cli="expect, tmux send-keys + capture-pane, socat EXEC:'node -i',pty",
        equivalence=Equivalence.FULL,
        key_insight="coproc IS a persistent REPL session. echo 'code' >&${NODE[1]} sends code. read <&${NODE[0]} gets result.",
    ),
    ToolMapping(
        codex_tool="js_repl_reset",
        codex_description="Restart the REPL kernel, clear state",
        bash_native="kill $NODE_PID; coproc NODE { node -i 2>&1; }  — kill and respawn",
        advanced_shell="Send '.exit\\n' to coproc, wait, respawn. Or kill -TERM + trap + respawn",
        modern_cli="tmux respawn-pane",
        equivalence=Equivalence.FULL,
        key_insight="Kill + respawn. The unix way: processes are cheap, just make a new one.",
    ),
    ToolMapping(
        codex_tool="exec (code mode)",
        codex_description="Execute code with nested tool access",
        bash_native="eval \"$code\"  |  source /dev/stdin <<< \"$code\"",
        advanced_shell="bash -c with exported functions. source <(cmd) for dynamic code loading",
        modern_cli="tmux, script",
        equivalence=Equivalence.FULL,
        key_insight="eval/source is code mode. Exported functions (export -f) give nested tool access.",
    ),
    ToolMapping(
        codex_tool="wait (code mode)",
        codex_description="Poll a yielded exec cell for new output",
        bash_native="read -t 0.1 line <&${COPROC[0]}  (non-blocking read with timeout)",
        advanced_shell="select/poll on fd: read -t TIMEOUT. Or: while IFS= read -r -t $YIELD line <&$FD; do ...; done",
        modern_cli="expect (expect/timeout pattern)",
        equivalence=Equivalence.FULL,
        key_insight="read -t TIMEOUT on a coproc FD is exactly 'poll for output with yield_time_ms'.",
    ),

    # =========================================================================
    # MEDIA & SEARCH (4 tools)
    # =========================================================================

    ToolMapping(
        codex_tool="view_image",
        codex_description="Display an image from the filesystem",
        bash_native="xdg-open image.png  — MIME-routed dispatch to desktop app (eog, Shotwell, Evince, etc.)",
        advanced_shell=(
            "xdg-open is a POSIX shell script that routes ANY file/URL to its registered handler via MIME types. "
            "xdg-mime query default image/png → org.gnome.Shotwell-Viewer.desktop. "
            "Also: gio open, Kitty icat, Sixel img2sixel, chafa (terminal braille art)"
        ),
        modern_cli="chafa, viu, catimg, timg, wezterm imgcat, feh, sxiv",
        equivalence=Equivalence.FULL,
        key_insight=(
            "xdg-open IS view_image — and view_pdf, view_html, play_audio, play_video. "
            "It's a universal MIME-routed dispatcher that's itself a shell script. "
            "On this system: PNG→Shotwell, PDF→Evince, HTML→Firefox, video→Totem, code→vim. "
            "142 .desktop entries registered, all accessible from shell."
        ),
        container_tool="toolbox run -c gui xdg-open image.png  (Wayland/X11 sockets auto-mounted by toolbox)",
        container_upgrade=(
            "Toolbox auto-mounts Wayland/X11 sockets and DBus, so xdg-open works inside toolbox containers — "
            "GUI apps launch on the host display. Podman needs explicit socket forwarding."
        ),
    ),
    ToolMapping(
        codex_tool="web_search",
        codex_description="Search the web, return results",
        bash_native="— (no native equivalent)",
        advanced_shell="curl + HTML parsing: curl -s 'https://html.duckduckgo.com/html/?q=...' | htmlq/pup",
        modern_cli="ddgr (DuckDuckGo), googler, surfraw, s (search CLI)",
        equivalence=Equivalence.NONE,
        key_insight="The shell has no concept of 'search the web'. curl + parsing is a workaround, not an equivalent.",
        container_tool="toolbox run -c search ddgr -n 5 'query'  (install ddgr in a search toolbox)",
        container_upgrade="Toolbox lets you install ddgr/googler/surfraw without host pollution. Network-capable by default.",
        equivalence_with_containers=Equivalence.PARTIAL,
    ),
    ToolMapping(
        codex_tool="image_generation",
        codex_description="Generate images from text prompts",
        bash_native="— (no native equivalent)",
        advanced_shell="ImageMagick convert for procedural images. gnuplot for charts. graphviz for diagrams",
        modern_cli="curl to DALL-E/SD APIs. stable-diffusion.cpp locally",
        equivalence=Equivalence.NONE,
        key_insight="Unix has procedural image tools but no generative AI. API calls via curl bridge the gap.",
        container_tool="podman run --gpus all stable-diffusion IMAGE 'prompt'  (GPU-passthrough container)",
        container_upgrade="Podman with --device nvidia.com/gpu=all runs local SD/FLUX in a container. DGX Spark's 128GB makes this viable.",
        equivalence_with_containers=Equivalence.PARTIAL,
    ),
    ToolMapping(
        codex_tool="tool_search",
        codex_description="Search for available tools by capability",
        bash_native="compgen -c prefix  |  type cmd  |  which cmd  |  command -v cmd",
        advanced_shell="apropos keyword (search man pages). dpkg -S $(which cmd). apt-file search bin/cmd",
        modern_cli="tldr, nix search, brew search, command-not-found",
        equivalence=Equivalence.FULL,
        key_insight="compgen -c is 'list all available commands'. apropos is 'search by what it does'. Together = tool_search.",
    ),

    # =========================================================================
    # MCP & EXTENSIBILITY (3 tools)
    # =========================================================================

    ToolMapping(
        codex_tool="tool_suggest",
        codex_description="Suggest a tool/connector to install",
        bash_native="command_not_found_handle() — bash hook when command not found",
        advanced_shell="apt-file search, pacman -F, dnf provides — find which package provides a command",
        modern_cli="nix search, brew search, command-not-found handler",
        equivalence=Equivalence.PARTIAL,
        key_insight="command_not_found_handle is the hook. But there's no 'what tool for this task?' reasoning.",
        container_tool="toolbox create --distro fedora tools && toolbox run -c tools dnf provides '*/cmdname'",
        container_upgrade="Spin up a fresh distro toolbox to search packages. Different distros = different package universes. Try them all.",
        equivalence_with_containers=Equivalence.FULL,
    ),
    ToolMapping(
        codex_tool="list_mcp_resources",
        codex_description="List resources from external services",
        bash_native="curl API_URL | jq '.'  |  psql -c 'SELECT ...'  |  aws s3 ls",
        advanced_shell="FUSE mounts (s3fs, sshfs, rclone mount) — expose remote resources as local filesystem",
        modern_cli="rclone, usql, httpie, mc (MinIO), pgcli",
        equivalence=Equivalence.PARTIAL,
        key_insight="FUSE is the unix 'everything is a file' answer: mount remote resources, then ls/cat/grep them.",
        container_tool="podman run --network host -v /mnt/fuse:/mnt:ro rclone-image lsd remote:",
        container_upgrade="Run FUSE/rclone/DB clients in containers with network access. Isolated credentials per container.",
        equivalence_with_containers=Equivalence.FULL,
    ),
    ToolMapping(
        codex_tool="read_mcp_resource",
        codex_description="Read a specific resource by URI",
        bash_native="curl $URI  |  cat /fuse/mount/path  |  sqlite3 db 'SELECT ...'",
        advanced_shell="Custom protocol handlers: xdg-open for URI dispatch. FUSE for transparent access",
        modern_cli="rclone cat remote:path, httpie, usql",
        equivalence=Equivalence.PARTIAL,
        key_insight="If it's FUSE-mounted, cat IS read_mcp_resource. The unix trick is making everything look like a file.",
        container_tool="podman run --network host rclone-image cat remote:bucket/file.json | jq '.'",
        container_upgrade="Each resource backend in its own container. Credentials isolated. FUSE mount or direct CLI access.",
        equivalence_with_containers=Equivalence.FULL,
    ),
]


# ---------------------------------------------------------------------------
# Summary statistics
# ---------------------------------------------------------------------------

def summary(with_containers: bool = False) -> dict[str, int]:
    counts = {e: 0 for e in Equivalence}
    for m in MATRIX:
        level = m.equivalence_with_containers if with_containers else m.equivalence
        counts[level] += 1
    return {e.name: c for e, c in counts.items()}


_SYM = {
    Equivalence.FULL: "●",
    Equivalence.PARTIAL: "◐",
    Equivalence.NONE: "○",
}


def print_matrix(compact: bool = False) -> None:
    """Print the matrix with both shell-only and container-enhanced ratings."""
    header = f"{'#':<3} {'Codex Tool':<28} {'Bash Native':<38} {'Shell':<6} {'+ Container':<12}"
    print(header)
    print("─" * len(header))
    for i, m in enumerate(MATRIX, 1):
        shell_sym = f"{_SYM[m.equivalence]} {m.equivalence.name}"
        container_sym = ""
        if m.equivalence_with_containers != m.equivalence:
            container_sym = f"→ {_SYM[m.equivalence_with_containers]} {m.equivalence_with_containers.name}"
        elif m.container_tool:
            container_sym = f"  {_SYM[m.equivalence_with_containers]} (same)"
        native = m.bash_native[:36] if compact else m.bash_native
        print(f"{i:<3} {m.codex_tool:<28} {native:<38} {shell_sym:<12} {container_sym}")

    shell_totals = summary(with_containers=False)
    container_totals = summary(with_containers=True)
    print(f"\nShell only:       ● {shell_totals['FULL']:>2}  ◐ {shell_totals['PARTIAL']:>2}  ○ {shell_totals['NONE']:>2}")
    print(f"+ Containers:     ● {container_totals['FULL']:>2}  ◐ {container_totals['PARTIAL']:>2}  ○ {container_totals['NONE']:>2}")
    upgraded = sum(1 for m in MATRIX if m.equivalence_with_containers != m.equivalence)
    print(f"Upgrades:         {upgraded} tools improved by adding toolbox/podman")


def print_container_details() -> None:
    """Print just the container-enhanced entries with details."""
    print("Container-Enhanced Tool Mappings")
    print("=" * 60)
    for m in MATRIX:
        if m.container_tool:
            upgraded = " ↑" if m.equivalence_with_containers != m.equivalence else ""
            print(f"\n{m.codex_tool}{upgraded}")
            print(f"  Container:  {m.container_tool}")
            print(f"  Upgrade:    {m.container_upgrade}")
            if m.equivalence_with_containers != m.equivalence:
                print(f"  Rating:     {m.equivalence.name} → {m.equivalence_with_containers.name}")


if __name__ == "__main__":
    print_matrix()
    print()
    print_container_details()
