"""
Shell knowledge corpus — the "brain" of ShellGenius.

This module encodes deep expertise about POSIX shells, Bash, Zsh, fish,
pipe idioms, file descriptor tricks, process substitution, and the
lesser-known corners of shell scripting that separate novices from wizards.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional


class Shell(Enum):
    POSIX = auto()   # /bin/sh — the universal baseline
    BASH = auto()     # GNU Bash — the workhorse
    ZSH = auto()      # Z shell — the power-user's shell
    FISH = auto()     # fish — the friendly shell (different syntax)
    DASH = auto()     # Debian Almquist — fast, minimal POSIX
    KSH = auto()      # Korn shell — originator of many features


class PipePattern(Enum):
    """Canonical pipe composition patterns."""
    FILTER = "filter"               # grep, awk, sed — narrow the stream
    TRANSFORM = "transform"         # sed, awk, cut, tr — reshape data
    AGGREGATE = "aggregate"         # sort | uniq -c, wc, tee — summarize
    FANOUT = "fanout"               # tee, process substitution — split stream
    FUNNEL = "funnel"               # paste, join, comm — merge streams
    SIEVE = "sieve"                 # filter + transform in one pass (awk)
    ACCUMULATE = "accumulate"       # xargs, parallel — batch into commands
    MONITOR = "monitor"             # pv, tee /dev/stderr — observe without altering


@dataclass(frozen=True)
class PipeIdiom:
    """A reusable pipe pattern with explanation."""
    name: str
    pattern: str
    shells: tuple[Shell, ...] = (Shell.BASH, Shell.ZSH, Shell.POSIX)
    category: PipePattern = PipePattern.FILTER
    explanation: str = ""
    gotchas: str = ""

    def for_shell(self, shell: Shell) -> bool:
        return shell in self.shells


# ---------------------------------------------------------------------------
# The Corpus: battle-tested pipe idioms
# ---------------------------------------------------------------------------

PIPE_IDIOMS: list[PipeIdiom] = [
    # === FILTER ===
    PipeIdiom(
        name="inverse_grep_chain",
        pattern="cmd | grep -v 'noise1' | grep -v 'noise2'",
        category=PipePattern.FILTER,
        explanation="Subtract known noise progressively. Each grep -v removes one class of unwanted lines.",
        gotchas="Order doesn't matter for correctness, but put the most selective filter first for speed.",
    ),
    PipeIdiom(
        name="grep_context_extract",
        pattern="cmd | grep -A5 -B2 'PATTERN'",
        category=PipePattern.FILTER,
        explanation="Extract a window around matches. -A (after) and -B (before) give surrounding context.",
    ),

    # === TRANSFORM ===
    PipeIdiom(
        name="field_extraction",
        pattern="cmd | awk '{print $2, $NF}'",
        category=PipePattern.TRANSFORM,
        explanation="Extract specific fields. $2 is the second field, $NF is the last. Awk auto-splits on whitespace.",
        gotchas="If the delimiter isn't whitespace, use -F: awk -F: '{print $1}'",
    ),
    PipeIdiom(
        name="stream_sed_replace",
        pattern="cmd | sed 's/old/new/g'",
        category=PipePattern.TRANSFORM,
        explanation="Global find-and-replace in a stream. The 'g' flag replaces all occurrences per line.",
    ),
    PipeIdiom(
        name="column_reorder",
        pattern="cmd | awk '{print $3, $1, $2}'",
        category=PipePattern.TRANSFORM,
        explanation="Reorder columns without temp files. Awk is the natural tool for columnar reshaping.",
    ),
    PipeIdiom(
        name="json_pipe_jq",
        pattern="cmd | jq '.[] | {name: .name, count: .items | length}'",
        category=PipePattern.TRANSFORM,
        explanation="jq is the awk of JSON. Pipe structured data through it for extraction and reshaping.",
    ),

    # === AGGREGATE ===
    PipeIdiom(
        name="frequency_count",
        pattern="cmd | sort | uniq -c | sort -rn | head -20",
        category=PipePattern.AGGREGATE,
        explanation="The classic histogram: sort to group, uniq -c to count, sort -rn for descending, head to cap.",
        gotchas="uniq requires sorted input — always sort first.",
    ),
    PipeIdiom(
        name="line_count_by_type",
        pattern="find . -name '*.py' | xargs wc -l | sort -n",
        category=PipePattern.AGGREGATE,
        explanation="Count lines per file, sorted. xargs batches filenames into wc invocations.",
        gotchas="Files with spaces break this. Use: find . -name '*.py' -print0 | xargs -0 wc -l",
    ),

    # === FANOUT ===
    PipeIdiom(
        name="tee_split",
        pattern="cmd | tee output.log | grep ERROR",
        category=PipePattern.FANOUT,
        explanation="tee duplicates the stream: one copy to file, the other continues down the pipe.",
    ),
    PipeIdiom(
        name="process_substitution_diff",
        pattern="diff <(cmd1) <(cmd2)",
        shells=(Shell.BASH, Shell.ZSH),
        category=PipePattern.FANOUT,
        explanation="Compare two command outputs without temp files. <() creates an anonymous named pipe (fd-backed).",
        gotchas="Not POSIX. Not available in dash, sh, or fish. Fish uses (cmd | psub) instead.",
    ),
    PipeIdiom(
        name="tee_to_multiple_commands",
        pattern="cmd | tee >(proc1) >(proc2) > /dev/null",
        shells=(Shell.BASH, Shell.ZSH),
        category=PipePattern.FANOUT,
        explanation="Fan out one stream to N consumers using process substitution with tee.",
        gotchas="Order of completion is nondeterministic. If you need synchronization, use named pipes.",
    ),

    # === FUNNEL ===
    PipeIdiom(
        name="paste_merge",
        pattern="paste <(cmd1) <(cmd2)",
        shells=(Shell.BASH, Shell.ZSH),
        category=PipePattern.FUNNEL,
        explanation="Merge two streams side-by-side, tab-separated. Combines columnar data from different sources.",
    ),
    PipeIdiom(
        name="comm_set_ops",
        pattern="comm -23 <(sort file1) <(sort file2)",
        shells=(Shell.BASH, Shell.ZSH),
        category=PipePattern.FUNNEL,
        explanation="Set difference: lines in file1 but not file2. comm -12 gives intersection, comm -3 gives symmetric diff.",
    ),

    # === SIEVE ===
    PipeIdiom(
        name="awk_filter_and_transform",
        pattern="cmd | awk '/PATTERN/ {gsub(/old/,\"new\",$3); print $1, $3}'",
        category=PipePattern.SIEVE,
        explanation="Awk can filter AND transform in one pass. /PATTERN/ selects rows, the block reshapes them.",
    ),

    # === ACCUMULATE ===
    PipeIdiom(
        name="xargs_parallel",
        pattern="cmd | xargs -P4 -I{} process {}",
        category=PipePattern.ACCUMULATE,
        explanation="Fan out to N parallel workers. -P4 runs 4 processes concurrently. -I{} sets the placeholder.",
        gotchas="Watch for argument quoting. Use -print0/-0 for filenames with spaces.",
    ),
    PipeIdiom(
        name="gnu_parallel",
        pattern="cmd | parallel -j8 'process {}'",
        category=PipePattern.ACCUMULATE,
        explanation="GNU parallel is xargs on steroids: job slots, progress bars, retries, remote execution.",
    ),

    # === MONITOR ===
    PipeIdiom(
        name="pv_throughput",
        pattern="pv input.bin | gzip > output.gz",
        category=PipePattern.MONITOR,
        explanation="pv (pipe viewer) shows throughput, ETA, and progress bar for any stream.",
    ),
    PipeIdiom(
        name="stderr_tap",
        pattern="cmd | tee /dev/stderr | next_cmd",
        category=PipePattern.MONITOR,
        explanation="Tap the stream for debugging: tee to stderr shows data mid-pipe without disrupting flow.",
    ),
]


# ---------------------------------------------------------------------------
# File Descriptor tricks — the deep lore
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FdTrick:
    name: str
    pattern: str
    explanation: str
    shells: tuple[Shell, ...] = (Shell.BASH, Shell.ZSH)

FD_TRICKS: list[FdTrick] = [
    FdTrick(
        name="swap_stdout_stderr",
        pattern="cmd 3>&1 1>&2 2>&3 3>&-",
        explanation="Swap stdout and stderr using fd 3 as a temp. Classic three-way swap.",
    ),
    FdTrick(
        name="capture_stderr_only",
        pattern="output=$(cmd 2>&1 1>/dev/null)",
        explanation="Capture only stderr into a variable by redirecting stdout to /dev/null first.",
    ),
    FdTrick(
        name="coproc_bidirectional",
        pattern="coproc myproc { cmd; }; echo input >&${myproc[1]}; read output <&${myproc[0]}",
        shells=(Shell.BASH,),
        explanation="Bash coproc creates a bidirectional pipe. Write to [1], read from [0]. Interactive IPC without named pipes.",
    ),
    FdTrick(
        name="fd_lock",
        pattern="exec 9>/tmp/lockfile; flock -n 9 || exit 1",
        shells=(Shell.BASH,),
        explanation="Advisory file locking using fd 9 and flock. Prevents concurrent script instances.",
    ),
    FdTrick(
        name="heredoc_fd",
        pattern="exec 3<<< 'inline data'; cat <&3",
        shells=(Shell.BASH, Shell.ZSH),
        explanation="Feed inline data through a file descriptor. Useful for passing data to commands that expect fd input.",
    ),
    FdTrick(
        name="named_pipe_ipc",
        pattern="mkfifo /tmp/pipe; cmd1 > /tmp/pipe & cmd2 < /tmp/pipe; rm /tmp/pipe",
        shells=(Shell.POSIX, Shell.BASH, Shell.ZSH, Shell.DASH),
        explanation="Named pipes (FIFOs) for IPC between unrelated processes. Works everywhere POSIX does.",
    ),
]


# ---------------------------------------------------------------------------
# Shlex & quoting deep knowledge
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class QuotingRule:
    name: str
    rule: str
    example: str
    pitfall: str = ""

QUOTING_RULES: list[QuotingRule] = [
    QuotingRule(
        name="single_vs_double",
        rule="Single quotes preserve everything literally. Double quotes allow $expansion and \\escaping.",
        example="""echo '$HOME' => $HOME (literal)  |  echo "$HOME" => /home/user (expanded)""",
    ),
    QuotingRule(
        name="nested_quoting",
        rule="You cannot nest single quotes. Use $'...' (ANSI-C) or alternate: \"it's\" or 'it'\\''s'",
        example="""echo 'it'\\''s a trap'  =>  it's a trap""",
        pitfall="Many people try 'it\\'s' — this does NOT work. The backslash is not special inside single quotes.",
    ),
    QuotingRule(
        name="word_splitting",
        rule="Unquoted $var undergoes word splitting and glob expansion. Always quote: \"$var\"",
        example="""f="my file.txt"; cat $f => error (splits into 'my' and 'file.txt'); cat "$f" => works""",
        pitfall="This is the #1 source of shell bugs. ShellCheck catches it. Always quote your variables.",
    ),
    QuotingRule(
        name="array_expansion",
        rule='"${arr[@]}" expands each element as a separate word. ${arr[*]} joins them into one word.',
        example="""arr=("a b" "c d"); printf '[%s]' "${arr[@]}" => [a b][c d]""",
        pitfall='${arr[@]} without quotes re-splits elements on whitespace — almost never what you want.',
    ),
    QuotingRule(
        name="shlex_split_in_python",
        rule="Python's shlex.split() mirrors Bash word-splitting. shlex.quote() safely escapes for shell injection.",
        example="""shlex.split("echo 'hello world' foo") => ['echo', 'hello world', 'foo']""",
        pitfall="shlex.split() does NOT handle Bash arrays, process substitution, or brace expansion.",
    ),
    QuotingRule(
        name="eval_danger",
        rule="eval re-parses a string as shell code. It's almost always the wrong tool — use arrays instead.",
        example="""cmd='ls -la "my dir"'; eval $cmd  # works but dangerous. Better: cmd=(ls -la "my dir"); "${cmd[@]}" """,
        pitfall="eval + user input = arbitrary code execution. This is how shell injection happens.",
    ),
]


# ---------------------------------------------------------------------------
# Cross-shell compatibility matrix
# ---------------------------------------------------------------------------

COMPAT_NOTES: dict[str, dict[str, str]] = {
    "process_substitution": {
        "bash": "<(cmd) and >(cmd) — fully supported",
        "zsh": "<(cmd) and >(cmd) — fully supported",
        "fish": "(cmd | psub) — different syntax",
        "dash": "NOT SUPPORTED — use named pipes or temp files",
        "posix": "NOT SUPPORTED — not in POSIX spec",
    },
    "arrays": {
        "bash": "arr=(a b c); ${arr[0]}",
        "zsh": "arr=(a b c); ${arr[1]} — 1-indexed!",
        "fish": "set arr a b c; $arr[1] — 1-indexed",
        "dash": "NOT SUPPORTED — use positional params: set -- a b c",
        "posix": "NOT SUPPORTED",
    },
    "pipefail": {
        "bash": "set -o pipefail — exit code is rightmost failure",
        "zsh": "set -o pipefail — same as bash",
        "fish": "built-in: $pipestatus array",
        "dash": "NOT SUPPORTED",
        "posix": "NOT SUPPORTED — POSIX only gives last command's exit code",
    },
    "brace_expansion": {
        "bash": "{a,b,c} and {1..10} — fully supported",
        "zsh": "{a,b,c} and {1..10} — fully supported",
        "fish": "NOT SUPPORTED — use (seq 1 10) or explicit lists",
        "dash": "NOT SUPPORTED",
        "posix": "NOT SUPPORTED — not in POSIX spec",
    },
}


def lookup_idioms(category: Optional[PipePattern] = None,
                  shell: Optional[Shell] = None) -> list[PipeIdiom]:
    """Query the corpus for matching pipe idioms."""
    results = PIPE_IDIOMS
    if category:
        results = [i for i in results if i.category == category]
    if shell:
        results = [i for i in results if i.for_shell(shell)]
    return results


def lookup_fd_tricks(shell: Optional[Shell] = None) -> list[FdTrick]:
    if shell:
        return [t for t in FD_TRICKS if shell in t.shells]
    return list(FD_TRICKS)
