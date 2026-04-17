"""
Knowledge Ingestion — ingest arbitrary documents into ShellGenius's ScaNN index.

Supports:
- PDF files
- Plain text / markdown files
- Directories (recursively scans for supported files)
- Code files (with language-aware chunking)

The ingestion system:
1. Scans the target path for supported files
2. Extracts text and chunks it
3. Embeds chunks with sentence-transformers
4. Saves a ScaNN index + compressed metadata to /usr/share/embeddings/<source-name>/
5. Writes a pointer in the global index registry so ShellGenius can find it later

Embeddings are grouped by source name under a shared directory:
    /usr/share/embeddings/
    ├── my-project/
    │   ├── index.scann/          ← ScaNN vector index (directory)
    │   ├── chunks.json.gz        ← gzip-compressed chunk text + metadata
    │   ├── manifest.json         ← what was ingested, when, stats
    │   └── .shellgenius-index    ← marker file (for discovery)
    └── linux-manual/
        └── ...

Falls back to ~/.local/share/shellgenius/embeddings/ if /usr/share/embeddings
is not writable.

Global registry:
    ~/.shellgenius/indices.json   ← pointers to all embedding directories
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .vector_index import VectorIndex, build as build_index, is_scann_index

_model = None


def _get_model():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        _model = SentenceTransformer("all-MiniLM-L6-v2")
    return _model


# ---------------------------------------------------------------------------
# Supported file types
# ---------------------------------------------------------------------------

SUPPORTED_EXTENSIONS = {
    # Documents
    ".pdf", ".txt", ".md", ".rst", ".org",
    # Code
    ".py", ".sh", ".bash", ".zsh", ".fish",
    ".js", ".ts", ".go", ".rs", ".c", ".h", ".cpp", ".hpp", ".cc", ".cxx", ".hh",
    ".java", ".rb", ".lua", ".pl", ".awk",
    # HDL / SystemVerilog (for Verilator / hardware co-sim corpora)
    ".v", ".sv", ".svh", ".vh",
    # Config
    ".yaml", ".yml", ".toml", ".json", ".ini", ".conf",
    ".dockerfile", ".Makefile",
    # Shell-specific
    ".bashrc", ".zshrc", ".profile", ".bash_profile",
}

# Man page extensions (section.gz or just section number)
MANPAGE_EXTENSIONS = {
    f".{s}.gz" for s in range(1, 9)
} | {
    f".{s}" for s in range(1, 9)
}

# Man page section descriptions
MANPAGE_SECTIONS = {
    "1": "user commands",
    "2": "system calls",
    "3": "library functions",
    "4": "special files",
    "5": "file formats",
    "6": "games",
    "7": "miscellaneous",
    "8": "system administration",
}

# Files to always skip
SKIP_PATTERNS = {
    ".git", "__pycache__", "node_modules", ".venv", "venv",
    ".embeddings", ".mypy_cache", ".pytest_cache",
    "dist", "build", ".egg-info",
}

# Default embedding storage locations
DEFAULT_EMBED_ROOT_SYSTEM = Path("/usr/share/embeddings")
DEFAULT_EMBED_ROOT_USER = Path.home() / ".local" / "share" / "shellgenius" / "embeddings"


def _resolve_embed_root() -> Path:
    """Resolve the embedding root directory, falling back to user dir if system dir is not writable."""
    try:
        DEFAULT_EMBED_ROOT_SYSTEM.mkdir(parents=True, exist_ok=True)
        # Verify writable by checking permissions
        if os.access(DEFAULT_EMBED_ROOT_SYSTEM, os.W_OK):
            return DEFAULT_EMBED_ROOT_SYSTEM
    except (PermissionError, OSError):
        pass
    DEFAULT_EMBED_ROOT_USER.mkdir(parents=True, exist_ok=True)
    return DEFAULT_EMBED_ROOT_USER


def _sanitize_source_name(target: Path, embed_root: Path) -> str:
    """Derive a grouped directory name from the source path."""
    name = target.stem if target.is_file() else target.name
    # Lowercase, replace non-alphanum with hyphens, collapse, strip
    name = re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')
    if not name:
        name = "index"
    # Truncate to 64 chars
    name = name[:64]
    # Check for collision: existing dir with different source
    candidate = embed_root / name
    if candidate.exists():
        manifest_path = candidate / "manifest.json"
        if manifest_path.exists():
            try:
                existing = json.loads(manifest_path.read_text())
                if existing.get("source") != str(target):
                    # Collision — append short hash
                    h = hashlib.sha256(str(target).encode()).hexdigest()[:6]
                    name = f"{name[:57]}-{h}"
            except (json.JSONDecodeError, OSError):
                pass
    return name


# ---------------------------------------------------------------------------
# Chunk types
# ---------------------------------------------------------------------------

@dataclass
class IngestChunk:
    text: str
    source_file: str       # relative path from ingest root
    source_type: str       # "pdf", "markdown", "code", "text"
    chunk_id: int = 0
    page: int = 0          # for PDFs
    line_start: int = 0    # for code/text
    line_end: int = 0
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "source_file": self.source_file,
            "source_type": self.source_type,
            "chunk_id": self.chunk_id,
            "page": self.page,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "tags": self.tags,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "IngestChunk":
        return cls(**d)


# ---------------------------------------------------------------------------
# Text extraction per file type
# ---------------------------------------------------------------------------

def _extract_pdf(path: Path, rel_path: str) -> list[IngestChunk]:
    """Extract text from a PDF file."""
    try:
        import fitz
    except ImportError:
        return []

    chunks = []
    doc = fitz.open(str(path))
    for page_num in range(len(doc)):
        try:
            page = doc[page_num]
            text = page.get_text()
            if text.strip():
                for chunk_text in _split_text(text):
                    chunks.append(IngestChunk(
                        text=chunk_text,
                        source_file=rel_path,
                        source_type="pdf",
                        page=page_num + 1,
                    ))
        except Exception:
            continue
    doc.close()
    return chunks


def _extract_text(path: Path, rel_path: str, source_type: str = "text") -> list[IngestChunk]:
    """Extract text from a plain text or markdown file."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    chunks = []
    sections = re.split(r'\n#{1,3}\s+', text)  # split on markdown headers
    if len(sections) <= 1:
        sections = _split_text(text)

    line_offset = 0
    for section in sections:
        section = section.strip()
        if len(section) < 30:
            continue
        for chunk_text in _split_text(section):
            line_count = chunk_text.count('\n')
            chunks.append(IngestChunk(
                text=chunk_text,
                source_file=rel_path,
                source_type=source_type,
                line_start=line_offset,
                line_end=line_offset + line_count,
            ))
            line_offset += line_count

    return chunks


def _extract_code(path: Path, rel_path: str) -> list[IngestChunk]:
    """Extract text from a code file, splitting on function/class boundaries."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    ext = path.suffix.lower()
    lang_tag = {
        ".py": "python", ".sh": "bash", ".bash": "bash", ".zsh": "zsh",
        ".js": "javascript", ".ts": "typescript", ".go": "go", ".rs": "rust",
        ".c": "c", ".h": "c", ".cpp": "cpp", ".hpp": "cpp",
        ".cc": "cpp", ".cxx": "cpp", ".hh": "cpp",
        ".java": "java", ".rb": "ruby",
        ".v": "verilog", ".sv": "systemverilog", ".svh": "systemverilog", ".vh": "verilog",
    }.get(ext, "code")

    # Split on function/class definitions (language-aware)
    if ext in (".py",):
        splitter = r'\n(?=(?:def |class |async def ))'
    elif ext in (".sh", ".bash", ".zsh"):
        splitter = r'\n(?=(?:\w+\s*\(\)|function\s+\w+))'
    elif ext in (".go",):
        splitter = r'\n(?=func\s)'
    elif ext in (".rs",):
        splitter = r'\n(?=(?:pub\s+)?fn\s)'
    elif ext in (".js", ".ts"):
        splitter = r'\n(?=(?:export\s+)?(?:async\s+)?(?:function|class|const\s+\w+\s*=))'
    else:
        splitter = None

    if splitter:
        sections = re.split(splitter, text)
    else:
        sections = _split_text(text)

    chunks = []
    line_offset = 0
    for section in sections:
        section = section.strip()
        if len(section) < 20:
            continue
        # Don't over-chunk code — keep functions together up to ~800 words
        for chunk_text in _split_text(section, chunk_size=800):
            line_count = chunk_text.count('\n')
            chunks.append(IngestChunk(
                text=chunk_text,
                source_file=rel_path,
                source_type="code",
                line_start=line_offset,
                line_end=line_offset + line_count,
                tags=[lang_tag],
            ))
            line_offset += line_count

    return chunks


def _extract_manpage(path: Path, rel_path: str) -> list[IngestChunk]:
    """
    Extract text from a man page (troff format, possibly gzipped).

    Renders the man page to plain text via `man -l` (local file) or `groff`,
    then splits on section headers (NAME, SYNOPSIS, DESCRIPTION, OPTIONS, etc.)
    for semantically meaningful chunks.
    """
    import subprocess

    # Determine the man page name and section from the filename
    # e.g., bash.1.gz -> name=bash, section=1
    name_parts = path.name.split(".")
    page_name = name_parts[0] if name_parts else path.stem
    section = ""
    for part in name_parts[1:]:
        if part.isdigit() and len(part) == 1:
            section = part
            break

    section_desc = MANPAGE_SECTIONS.get(section, "")

    # Render to plain text
    text = None

    # Method 1: man -l (render local file)
    try:
        result = subprocess.run(
            ["man", "-l", str(path)],
            capture_output=True, timeout=15,
            env={**os.environ, "MANWIDTH": "120", "COLUMNS": "120", "MAN_KEEP_FORMATTING": "0"},
        )
        if result.returncode == 0:
            text = result.stdout.decode("utf-8", errors="replace")
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass

    # Method 2: zcat | groff (for gzipped troff)
    if text is None and str(path).endswith(".gz"):
        try:
            result = subprocess.run(
                f"zcat {shlex.quote(str(path))} | groff -mandoc -Tutf8 2>/dev/null | col -bx",
                shell=True, capture_output=True, timeout=15,
            )
            if result.returncode == 0:
                text = result.stdout.decode("utf-8", errors="replace")
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            pass

    # Method 3: man <name> (fall back to system man)
    if text is None:
        try:
            cmd = ["man", section, page_name] if section else ["man", page_name]
            result = subprocess.run(
                cmd, capture_output=True, timeout=15,
                env={**os.environ, "MANWIDTH": "120", "COLUMNS": "120"},
            )
            if result.returncode == 0:
                text = result.stdout.decode("utf-8", errors="replace")
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            pass

    if not text or len(text.strip()) < 50:
        return []

    # Clean up: strip backspace-based bold/underline artifacts and control chars
    text = re.sub(r'.\x08', '', text)           # backspace overstrikes
    text = re.sub(r'\x1b\[[0-9;]*m', '', text)  # ANSI escape sequences
    text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', text)  # control chars

    # Split on man page section headers (lines that are ALL CAPS at the left margin)
    sections = re.split(r'\n(?=[A-Z][A-Z ]{2,}$)', text, flags=re.MULTILINE)

    chunks = []
    for sec in sections:
        sec = sec.strip()
        if len(sec) < 30:
            continue

        # Extract section header for tagging
        header_match = re.match(r'^([A-Z][A-Z ]+)', sec)
        section_header = header_match.group(1).strip().lower() if header_match else ""

        # Auto-tag based on section header and content
        tags = [f"man{section}"] if section else ["man"]
        if section_desc:
            tags.append(section_desc.replace(" ", "_"))
        if section_header:
            tags.append(f"section:{section_header}")

        # Chunk the section (man page sections can be very long, e.g. bash DESCRIPTION)
        for chunk_text in _split_text(sec, chunk_size=512):
            chunks.append(IngestChunk(
                text=f"[man {page_name}({section})] {chunk_text}" if section else chunk_text,
                source_file=rel_path,
                source_type="manpage",
                tags=tags,
            ))

    return chunks


def _split_text(text: str, chunk_size: int = 512, overlap: int = 64) -> list[str]:
    """Split text into chunks of ~chunk_size words with overlap."""
    text = re.sub(r'\n{3,}', '\n\n', text)
    words = text.split()
    if len(words) <= chunk_size:
        return [text.strip()] if text.strip() else []

    chunks = []
    start = 0
    while start < len(words):
        end = start + chunk_size
        chunk = " ".join(words[start:end])
        if chunk.strip():
            chunks.append(chunk.strip())
        start = end - overlap

    return chunks


# ---------------------------------------------------------------------------
# Scan and ingest
# ---------------------------------------------------------------------------

def _is_manpage(path: Path) -> bool:
    """Check if a file looks like a man page (e.g., bash.1.gz, pipe.2)."""
    name = path.name
    # Match patterns like: name.N.gz or name.N
    return bool(re.match(r'^.+\.[1-8](\.gz)?$', name))


def scan_directory(root: Path) -> list[Path]:
    """Recursively find all supported files in a directory."""
    files = []
    for dirpath, dirnames, filenames in os.walk(root):
        # Skip ignored directories
        dirnames[:] = [d for d in dirnames if d not in SKIP_PATTERNS]
        for fname in filenames:
            fpath = Path(dirpath) / fname
            if (fpath.suffix.lower() in SUPPORTED_EXTENSIONS
                    or fname in (".bashrc", ".zshrc", ".profile", "Makefile", "Dockerfile")
                    or _is_manpage(fpath)):
                files.append(fpath)
    return sorted(files)


def ingest(
    target: str | Path,
    *,
    output_dir: Optional[str | Path] = None,
    show_progress: bool = True,
) -> dict:
    """
    Ingest a file or directory into a ScaNN vector index.

    Stores embeddings in /usr/share/embeddings/<source-name>/ by default,
    grouped by source name. Falls back to ~/.local/share/shellgenius/embeddings/
    if the system directory is not writable.

    Args:
        target: File or directory path to ingest.
        output_dir: Custom output directory (overrides default /usr/share/embeddings/).
        show_progress: Print progress.

    Returns:
        Stats dict with chunk count, file count, index path, embed_dir.
    """
    import numpy as np
    model = _get_model()

    target = Path(target).resolve()

    if target.is_file():
        files = [target]
        root = target.parent
    elif target.is_dir():
        files = scan_directory(target)
        root = target
    else:
        raise FileNotFoundError(f"Not found: {target}")

    if not files:
        raise ValueError(f"No supported files found in {target}")

    # Determine output location
    if output_dir:
        embed_dir = Path(output_dir).resolve()
    else:
        embed_root = _resolve_embed_root()
        group_name = _sanitize_source_name(target, embed_root)
        embed_dir = embed_root / group_name

    if show_progress:
        print(f"Scanning {target}...")
        print(f"  Found {len(files)} files")

    # Extract chunks from all files
    all_chunks: list[IngestChunk] = []
    for fpath in files:
        rel = str(fpath.relative_to(root))
        ext = fpath.suffix.lower()

        if ext == ".pdf":
            chunks = _extract_pdf(fpath, rel)
        elif _is_manpage(fpath):
            chunks = _extract_manpage(fpath, rel)
        elif ext in (".md", ".rst", ".org"):
            chunks = _extract_text(fpath, rel, "markdown")
        elif ext in (".py", ".sh", ".bash", ".zsh", ".fish", ".js", ".ts",
                      ".go", ".rs", ".c", ".h", ".cpp", ".hpp", ".java",
                      ".rb", ".lua", ".pl", ".awk"):
            chunks = _extract_code(fpath, rel)
        else:
            chunks = _extract_text(fpath, rel)

        if show_progress and chunks:
            print(f"  {rel}: {len(chunks)} chunks")

        all_chunks.extend(chunks)

    if not all_chunks:
        raise ValueError("No text extracted from any files")

    # Assign chunk IDs
    for i, chunk in enumerate(all_chunks):
        chunk.chunk_id = i

    if show_progress:
        print(f"\nEmbedding {len(all_chunks)} chunks...")

    # Embed
    texts = [c.text for c in all_chunks]
    embeddings = model.encode(
        texts,
        normalize_embeddings=True,
        show_progress_bar=show_progress,
        batch_size=64,
    )
    embeddings = np.array(embeddings, dtype=np.float32)

    # Build ScaNN index (adaptive: brute-force for small, tree+AH for large)
    dim = embeddings.shape[1]
    index = build_index(embeddings)
    if show_progress:
        print(f"  Index strategy: {index.strategy} ({index.ntotal} vectors, dim={dim})")

    # Save
    embed_dir.mkdir(parents=True, exist_ok=True)
    index.save(embed_dir / "index.scann")
    with gzip.open(embed_dir / "chunks.json.gz", "wt", encoding="utf-8") as f:
        json.dump([c.to_dict() for c in all_chunks], f)

    # Write manifest
    manifest = {
        "source": str(target),
        "embed_dir": str(embed_dir),
        "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "files_ingested": len(files),
        "total_chunks": len(all_chunks),
        "embedding_dim": dim,
        "model": "all-MiniLM-L6-v2",
        "file_types": list(set(c.source_type for c in all_chunks)),
    }
    (embed_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))

    # Marker file for discovery
    (embed_dir / ".shellgenius-index").write_text(
        json.dumps({"source": str(target), "created": manifest["created"]})
    )

    # Register in global index
    _register_index(str(embed_dir), manifest)

    if show_progress:
        print(f"\nSaved to {embed_dir}/")
        print(f"  Chunks: {len(all_chunks)}")
        print(f"  Files: {len(files)}")
        print(f"  Index: {embed_dir / 'index.scann'}")

    return manifest


# ---------------------------------------------------------------------------
# Global index registry — pointers like CLAUDE.md
# ---------------------------------------------------------------------------

REGISTRY_DIR = Path.home() / ".shellgenius"
REGISTRY_FILE = REGISTRY_DIR / "indices.json"


def _load_registry() -> dict:
    if REGISTRY_FILE.exists():
        try:
            return json.loads(REGISTRY_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {"indices": []}


def _save_registry(registry: dict):
    REGISTRY_DIR.mkdir(parents=True, exist_ok=True)
    REGISTRY_FILE.write_text(json.dumps(registry, indent=2))


def _register_index(embed_dir: str, manifest: dict):
    """Register a .embeddings directory in the global index."""
    registry = _load_registry()

    # Update or add
    existing = [i for i in registry["indices"] if i["path"] == embed_dir]
    if existing:
        existing[0].update({
            "source": manifest["source"],
            "chunks": manifest["total_chunks"],
            "updated": manifest["created"],
        })
    else:
        registry["indices"].append({
            "path": embed_dir,
            "source": manifest["source"],
            "chunks": manifest["total_chunks"],
            "created": manifest["created"],
        })

    _save_registry(registry)


def list_indices() -> list[dict]:
    """List all registered vector indices."""
    registry = _load_registry()
    # Verify each still exists
    valid = []
    for entry in registry["indices"]:
        path = Path(entry["path"])
        if is_scann_index(path / "index.scann"):
            entry["status"] = "ok"
            valid.append(entry)
        else:
            entry["status"] = "missing"
            valid.append(entry)
    return valid


def load_index(embed_dir: str | Path) -> tuple:
    """Load a ScaNN index and its chunks. Supports both gzipped and plain chunk files."""
    embed_dir = Path(embed_dir)

    index = VectorIndex.load(embed_dir / "index.scann")

    gz_path = embed_dir / "chunks.json.gz"
    plain_path = embed_dir / "chunks.json"
    if gz_path.exists():
        with gzip.open(gz_path, "rt", encoding="utf-8") as f:
            chunks_data = json.load(f)
    elif plain_path.exists():
        chunks_data = json.loads(plain_path.read_text())
    else:
        raise FileNotFoundError(f"No chunks file found in {embed_dir}")

    chunks = [IngestChunk.from_dict(d) for d in chunks_data]
    return index, chunks


def query_index(
    embed_dir: str | Path,
    question: str,
    *,
    top_k: int = 5,
) -> list[tuple[IngestChunk, float]]:
    """Query a specific ScaNN index."""
    import numpy as np
    model = _get_model()

    index, chunks = load_index(embed_dir)
    query_vec = model.encode([question], normalize_embeddings=True)
    query_vec = np.array(query_vec, dtype=np.float32)

    distances, indices = index.search(query_vec, min(top_k, len(chunks)))

    results = []
    for dist, idx in zip(distances[0], indices[0]):
        if 0 <= idx < len(chunks):
            results.append((chunks[idx], float(dist)))
    return results


def query_all_indices(
    question: str,
    *,
    top_k: int = 5,
) -> list[tuple[IngestChunk, float, str]]:
    """Query ALL registered indices and merge results."""
    all_results = []
    for entry in list_indices():
        if entry.get("status") != "ok":
            continue
        try:
            results = query_index(entry["path"], question, top_k=top_k)
            for chunk, score in results:
                all_results.append((chunk, score, entry["source"]))
        except Exception:
            continue

    # Sort by score descending, take top_k
    all_results.sort(key=lambda x: -x[1])
    return all_results[:top_k]


# ---------------------------------------------------------------------------
# Man page bulk ingestion
# ---------------------------------------------------------------------------

def ingest_manpages(
    pages: Optional[list[str]] = None,
    *,
    sections: Optional[list[str]] = None,
    output_dir: Optional[str | Path] = None,
    show_progress: bool = True,
) -> dict:
    """
    Ingest man pages into the knowledge base.

    Args:
        pages: Specific page names to ingest (e.g., ["bash", "grep", "pipe", "fork"]).
               If None, ingests all pages from the specified sections.
        sections: Man sections to ingest (e.g., ["1", "2"]). Defaults to ["1", "2", "3"].
        output_dir: Custom output directory. Defaults to centralized embedding store.
        show_progress: Print progress.

    Returns:
        Stats dict with chunk count, page count, etc.

    Examples:
        ingest_manpages(["bash", "grep", "awk", "sed", "find", "xargs", "pipe", "fork", "exec", "socket"])
        ingest_manpages(sections=["2"])  # all syscalls
        ingest_manpages(sections=["1", "2", "3", "7"])  # commands + syscalls + libs + misc
    """
    import subprocess

    if sections is None:
        sections = ["1", "2", "3"]

    man_dirs = []
    for sec in sections:
        d = Path(f"/usr/share/man/man{sec}")
        if d.is_dir():
            man_dirs.append(d)

    if not man_dirs:
        raise FileNotFoundError("No man directories found in /usr/share/man/")

    # Collect man page files
    files = []
    for d in man_dirs:
        for f in sorted(d.iterdir()):
            if not _is_manpage(f):
                continue
            if pages is not None:
                # Filter: only include requested pages
                page_name = f.name.split(".")[0]
                if page_name not in pages:
                    continue
            files.append(f)

    if not files:
        raise ValueError(f"No man pages found matching criteria (sections={sections}, pages={pages})")

    if show_progress:
        print(f"Found {len(files)} man pages across sections {', '.join(sections)}")

    # Determine output location
    if output_dir:
        embed_dir = Path(output_dir).resolve()
    else:
        embed_root = _resolve_embed_root()
        if pages:
            group_name = f"manpages-{'-'.join(sorted(pages)[:5])}"
            if len(pages) > 5:
                group_name += f"-and-{len(pages)-5}-more"
        else:
            group_name = f"manpages-section-{'-'.join(sections)}"
        group_name = group_name[:64]
        embed_dir = embed_root / group_name

    # Extract chunks
    import numpy as np
    model = _get_model()

    all_chunks: list[IngestChunk] = []
    failed = 0
    for fpath in files:
        rel = fpath.name
        chunks = _extract_manpage(fpath, rel)
        if chunks:
            if show_progress:
                print(f"  {rel}: {len(chunks)} chunks")
            all_chunks.extend(chunks)
        else:
            failed += 1

    if not all_chunks:
        raise ValueError("No text extracted from any man pages")

    for i, chunk in enumerate(all_chunks):
        chunk.chunk_id = i

    if show_progress:
        print(f"\nExtracted {len(all_chunks)} chunks from {len(files) - failed} pages ({failed} failed)")
        print(f"Embedding {len(all_chunks)} chunks...")

    # Embed
    texts = [c.text for c in all_chunks]
    embeddings = model.encode(texts, normalize_embeddings=True, show_progress_bar=show_progress, batch_size=64)
    embeddings = np.array(embeddings, dtype=np.float32)

    # Build ScaNN index
    dim = embeddings.shape[1]
    index = build_index(embeddings)
    if show_progress:
        print(f"  Index strategy: {index.strategy} ({index.ntotal} vectors, dim={dim})")

    # Save
    embed_dir.mkdir(parents=True, exist_ok=True)
    index.save(embed_dir / "index.scann")
    with gzip.open(embed_dir / "chunks.json.gz", "wt", encoding="utf-8") as f:
        json.dump([c.to_dict() for c in all_chunks], f)

    source_desc = f"manpages(sections={sections}" + (f", pages={pages})" if pages else ")")
    manifest = {
        "source": source_desc,
        "embed_dir": str(embed_dir),
        "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "files_ingested": len(files) - failed,
        "total_chunks": len(all_chunks),
        "embedding_dim": dim,
        "model": "all-MiniLM-L6-v2",
        "file_types": ["manpage"],
        "sections": sections,
        "pages": pages,
    }
    (embed_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    (embed_dir / ".shellgenius-index").write_text(
        json.dumps({"source": source_desc, "created": manifest["created"]})
    )
    _register_index(str(embed_dir), manifest)

    if show_progress:
        print(f"\nSaved to {embed_dir}/")
        print(f"  Chunks: {len(all_chunks)}")
        print(f"  Pages: {len(files) - failed}")
        print(f"  Index: {embed_dir / 'index.scann'}")

    return manifest


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage:")
        print("  python -m shellgenius.knowledge.ingest <path>           # ingest a file or directory")
        print("  python -m shellgenius.knowledge.ingest --man <pages>    # ingest specific man pages")
        print("  python -m shellgenius.knowledge.ingest --man-section 2  # ingest all man pages in section")
        print("  python -m shellgenius.knowledge.ingest --list           # list all indices")
        print("  python -m shellgenius.knowledge.ingest --query <text>   # query all indices")
        sys.exit(0)

    if sys.argv[1] == "--list":
        indices = list_indices()
        if not indices:
            print("No indices registered.")
        for idx in indices:
            status = idx.get("status", "?")
            sym = "ok" if status == "ok" else "!!"
            print(f"  [{sym}] {idx['source']}")
            print(f"       {idx['chunks']} chunks @ {idx['path']}")
        sys.exit(0)

    if sys.argv[1] == "--query" and len(sys.argv) > 2:
        question = " ".join(sys.argv[2:])
        results = query_all_indices(question, top_k=5)
        if not results:
            print("No results.")
        for chunk, score, source in results:
            print(f"  [{score:.3f}] {chunk.source_file}:{chunk.line_start} ({source})")
            print(f"          {chunk.text[:120]}...")
        sys.exit(0)

    if sys.argv[1] == "--man":
        # Ingest specific man pages: --man bash grep awk pipe fork exec
        pages = sys.argv[2:] if len(sys.argv) > 2 else None
        if not pages:
            print("Usage: --man page1 page2 ...  (e.g., --man bash grep pipe fork)")
            sys.exit(1)
        ingest_manpages(pages=pages)
        sys.exit(0)

    if sys.argv[1] == "--man-section":
        # Ingest all pages in a section: --man-section 2
        sections = sys.argv[2:] if len(sys.argv) > 2 else ["1", "2", "3"]
        ingest_manpages(sections=sections)
        sys.exit(0)

    if sys.argv[1] == "--man-shell":
        # Curated set of shell-relevant man pages
        shell_pages = [
            # Core shell
            "bash", "zsh", "dash", "sh",
            # Text processing
            "grep", "sed", "awk", "cut", "tr", "sort", "uniq", "wc", "head", "tail",
            "tee", "paste", "join", "comm", "column", "fmt", "fold", "nl", "rev", "tac",
            # File operations
            "find", "xargs", "ls", "cp", "mv", "rm", "mkdir", "chmod", "chown", "ln", "stat",
            "file", "diff", "patch", "tar", "gzip", "bzip2",
            # Process & IPC
            "kill", "ps", "top", "nice", "nohup", "timeout", "wait", "jobs",
            "mkfifo", "flock",
            # System calls (section 2)
            "pipe", "fork", "exec", "execve", "wait4", "waitpid",
            "dup", "dup2", "fcntl", "open", "close", "read", "write",
            "socket", "bind", "listen", "accept", "connect", "send", "recv",
            "select", "poll", "epoll_create", "epoll_ctl", "epoll_wait",
            "signal", "sigaction", "kill_2", "mmap", "munmap",
            "clone", "unshare", "setns", "pivot_root",
            # Networking
            "curl", "wget", "ssh", "nc", "socat",
            # Modern tools
            "jq", "parallel", "tmux", "screen",
            # Container/system
            "podman", "toolbox", "systemctl", "journalctl",
        ]
        ingest_manpages(pages=shell_pages, sections=["1", "2", "3", "7", "8"])
        sys.exit(0)

    # Ingest file/directory
    target = sys.argv[1]
    output = sys.argv[2] if len(sys.argv) > 2 else None
    ingest(target, output_dir=output)
