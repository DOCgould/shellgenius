//! Codex ↔ Shell Equivalence Matrix
//!
//! Maps every OpenAI Codex CLI tool to its native bash/unix equivalent,
//! plus how toolbox/podman elevate the shell equivalents.

use std::sync::LazyLock;

use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum Equivalence {
    Full,
    Partial,
    None,
}

#[derive(Debug, Clone)]
pub struct ToolMapping {
    pub codex_tool: String,
    pub codex_description: String,
    pub bash_native: String,
    pub advanced_shell: String,
    pub modern_cli: String,
    pub equivalence: Equivalence,
    pub key_insight: String,
    pub container_tool: String,
    pub container_upgrade: String,
    pub equivalence_with_containers: Equivalence,
}

fn mapping(
    codex_tool: &str, codex_desc: &str, bash: &str, advanced: &str, modern: &str,
    equiv: Equivalence, insight: &str, container: &str, upgrade: &str, equiv_c: Equivalence,
) -> ToolMapping {
    ToolMapping {
        codex_tool: codex_tool.into(), codex_description: codex_desc.into(),
        bash_native: bash.into(), advanced_shell: advanced.into(), modern_cli: modern.into(),
        equivalence: equiv, key_insight: insight.into(),
        container_tool: container.into(), container_upgrade: upgrade.into(),
        equivalence_with_containers: equiv_c,
    }
}

pub static MATRIX: LazyLock<Vec<ToolMapping>> = LazyLock::new(|| {
    use Equivalence::*;
    vec![
        // SHELL EXECUTION
        mapping("shell / shell_command", "Execute a command, capture stdout/stderr/exit code",
            "cmd  |  $(cmd)  |  bash -c 'cmd'",
            "exec (replace process), coproc (bidirectional), subshell ( cmd )",
            "timeout, script (PTY alloc), env -i (clean env)",
            Full, "The shell IS the command executor.",
            "toolbox run -c NAME cmd  |  podman exec NAME cmd",
            "Container gives you: isolated PID/net/mount namespace, sandbox_permissions via profiles.",
            Full),
        mapping("exec_command", "Run in PTY, return session ID for ongoing interaction",
            "coproc NAME { cmd; }",
            "script -qc 'cmd' /dev/null (force PTY), expect (PTY automation)",
            "tmux new-window, zellij",
            Full, "coproc gives you a persistent session with stdin/stdout FDs and a PID.",
            "", "", Full),
        mapping("write_stdin", "Write to a running process's stdin, poll for output",
            r#"echo data >&"${COPROC[1]}"  |  echo > /tmp/fifo"#,
            "mkfifo (named pipe), /proc/$PID/fd/0 (direct fd write), coproc FDs",
            "tmux send-keys, expect, socat",
            Full, "coproc[1] IS write_stdin. coproc[0] IS read_stdout.",
            "", "", Full),
        // FILE OPERATIONS
        mapping("apply_patch", "Apply structured file edits (add/delete/update hunks)",
            "patch -p1 < diff.patch",
            "ed (scriptable line editor), diff -e (outputs ed scripts), sed -i",
            "delta (viewer), sd (sed alt), comby (structural), ast-grep",
            Full, "patch(1) is the ORIGINAL implementation of this concept.",
            "", "", Full),
        mapping("apply_patch (freeform)", "Custom Lark grammar for file edits",
            "printf '/pattern/c\\nnew line\\n.\\nw\\n' | ed -s file",
            "ed scripts, awk structural transforms, perl -pi -e, m4 macros",
            "comby (structural search/replace), ast-grep, fastmod",
            Partial, "ed can do anything the freeform grammar can, but translating the grammar requires a parser.",
            "toolbox run -c dev comby 'PATTERN' 'REPLACEMENT' -i .py",
            "Install comby/ast-grep in a toolbox without polluting host.",
            Full),
        mapping("list_dir", "List directory with offset, limit, and depth control",
            "find . -maxdepth N | sort | tail -n +OFFSET | head -n LIMIT",
            "find -printf for custom format, shopt -s globstar",
            "fd --max-depth N, eza --tree --level=N, broot",
            Full, "find + head/tail gives you pagination. -maxdepth gives you depth.",
            "", "", Full),
        // AGENT/SUB-AGENT SYSTEM
        mapping("spawn_agent", "Launch an independent sub-task with its own context",
            "cmd &  then  PID=$!",
            "coproc AGENT { cmd; }, setsid (new session), process groups",
            "GNU parallel, xargs -P, tmux new-window",
            Full, "& is spawn_agent. $! is agent_id. Process groups are agent trees.",
            "podman run -d --name agent-1 IMAGE cmd",
            "Each agent gets full PID/net/mount isolation. Clean kill via podman stop.",
            Full),
        mapping("send_input / send_message", "Send data/message to a running agent",
            r#"echo msg >&"${AGENT[1]}"  |  kill -SIGUSR1 $PID"#,
            "mkfifo /tmp/agent_inbox, /proc/$PID/fd/0, unix domain sockets",
            "tmux send-keys, socat UNIX-CONNECT, dbus-send",
            Full, "Coproc FDs = typed message channel. Signals = interrupt.",
            "", "", Full),
        mapping("wait_agent", "Wait for agent(s) to reach final status",
            "wait $PID  |  wait -n  |  wait -n -p VAR",
            "trap 'handler' CHLD, pidfd_open + poll (Linux 5.3+)",
            "GNU parallel --wait",
            Full, "wait -n -p VAR (bash 5.1) is the exact equivalent.",
            "", "", Full),
        mapping("close_agent", "Kill an agent and its entire subtree",
            "kill -- -$PGID",
            "setsid at spawn → clean tree kill. pkill -P (children)",
            "timeout --signal=TERM 30s cmd",
            Full, "setsid at spawn time + kill -PGID = clean tree kill.",
            "podman stop NAME  |  podman kill NAME",
            "Container stop kills ENTIRE process tree guaranteed.",
            Full),
        mapping("resume_agent", "Resume a previously stopped agent",
            "kill -CONT $PID  |  fg %job  |  bg %job",
            "SIGSTOP/SIGCONT pair. Cgroup freezer. ptrace(PTRACE_CONT)",
            "reptyr (reattach to different terminal)",
            Full, "SIGSTOP/SIGCONT is the kernel primitive.",
            "podman unpause NAME  |  podman start NAME",
            "podman pause/unpause freezes the ENTIRE cgroup.",
            Full),
        mapping("list_agents", "List running agents in the thread tree",
            "jobs -l",
            "ps --ppid $$ -o pid,stat,cmd  |  pstree -p $$",
            "procs --tree, htop -p",
            Full, "jobs is list_agents. ps --ppid $$ is the programmatic version.",
            "podman ps --format json  |  toolbox list",
            "Structured JSON output with state, image, ports, mounts.",
            Full),
        mapping("spawn_agents_on_csv", "Fan out: one worker per CSV row",
            r#"while IFS=, read -r c1 c2; do process "$c1" "$c2" & done < data.csv"#,
            "GNU parallel --csv -a data.csv 'cmd {1} {2}'",
            "GNU parallel --csv, miller (mlr), xsv + parallel",
            Full, "GNU parallel --csv is PURPOSE-BUILT for this exact pattern.",
            "", "", Full),
        mapping("report_agent_job_result", "Worker reports result back to coordinator",
            "exit $status | result=$(cmd)",
            "Named pipe: echo result > /tmp/results_fifo. /dev/shm for shared mem",
            "GNU parallel --results dir/",
            Full, "Exit code IS the simplest result. stdout IS the data channel.",
            "", "", Full),
        // PLANNING & INTERACTION
        mapping("update_plan", "Structured task plan with step + status tracking",
            "declare -A PLAN; PLAN[step1]='done'",
            "jq + flock for concurrency",
            "jq, taskwarrior, todo.txt-cli, sqlite3",
            Partial, "Shell can track state but lacks structured task management.",
            "podman ps --format '{{.Names}} {{.State}}'",
            "Each task IS a container. Its state IS the plan status.",
            Full),
        mapping("request_user_input", "Prompt user with multiple-choice questions",
            "select opt in opt1 opt2 opt3; do ...; done",
            "exec 3</dev/tty, dialog/whiptail for TUI",
            "gum choose, gum input, fzf, dialog",
            Full, "select is the built-in menu. /dev/tty lets you prompt inside a pipeline.",
            "", "", Full),
        mapping("request_permissions", "Ask for filesystem/network access grants",
            "read -p 'Allow access to /path? [y/n] '",
            "setfacl, capabilities(7), landlock, seccomp, bubblewrap",
            "firejail, bubblewrap, podman --cap-drop=ALL",
            Partial, "Unix has powerful permission primitives but they're OS-level, not app-level consent.",
            "podman run --network=none --read-only --cap-drop=ALL -v /path:/path:ro IMAGE cmd",
            "Sandbox profiles ARE permission grants. The container boundary enforces the permission.",
            Full),
        // CODE & REPL
        mapping("js_repl", "Persistent JavaScript REPL with state between evaluations",
            "coproc NODE { node -i 2>&1; }",
            "mkfifo pair + node -i. expect. script -qc 'node -i'",
            "expect, tmux send-keys + capture-pane",
            Full, "coproc IS a persistent REPL session.",
            "", "", Full),
        mapping("js_repl_reset", "Restart the REPL kernel, clear state",
            "kill $NODE_PID; coproc NODE { node -i 2>&1; }",
            "Send '.exit\\n' to coproc, wait, respawn",
            "tmux respawn-pane",
            Full, "Kill + respawn. Processes are cheap.",
            "", "", Full),
        mapping("exec (code mode)", "Execute code with nested tool access",
            r#"eval "$code"  |  source /dev/stdin <<< "$code""#,
            "bash -c with exported functions. source <(cmd)",
            "tmux, script",
            Full, "eval/source is code mode. export -f gives nested tool access.",
            "", "", Full),
        mapping("wait (code mode)", "Poll a yielded exec cell for new output",
            "read -t 0.1 line <&${COPROC[0]}",
            "while IFS= read -r -t $YIELD line <&$FD; do ...; done",
            "expect (expect/timeout pattern)",
            Full, "read -t TIMEOUT on a coproc FD is exactly 'poll for output with yield_time_ms'.",
            "", "", Full),
        // MEDIA & SEARCH
        mapping("view_image", "Display an image from the filesystem",
            "xdg-open image.png  — MIME-routed dispatch to desktop app",
            "Kitty icat, Sixel img2sixel, chafa (terminal braille art)",
            "chafa, viu, catimg, timg, wezterm imgcat",
            Full, "xdg-open IS view_image — it's a universal MIME-routed dispatcher.",
            "toolbox run -c gui xdg-open image.png",
            "Toolbox auto-mounts Wayland/X11 sockets — GUI apps work inside.",
            Full),
        mapping("web_search", "Search the web, return results",
            "— (no native equivalent)",
            "curl + HTML parsing: curl -s 'https://html.duckduckgo.com/html/?q=...' | htmlq",
            "ddgr (DuckDuckGo), googler, surfraw",
            None, "The shell has no concept of 'search the web'.",
            "toolbox run -c search ddgr -n 5 'query'",
            "Toolbox lets you install ddgr/googler without host pollution.",
            Partial),
        mapping("image_generation", "Generate images from text prompts",
            "— (no native equivalent)",
            "ImageMagick convert for procedural images. gnuplot. graphviz",
            "curl to DALL-E/SD APIs. stable-diffusion.cpp",
            None, "Unix has procedural image tools but no generative AI.",
            "podman run --gpus all stable-diffusion IMAGE 'prompt'",
            "Podman with GPU passthrough runs local SD in a container.",
            Partial),
        mapping("tool_search", "Search for available tools by capability",
            "compgen -c prefix  |  type cmd  |  which cmd",
            "apropos keyword, dpkg -S $(which cmd), apt-file search bin/cmd",
            "tldr, nix search, brew search, command-not-found",
            Full, "compgen -c + apropos = tool_search.",
            "", "", Full),
        // MCP & EXTENSIBILITY
        mapping("tool_suggest", "Suggest a tool/connector to install",
            "command_not_found_handle()",
            "apt-file search, pacman -F, dnf provides",
            "nix search, brew search",
            Partial, "command_not_found_handle is the hook. No 'what tool for task X?' reasoning.",
            "toolbox create --distro fedora tools && toolbox run -c tools dnf provides '*/cmdname'",
            "Spin up a fresh distro toolbox to search packages.",
            Full),
        mapping("list_mcp_resources", "List resources from external services",
            "curl API_URL | jq '.'  |  psql -c 'SELECT ...'  |  aws s3 ls",
            "FUSE mounts (s3fs, sshfs, rclone mount)",
            "rclone, usql, httpie, mc (MinIO), pgcli",
            Partial, "FUSE is the unix 'everything is a file' answer.",
            "podman run --network host rclone-image lsd remote:",
            "Run FUSE/rclone/DB clients in containers. Isolated credentials per container.",
            Full),
        mapping("read_mcp_resource", "Read a specific resource by URI",
            "curl $URI  |  cat /fuse/mount/path  |  sqlite3 db 'SELECT ...'",
            "xdg-open for URI dispatch. FUSE for transparent access",
            "rclone cat remote:path, httpie, usql",
            Partial, "If it's FUSE-mounted, cat IS read_mcp_resource.",
            "podman run --network host rclone-image cat remote:bucket/file.json | jq '.'",
            "Each resource backend in its own container. Credentials isolated.",
            Full),
    ]
});

pub fn summary(with_containers: bool) -> std::collections::HashMap<Equivalence, usize> {
    let mut counts = std::collections::HashMap::new();
    for m in MATRIX.iter() {
        let level = if with_containers {
            m.equivalence_with_containers
        } else {
            m.equivalence
        };
        *counts.entry(level).or_insert(0) += 1;
    }
    counts
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_matrix_has_entries() {
        assert!(MATRIX.len() >= 28); // some entries are grouped (e.g. "send_input / send_message")
    }

    #[test]
    fn test_summary_shell_only() {
        let s = summary(false);
        assert!(s.get(&Equivalence::Full).copied().unwrap_or(0) >= 20);
    }

    #[test]
    fn test_summary_with_containers_is_better() {
        let shell = summary(false);
        let containers = summary(true);
        assert!(containers.get(&Equivalence::Full).unwrap_or(&0) >= shell.get(&Equivalence::Full).unwrap_or(&0));
    }

    #[test]
    fn test_no_none_upgrades_to_full() {
        for m in MATRIX.iter() {
            if m.equivalence == Equivalence::None {
                assert_ne!(m.equivalence_with_containers, Equivalence::Full,
                    "{}: NONE shouldn't jump to FULL", m.codex_tool);
            }
        }
    }
}
