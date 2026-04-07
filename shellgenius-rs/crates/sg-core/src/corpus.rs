//! Shell knowledge corpus — the "brain" of ShellGenius.
//!
//! Encodes deep expertise about POSIX shells, Bash, Zsh, fish,
//! pipe idioms, file descriptor tricks, process substitution, and the
//! lesser-known corners of shell scripting.

use std::collections::HashMap;
use std::sync::LazyLock;

use serde::{Deserialize, Serialize};

// ---------------------------------------------------------------------------
// Shell & PipePattern enums
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "UPPERCASE")]
pub enum Shell {
    Posix,
    Bash,
    Zsh,
    Fish,
    Dash,
    Ksh,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum PipePattern {
    Filter,
    Transform,
    Aggregate,
    Fanout,
    Funnel,
    Sieve,
    Accumulate,
    Monitor,
}

// ---------------------------------------------------------------------------
// PipeIdiom
// ---------------------------------------------------------------------------

#[derive(Debug, Clone)]
pub struct PipeIdiom {
    pub name: String,
    pub pattern: String,
    pub shells: Vec<Shell>,
    pub category: PipePattern,
    pub explanation: String,
    pub gotchas: String,
}

impl PipeIdiom {
    pub fn for_shell(&self, shell: Shell) -> bool {
        self.shells.contains(&shell)
    }
}

fn idiom(
    name: &str,
    pattern: &str,
    category: PipePattern,
    shells: &[Shell],
    explanation: &str,
    gotchas: &str,
) -> PipeIdiom {
    PipeIdiom {
        name: name.into(),
        pattern: pattern.into(),
        shells: shells.to_vec(),
        category,
        explanation: explanation.into(),
        gotchas: gotchas.into(),
    }
}

const BZP: &[Shell] = &[Shell::Bash, Shell::Zsh, Shell::Posix];
const BZ: &[Shell] = &[Shell::Bash, Shell::Zsh];

pub static PIPE_IDIOMS: LazyLock<Vec<PipeIdiom>> = LazyLock::new(|| {
    vec![
        // === FILTER ===
        idiom("inverse_grep_chain",
            "cmd | grep -v 'noise1' | grep -v 'noise2'",
            PipePattern::Filter, BZP,
            "Subtract known noise progressively. Each grep -v removes one class of unwanted lines.",
            "Order doesn't matter for correctness, but put the most selective filter first for speed."),
        idiom("grep_context_extract",
            "cmd | grep -A5 -B2 'PATTERN'",
            PipePattern::Filter, BZP,
            "Extract a window around matches. -A (after) and -B (before) give surrounding context.",
            ""),
        // === TRANSFORM ===
        idiom("field_extraction",
            "cmd | awk '{print $2, $NF}'",
            PipePattern::Transform, BZP,
            "Extract specific fields. $2 is the second field, $NF is the last. Awk auto-splits on whitespace.",
            "If the delimiter isn't whitespace, use -F: awk -F: '{print $1}'"),
        idiom("stream_sed_replace",
            "cmd | sed 's/old/new/g'",
            PipePattern::Transform, BZP,
            "Global find-and-replace in a stream. The 'g' flag replaces all occurrences per line.",
            ""),
        idiom("column_reorder",
            "cmd | awk '{print $3, $1, $2}'",
            PipePattern::Transform, BZP,
            "Reorder columns without temp files. Awk is the natural tool for columnar reshaping.",
            ""),
        idiom("json_pipe_jq",
            "cmd | jq '.[] | {name: .name, count: .items | length}'",
            PipePattern::Transform, BZP,
            "jq is the awk of JSON. Pipe structured data through it for extraction and reshaping.",
            ""),
        // === AGGREGATE ===
        idiom("frequency_count",
            "cmd | sort | uniq -c | sort -rn | head -20",
            PipePattern::Aggregate, BZP,
            "The classic histogram: sort to group, uniq -c to count, sort -rn for descending, head to cap.",
            "uniq requires sorted input — always sort first."),
        idiom("line_count_by_type",
            "find . -name '*.py' | xargs wc -l | sort -n",
            PipePattern::Aggregate, BZP,
            "Count lines per file, sorted. xargs batches filenames into wc invocations.",
            "Files with spaces break this. Use: find . -name '*.py' -print0 | xargs -0 wc -l"),
        // === FANOUT ===
        idiom("tee_split",
            "cmd | tee output.log | grep ERROR",
            PipePattern::Fanout, BZP,
            "tee duplicates the stream: one copy to file, the other continues down the pipe.",
            ""),
        idiom("process_substitution_diff",
            "diff <(cmd1) <(cmd2)",
            PipePattern::Fanout, BZ,
            "Compare two command outputs without temp files. <() creates an anonymous named pipe (fd-backed).",
            "Not POSIX. Not available in dash, sh, or fish. Fish uses (cmd | psub) instead."),
        idiom("tee_to_multiple_commands",
            "cmd | tee >(proc1) >(proc2) > /dev/null",
            PipePattern::Fanout, BZ,
            "Fan out one stream to N consumers using process substitution with tee.",
            "Order of completion is nondeterministic. If you need synchronization, use named pipes."),
        // === FUNNEL ===
        idiom("paste_merge",
            "paste <(cmd1) <(cmd2)",
            PipePattern::Funnel, BZ,
            "Merge two streams side-by-side, tab-separated. Combines columnar data from different sources.",
            ""),
        idiom("comm_set_ops",
            "comm -23 <(sort file1) <(sort file2)",
            PipePattern::Funnel, BZ,
            "Set difference: lines in file1 but not file2. comm -12 gives intersection, comm -3 gives symmetric diff.",
            ""),
        // === SIEVE ===
        idiom("awk_filter_and_transform",
            r#"cmd | awk '/PATTERN/ {gsub(/old/,"new",$3); print $1, $3}'"#,
            PipePattern::Sieve, BZP,
            "Awk can filter AND transform in one pass. /PATTERN/ selects rows, the block reshapes them.",
            ""),
        // === ACCUMULATE ===
        idiom("xargs_parallel",
            "cmd | xargs -P4 -I{} process {}",
            PipePattern::Accumulate, BZP,
            "Fan out to N parallel workers. -P4 runs 4 processes concurrently. -I{} sets the placeholder.",
            "Watch for argument quoting. Use -print0/-0 for filenames with spaces."),
        idiom("gnu_parallel",
            "cmd | parallel -j8 'process {}'",
            PipePattern::Accumulate, BZP,
            "GNU parallel is xargs on steroids: job slots, progress bars, retries, remote execution.",
            ""),
        // === MONITOR ===
        idiom("pv_throughput",
            "pv input.bin | gzip > output.gz",
            PipePattern::Monitor, BZP,
            "pv (pipe viewer) shows throughput, ETA, and progress bar for any stream.",
            ""),
        idiom("stderr_tap",
            "cmd | tee /dev/stderr | next_cmd",
            PipePattern::Monitor, BZP,
            "Tap the stream for debugging: tee to stderr shows data mid-pipe without disrupting flow.",
            ""),
    ]
});

// ---------------------------------------------------------------------------
// FdTrick
// ---------------------------------------------------------------------------

#[derive(Debug, Clone)]
pub struct FdTrick {
    pub name: String,
    pub pattern: String,
    pub explanation: String,
    pub shells: Vec<Shell>,
}

pub static FD_TRICKS: LazyLock<Vec<FdTrick>> = LazyLock::new(|| {
    vec![
        FdTrick {
            name: "swap_stdout_stderr".into(),
            pattern: "cmd 3>&1 1>&2 2>&3 3>&-".into(),
            explanation: "Swap stdout and stderr using fd 3 as a temp. Classic three-way swap.".into(),
            shells: vec![Shell::Bash, Shell::Zsh],
        },
        FdTrick {
            name: "capture_stderr_only".into(),
            pattern: "output=$(cmd 2>&1 1>/dev/null)".into(),
            explanation: "Capture only stderr into a variable by redirecting stdout to /dev/null first.".into(),
            shells: vec![Shell::Bash, Shell::Zsh],
        },
        FdTrick {
            name: "coproc_bidirectional".into(),
            pattern: "coproc myproc { cmd; }; echo input >&${myproc[1]}; read output <&${myproc[0]}".into(),
            explanation: "Bash coproc creates a bidirectional pipe. Write to [1], read from [0]. Interactive IPC without named pipes.".into(),
            shells: vec![Shell::Bash],
        },
        FdTrick {
            name: "fd_lock".into(),
            pattern: "exec 9>/tmp/lockfile; flock -n 9 || exit 1".into(),
            explanation: "Advisory file locking using fd 9 and flock. Prevents concurrent script instances.".into(),
            shells: vec![Shell::Bash],
        },
        FdTrick {
            name: "heredoc_fd".into(),
            pattern: "exec 3<<< 'inline data'; cat <&3".into(),
            explanation: "Feed inline data through a file descriptor. Useful for passing data to commands that expect fd input.".into(),
            shells: vec![Shell::Bash, Shell::Zsh],
        },
        FdTrick {
            name: "named_pipe_ipc".into(),
            pattern: "mkfifo /tmp/pipe; cmd1 > /tmp/pipe & cmd2 < /tmp/pipe; rm /tmp/pipe".into(),
            explanation: "Named pipes (FIFOs) for IPC between unrelated processes. Works everywhere POSIX does.".into(),
            shells: vec![Shell::Posix, Shell::Bash, Shell::Zsh, Shell::Dash],
        },
    ]
});

// ---------------------------------------------------------------------------
// QuotingRule
// ---------------------------------------------------------------------------

#[derive(Debug, Clone)]
pub struct QuotingRule {
    pub name: String,
    pub rule: String,
    pub example: String,
    pub pitfall: String,
}

pub static QUOTING_RULES: LazyLock<Vec<QuotingRule>> = LazyLock::new(|| {
    vec![
        QuotingRule {
            name: "single_vs_double".into(),
            rule: "Single quotes preserve everything literally. Double quotes allow $expansion and \\escaping.".into(),
            example: r#"echo '$HOME' => $HOME (literal)  |  echo "$HOME" => /home/user (expanded)"#.into(),
            pitfall: String::new(),
        },
        QuotingRule {
            name: "nested_quoting".into(),
            rule: r#"You cannot nest single quotes. Use $'...' (ANSI-C) or alternate: "it's" or 'it'\''s'"#.into(),
            example: r"echo 'it'\''s a trap'  =>  it's a trap".into(),
            pitfall: r"Many people try 'it\'s' — this does NOT work. The backslash is not special inside single quotes.".into(),
        },
        QuotingRule {
            name: "word_splitting".into(),
            rule: r#"Unquoted $var undergoes word splitting and glob expansion. Always quote: "$var""#.into(),
            example: r#"f="my file.txt"; cat $f => error (splits into 'my' and 'file.txt'); cat "$f" => works"#.into(),
            pitfall: "This is the #1 source of shell bugs. ShellCheck catches it. Always quote your variables.".into(),
        },
        QuotingRule {
            name: "array_expansion".into(),
            rule: r#""${arr[@]}" expands each element as a separate word. ${arr[*]} joins them into one word."#.into(),
            example: r#"arr=("a b" "c d"); printf '[%s]' "${arr[@]}" => [a b][c d]"#.into(),
            pitfall: r"${arr[@]} without quotes re-splits elements on whitespace — almost never what you want.".into(),
        },
        QuotingRule {
            name: "shlex_split".into(),
            rule: "shlex.split() mirrors Bash word-splitting. shlex.quote() safely escapes for shell injection.".into(),
            example: r#"shlex.split("echo 'hello world' foo") => ['echo', 'hello world', 'foo']"#.into(),
            pitfall: "shlex.split() does NOT handle Bash arrays, process substitution, or brace expansion.".into(),
        },
        QuotingRule {
            name: "eval_danger".into(),
            rule: "eval re-parses a string as shell code. It's almost always the wrong tool — use arrays instead.".into(),
            example: r#"cmd='ls -la "my dir"'; eval $cmd  # works but dangerous. Better: cmd=(ls -la "my dir"); "${cmd[@]}""#.into(),
            pitfall: "eval + user input = arbitrary code execution. This is how shell injection happens.".into(),
        },
    ]
});

// ---------------------------------------------------------------------------
// Cross-shell compatibility matrix
// ---------------------------------------------------------------------------

/// COMPAT_NOTES[feature][shell] -> description
pub static COMPAT_NOTES: LazyLock<HashMap<String, HashMap<String, String>>> = LazyLock::new(|| {
    let mut m = HashMap::new();

    let mut ps = HashMap::new();
    ps.insert("bash".into(), "<(cmd) and >(cmd) — fully supported".into());
    ps.insert("zsh".into(), "<(cmd) and >(cmd) — fully supported".into());
    ps.insert("fish".into(), "(cmd | psub) — different syntax".into());
    ps.insert("dash".into(), "NOT SUPPORTED — use named pipes or temp files".into());
    ps.insert("posix".into(), "NOT SUPPORTED — not in POSIX spec".into());
    m.insert("process_substitution".into(), ps);

    let mut arr = HashMap::new();
    arr.insert("bash".into(), "arr=(a b c); ${arr[0]}".into());
    arr.insert("zsh".into(), "arr=(a b c); ${arr[1]} — 1-indexed!".into());
    arr.insert("fish".into(), "set arr a b c; $arr[1] — 1-indexed".into());
    arr.insert("dash".into(), "NOT SUPPORTED — use positional params: set -- a b c".into());
    arr.insert("posix".into(), "NOT SUPPORTED".into());
    m.insert("arrays".into(), arr);

    let mut pf = HashMap::new();
    pf.insert("bash".into(), "set -o pipefail — exit code is rightmost failure".into());
    pf.insert("zsh".into(), "set -o pipefail — same as bash".into());
    pf.insert("fish".into(), "built-in: $pipestatus array".into());
    pf.insert("dash".into(), "NOT SUPPORTED".into());
    pf.insert("posix".into(), "NOT SUPPORTED — POSIX only gives last command's exit code".into());
    m.insert("pipefail".into(), pf);

    let mut be = HashMap::new();
    be.insert("bash".into(), "{a,b,c} and {1..10} — fully supported".into());
    be.insert("zsh".into(), "{a,b,c} and {1..10} — fully supported".into());
    be.insert("fish".into(), "NOT SUPPORTED — use (seq 1 10) or explicit lists".into());
    be.insert("dash".into(), "NOT SUPPORTED".into());
    be.insert("posix".into(), "NOT SUPPORTED — not in POSIX spec".into());
    m.insert("brace_expansion".into(), be);

    m
});

// ---------------------------------------------------------------------------
// Lookup functions
// ---------------------------------------------------------------------------

pub fn lookup_idioms(
    category: Option<PipePattern>,
    shell: Option<Shell>,
) -> Vec<PipeIdiom> {
    PIPE_IDIOMS
        .iter()
        .filter(|i| category.is_none_or(|c| i.category == c))
        .filter(|i| shell.is_none_or(|s| i.for_shell(s)))
        .cloned()
        .collect()
}

pub fn lookup_fd_tricks(shell: Option<Shell>) -> Vec<FdTrick> {
    FD_TRICKS
        .iter()
        .filter(|t| shell.is_none_or(|s| t.shells.contains(&s)))
        .cloned()
        .collect()
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_idioms_not_empty() {
        assert!(PIPE_IDIOMS.len() > 10);
    }

    #[test]
    fn test_lookup_by_category() {
        let filters = lookup_idioms(Some(PipePattern::Filter), None);
        assert!(filters.iter().all(|i| i.category == PipePattern::Filter));
        assert!(filters.len() >= 2);
    }

    #[test]
    fn test_lookup_by_shell() {
        let posix = lookup_idioms(None, Some(Shell::Posix));
        for i in &posix {
            assert!(i.shells.contains(&Shell::Posix));
        }
    }

    #[test]
    fn test_fd_tricks_not_empty() {
        assert!(FD_TRICKS.len() >= 5);
    }

    #[test]
    fn test_quoting_rules_not_empty() {
        assert!(QUOTING_RULES.len() >= 5);
    }

    #[test]
    fn test_fd_tricks_by_shell() {
        let bash_tricks = lookup_fd_tricks(Some(Shell::Bash));
        for t in &bash_tricks {
            assert!(t.shells.contains(&Shell::Bash));
        }
    }

    #[test]
    fn test_compat_notes_has_entries() {
        assert!(COMPAT_NOTES.contains_key("process_substitution"));
        assert!(COMPAT_NOTES.contains_key("arrays"));
        assert!(COMPAT_NOTES.contains_key("pipefail"));
        assert!(COMPAT_NOTES.contains_key("brace_expansion"));
    }
}
