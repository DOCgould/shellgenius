"""
Pipe Algebra — composing shell pipelines programmatically.

The key insight: a Unix pipeline is a directed acyclic graph of transforms.
Each stage has typed stdin/stdout (text lines, JSON, binary, TSV, etc.)
and the algebra ensures type-compatible composition.

ShellGenius uses this to:
1. Decompose user intent into atomic pipe stages
2. Select the best tool for each stage
3. Compose them with correct quoting and fd routing
4. Validate the pipeline before execution
"""

from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional


class StreamType(Enum):
    """What kind of data flows through the pipe."""
    TEXT_LINES = auto()    # newline-delimited text (the default)
    TSV = auto()           # tab-separated values
    CSV = auto()           # comma-separated values
    JSON = auto()          # JSON (object, array, or jsonlines)
    JSON_LINES = auto()    # one JSON object per line (ndjson)
    BINARY = auto()        # raw bytes (images, compressed, etc.)
    NULL_DELIM = auto()    # \0-delimited (find -print0 output)
    KEY_VALUE = auto()     # key=value pairs


@dataclass
class PipeStage:
    """A single stage in a pipeline."""
    command: str                         # the shell command (may include args)
    args: list[str] = field(default_factory=list)
    input_type: StreamType = StreamType.TEXT_LINES
    output_type: StreamType = StreamType.TEXT_LINES
    description: str = ""                # human-readable explanation
    safe: bool = True                    # False if command has side effects

    @property
    def full_command(self) -> str:
        if self.args:
            return f"{self.command} {' '.join(shlex.quote(a) for a in self.args)}"
        return self.command

    def __repr__(self) -> str:
        return f"Stage({self.full_command!r})"


class PipelineError(Exception):
    """Raised when pipeline composition is invalid."""


@dataclass
class Pipeline:
    """
    An ordered sequence of PipeStages with type-checked composition.

    Usage:
        p = Pipeline()
        p.add(PipeStage("find . -name '*.log' -print0", output_type=StreamType.NULL_DELIM))
        p.add(PipeStage("xargs -0 grep -l ERROR", input_type=StreamType.NULL_DELIM))
        p.add(PipeStage("wc -l"))
        print(p.render())  # => "find . -name '*.log' -print0 | xargs -0 grep -l ERROR | wc -l"
    """
    stages: list[PipeStage] = field(default_factory=list)
    description: str = ""

    def add(self, stage: PipeStage) -> "Pipeline":
        if self.stages:
            prev = self.stages[-1]
            if not self._types_compatible(prev.output_type, stage.input_type):
                raise PipelineError(
                    f"Type mismatch: {prev.full_command!r} outputs {prev.output_type.name} "
                    f"but {stage.full_command!r} expects {stage.input_type.name}. "
                    f"Insert a converter stage (e.g., jq -r '.[]' for JSON→TEXT)."
                )
        self.stages.append(stage)
        return self

    def render(self) -> str:
        """Render the pipeline as a shell command string."""
        return " | ".join(s.full_command for s in self.stages)

    def render_explained(self) -> str:
        """Render with inline comments explaining each stage."""
        lines = []
        for i, s in enumerate(self.stages):
            sep = "" if i == 0 else "  | "
            comment = f"  # {s.description}" if s.description else ""
            lines.append(f"{sep}{s.full_command}{comment}")
        return " \\\n".join(lines)

    @property
    def has_side_effects(self) -> bool:
        return any(not s.safe for s in self.stages)

    @property
    def output_type(self) -> Optional[StreamType]:
        return self.stages[-1].output_type if self.stages else None

    def validate(self) -> list[str]:
        """Return a list of warnings about this pipeline."""
        warnings = []
        for i, stage in enumerate(self.stages):
            # Check for common mistakes
            cmd_base = stage.command.split()[0] if stage.command else ""
            if cmd_base == "cat" and i == 0 and len(self.stages) > 1:
                warnings.append(
                    f"Stage 0: Useless Use of Cat (UUOC). "
                    f"Feed the file directly to the next command: "
                    f"< file {self.stages[1].full_command}"
                )
            if cmd_base == "grep" and "| grep" in self.render():
                # Multiple greps can often be combined
                grep_count = sum(1 for s in self.stages if s.command.split()[0] == "grep")
                if grep_count >= 3:
                    warnings.append(
                        f"Pipeline has {grep_count} grep stages. "
                        f"Consider combining with: grep -E 'pat1|pat2|pat3' "
                        f"or using awk for complex filtering."
                    )
            if cmd_base == "sort" and i > 0:
                prev_base = self.stages[i-1].command.split()[0]
                if prev_base == "sort":
                    warnings.append(f"Stage {i}: Double sort detected. This is likely redundant.")
        return warnings

    @staticmethod
    def _types_compatible(output: StreamType, input_: StreamType) -> bool:
        """Check if an output type can flow into an input type."""
        if input_ == StreamType.TEXT_LINES:
            # TEXT_LINES is the universal receiver (everything is bytes)
            return True
        if output == input_:
            return True
        # NULL_DELIM can flow into tools that understand it
        if output == StreamType.NULL_DELIM and input_ == StreamType.NULL_DELIM:
            return True
        # JSON_LINES is a subtype of TEXT_LINES and JSON
        if output == StreamType.JSON_LINES and input_ in (StreamType.JSON, StreamType.TEXT_LINES):
            return True
        return False


# ---------------------------------------------------------------------------
# Pipeline builder helpers — the "algebra" operators
# ---------------------------------------------------------------------------

def chain(*stages: PipeStage) -> Pipeline:
    """Compose stages left-to-right with type checking."""
    p = Pipeline()
    for s in stages:
        p.add(s)
    return p


def safely_quote_command(cmd: str) -> str:
    """
    Use shlex to safely parse and re-quote a command string.
    Catches injection attempts and malformed quoting.
    """
    try:
        tokens = shlex.split(cmd)
    except ValueError as e:
        raise PipelineError(f"Malformed command string: {e}") from e
    return " ".join(shlex.quote(t) for t in tokens)


def explain_pipeline(cmd_string: str) -> list[dict[str, str]]:
    """
    Break a raw pipeline string into explained stages.
    Returns a list of {command, explanation} dicts.
    """
    # Naive split on unquoted pipes — good enough for common cases
    import re
    # Split on | that isn't inside quotes
    stages_raw = _split_pipeline(cmd_string)
    explained = []
    for raw in stages_raw:
        raw = raw.strip()
        base = raw.split()[0] if raw else ""
        explained.append({
            "command": raw,
            "tool": base,
            "explanation": _explain_tool(base, raw),
        })
    return explained


def _split_pipeline(cmd: str) -> list[str]:
    """Split a command string on pipe characters, respecting quotes."""
    stages = []
    current = []
    in_single = False
    in_double = False
    i = 0
    while i < len(cmd):
        c = cmd[i]
        if c == "'" and not in_double:
            in_single = not in_single
        elif c == '"' and not in_single:
            in_double = not in_double
        elif c == '|' and not in_single and not in_double:
            # Check for || (logical OR) — don't split on that
            if i + 1 < len(cmd) and cmd[i + 1] == '|':
                current.append('||')
                i += 2
                continue
            stages.append(''.join(current))
            current = []
            i += 1
            continue
        current.append(c)
        i += 1
    if current:
        stages.append(''.join(current))
    return stages


_TOOL_EXPLANATIONS = {
    "grep": "Filter lines matching a pattern",
    "sed": "Stream editor — find and replace in-flight",
    "awk": "Pattern-action language for columnar data",
    "sort": "Sort lines (lexicographic by default, -n for numeric)",
    "uniq": "Deduplicate adjacent identical lines (sort first!)",
    "cut": "Extract fields/columns by delimiter",
    "tr": "Transliterate or delete characters",
    "head": "Take first N lines",
    "tail": "Take last N lines (or follow with -f)",
    "wc": "Count lines (-l), words (-w), or bytes (-c)",
    "tee": "Duplicate stream: one to file, one continues in pipe",
    "xargs": "Convert stdin lines into command arguments",
    "find": "Recursively search for files by criteria",
    "jq": "JSON processor — the awk of structured data",
    "cat": "Concatenate files (often useless at pipe start — see UUOC)",
    "parallel": "GNU parallel — run commands concurrently",
    "pv": "Pipe viewer — show throughput and progress",
    "comm": "Set operations on sorted files (intersection, difference)",
    "paste": "Merge lines from multiple inputs side-by-side",
    "join": "Database-style join on sorted files by key field",
    "column": "Format stdin into aligned columns",
    "rev": "Reverse each line character-by-character",
    "tac": "Reverse line order (cat backwards)",
    "nl": "Number lines",
    "fmt": "Reflow text to a given width",
    "fold": "Wrap lines to a given width (byte-level)",
    "expand": "Convert tabs to spaces",
    "unexpand": "Convert spaces to tabs",
    # MIME-routed dispatch
    "xdg-open": "Open file/URL with registered desktop app (MIME-routed: PNG→viewer, PDF→reader, URL→browser)",
    "gio": "GNOME I/O — open, trash, mount, copy with MIME dispatch (gio open = xdg-open)",
    "xdg-mime": "Query or set MIME type handlers (xdg-mime query default image/png)",
    # Container tools
    "toolbox": "Toolbox — run commands in a rich containerized dev environment",
    "podman": "Podman — rootless container engine (create, run, exec, stop, inspect)",
}


def _explain_tool(base: str, full_cmd: str) -> str:
    base_explanation = _TOOL_EXPLANATIONS.get(base, f"Run {base}")
    # Add flag-specific detail for common cases
    if base == "sort" and "-rn" in full_cmd:
        return f"{base_explanation} (descending numeric order)"
    if base == "sort" and "-n" in full_cmd:
        return f"{base_explanation} (numeric order)"
    if base == "grep" and "-v" in full_cmd:
        return "Invert match — exclude lines matching pattern"
    if base == "grep" and "-c" in full_cmd:
        return "Count matching lines"
    if base == "grep" and "-l" in full_cmd:
        return "List files containing matches"
    if base == "xargs" and "-P" in full_cmd:
        return f"{base_explanation} (parallel execution)"
    if base == "xargs" and "-0" in full_cmd:
        return f"{base_explanation} (null-delimited input)"
    return base_explanation
