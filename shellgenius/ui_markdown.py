"""
Markdown-to-Terminal renderer — makes LLM output readable in a shell.

The problem: LLMs return markdown (## headers, **bold**, ```code blocks```)
which looks like noise in a raw terminal. This module renders it with ANSI
escape codes so it's actually pleasant to read.

Zero dependencies. Uses only the ANSI primitives from shellgenius.ui.

Handles:
  # H1, ## H2, ### H3        → bold, colored, with spacing
  **bold**                    → ANSI bold
  *italic* / _italic_        → ANSI italic
  `inline code`              → highlighted/reversed
  ```code blocks```          → indented, dim border, syntax-colored
  - bullet lists             → clean indentation with marker
  1. numbered lists           → preserved numbering
  > blockquotes              → dim with bar
  --- / ***                  → horizontal rule
  [text](url)                → text (url in dim)
  plain paragraphs           → reflowed with left padding
"""

from __future__ import annotations

import re
from shellgenius.ui import (
    bold, dim, italic, red, green, yellow, blue, cyan, magenta,
    bold_cyan, bold_green, bold_yellow,
    _COLOR, SYM_PIPE,
)


# ---------------------------------------------------------------------------
# Inline formatting
# ---------------------------------------------------------------------------

def _render_inline(text: str) -> str:
    """Apply inline markdown formatting to a line of text."""
    if not _COLOR:
        # Strip markdown syntax for plain text
        text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
        text = re.sub(r'\*(.+?)\*', r'\1', text)
        text = re.sub(r'_(.+?)_', r'\1', text)
        text = re.sub(r'`(.+?)`', r'\1', text)
        text = re.sub(r'\[(.+?)\]\((.+?)\)', r'\1 (\2)', text)
        return text

    # Bold: **text** or __text__
    text = re.sub(r'\*\*(.+?)\*\*', lambda m: bold(m.group(1)), text)
    text = re.sub(r'__(.+?)__', lambda m: bold(m.group(1)), text)

    # Italic: *text* or _text_ (but not inside words like file_name)
    text = re.sub(r'(?<!\w)\*(.+?)\*(?!\w)', lambda m: italic(m.group(1)), text)
    text = re.sub(r'(?<!\w)_(.+?)_(?!\w)', lambda m: italic(m.group(1)), text)

    # Inline code: `text`
    text = re.sub(r'`(.+?)`', lambda m: _inline_code(m.group(1)), text)

    # Links: [text](url)
    text = re.sub(r'\[(.+?)\]\((.+?)\)', lambda m: f"{m.group(1)} {dim('(' + m.group(2) + ')')}", text)

    return text


def _inline_code(text: str) -> str:
    """Render inline code with background highlight."""
    if not _COLOR:
        return f"`{text}`"
    # Use reversed video for inline code — works on any terminal
    return f"\033[7m {text} \033[0m"


# ---------------------------------------------------------------------------
# Code block rendering
# ---------------------------------------------------------------------------

def _render_code_block(lines: list[str], lang: str = "") -> list[str]:
    """Render a fenced code block with border and optional syntax hints."""
    out = []
    # Top border
    lang_label = f" {dim(lang)}" if lang else ""
    out.append(f"  {dim('┌──')}{lang_label}")

    for line in lines:
        # Basic syntax coloring for shell code
        colored = _syntax_color(line, lang) if _COLOR else line
        out.append(f"  {dim('│')} {colored}")

    # Bottom border
    out.append(f"  {dim('└──')}")
    return out


def _syntax_color(line: str, lang: str) -> str:
    """Minimal syntax highlighting for shell code."""
    if lang not in ("bash", "sh", "shell", "zsh", ""):
        return line

    # Comments
    if re.match(r'\s*#', line):
        return dim(line)

    # Strings (simple — just color quoted sections)
    line = re.sub(r'(".*?")', lambda m: green(m.group(1)), line)
    line = re.sub(r"('.*?')", lambda m: green(m.group(1)), line)

    # Variables
    line = re.sub(r'(\$\w+|\$\{[^}]+\})', lambda m: cyan(m.group(1)), line)

    # Pipe operators
    line = re.sub(r'(\s\|\s)', lambda m: bold_yellow(m.group(1)), line)

    # Command at start of line (first word)
    line = re.sub(r'^(\s*)([\w./-]+)', lambda m: m.group(1) + bold(m.group(2)), line, count=1)

    return line


# ---------------------------------------------------------------------------
# Block-level rendering
# ---------------------------------------------------------------------------

def render_markdown(text: str, *, indent: int = 2) -> str:
    """
    Render a markdown string as ANSI-formatted terminal output.

    Args:
        text: Raw markdown from the LLM.
        indent: Left padding (spaces).

    Returns:
        ANSI-formatted string ready for print().
    """
    pad = " " * indent
    lines = text.split("\n")
    output: list[str] = []
    i = 0

    while i < len(lines):
        line = lines[i]

        # --- Fenced code blocks ---
        if line.strip().startswith("```"):
            lang = line.strip().removeprefix("```").strip()
            code_lines = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code_lines.append(lines[i])
                i += 1
            i += 1  # skip closing ```
            rendered = _render_code_block(code_lines, lang)
            output.append("")  # spacing before
            output.extend(f"{pad}{l}" for l in rendered)
            output.append("")  # spacing after
            continue

        # --- Headers ---
        h_match = re.match(r'^(#{1,6})\s+(.*)', line)
        if h_match:
            level = len(h_match.group(1))
            title = h_match.group(2)
            output.append("")
            if level == 1:
                output.append(f"{pad}{bold_cyan(title.upper())}")
            elif level == 2:
                output.append(f"{pad}{bold(title)}")
            elif level == 3:
                output.append(f"{pad}{bold(title)}")
            else:
                output.append(f"{pad}{bold(title)}")
            i += 1
            continue

        # --- Horizontal rules ---
        if re.match(r'^\s*(---+|\*\*\*+|___+)\s*$', line):
            rule = "─" * 50 if _COLOR else "-" * 50
            output.append(f"{pad}{dim(rule)}")
            i += 1
            continue

        # --- Blockquotes ---
        if line.strip().startswith(">"):
            quote_text = line.strip().removeprefix(">").strip()
            bar = dim("▎") if _COLOR else "|"
            output.append(f"{pad}  {bar} {dim(_render_inline(quote_text))}")
            i += 1
            continue

        # --- Unordered lists ---
        list_match = re.match(r'^(\s*)[*\-+]\s+(.*)', line)
        if list_match:
            list_indent = len(list_match.group(1))
            content = list_match.group(2)
            marker = dim("•") if _COLOR else "-"
            extra_pad = " " * list_indent
            output.append(f"{pad}  {extra_pad}{marker} {_render_inline(content)}")
            i += 1
            continue

        # --- Ordered lists ---
        olist_match = re.match(r'^(\s*)(\d+)\.\s+(.*)', line)
        if olist_match:
            list_indent = len(olist_match.group(1))
            num = olist_match.group(2)
            content = olist_match.group(3)
            extra_pad = " " * list_indent
            output.append(f"{pad}  {extra_pad}{dim(num + '.')} {_render_inline(content)}")
            i += 1
            continue

        # --- Empty lines (paragraph breaks) ---
        if not line.strip():
            output.append("")
            i += 1
            continue

        # --- Regular paragraphs ---
        output.append(f"{pad}{_render_inline(line)}")
        i += 1

    return "\n".join(output)


# ---------------------------------------------------------------------------
# Quick test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    sample = """\
# File Descriptor Swapping

To swap `stdout` and `stderr` in **bash**, use the three-way swap pattern:

```bash
# The classic fd swap
cmd 3>&1 1>&2 2>&3 3>&-
```

### How it works

1. **`3>&1`**: Save stdout to fd 3
2. **`1>&2`**: Redirect stdout to stderr
3. **`2>&3`**: Redirect stderr to the saved stdout
4. **`3>&-`**: Close the temporary fd

> Per TLPI Ch.5, dup2() atomically closes the target fd and copies the source.

---

### Gotchas

- Don't forget the `3>&-` cleanup — leaked fds are a *common* bug
- This only works in **bash** and **zsh**, not POSIX sh
- For a simpler approach, try: `{ cmd | filter; } 2>&1 1>&3 | error_handler`

See also: [TLPI Ch.5](https://man7.org/tlpi/) for the full fd semantics.
"""
    print(render_markdown(sample))
