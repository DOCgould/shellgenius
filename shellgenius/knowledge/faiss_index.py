"""
FAISS Knowledge Base — vector search over The Linux Programming Interface.

This module:
1. Extracts text from the TLPI PDF by chapter
2. Chunks text into semantically meaningful segments
3. Embeds chunks using sentence-transformers (all-MiniLM-L6-v2, 384-dim)
4. Builds a FAISS index for fast similarity search
5. Provides a query interface for the ShellGenius agent

The index gives the agent deep syscall/kernel knowledge:
- 500+ system calls documented
- Pipes, FIFOs, sockets, signals, processes, IPC, terminals, PTYs
- The exact semantics that underpin every shell command

Architecture:
    [User query: "how do named pipes work?"]
        ↓ embed
    [384-dim vector]
        ↓ FAISS search
    [Top-K chunk IDs]
        ↓ retrieve
    [TLPI text: "Chapter 44: Pipes and FIFOs..."]
        ↓
    [ShellGenius agent uses this to give expert answers]
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# Lazy imports — these are heavy
_faiss = None
_fitz = None
_model = None


def _get_faiss():
    global _faiss
    if _faiss is None:
        import faiss
        _faiss = faiss
    return _faiss


def _get_fitz():
    global _fitz
    if _fitz is None:
        import fitz
        _fitz = fitz
    return _fitz


def _get_model():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        _model = SentenceTransformer("all-MiniLM-L6-v2")
    return _model


# ---------------------------------------------------------------------------
# Chapter map — page ranges from the TOC
# ---------------------------------------------------------------------------

# PDF page numbers (0-indexed) based on book page numbers + front matter offset
# Front matter is ~30 pages, so book page 1 ≈ PDF page ~31
# We'll detect this dynamically, but here's the chapter structure:

CHAPTERS = [
    (1, "History and Standards", 1),
    (2, "Fundamental Concepts", 21),
    (3, "System Programming Concepts", 43),
    (4, "File I/O: The Universal I/O Model", 69),
    (5, "File I/O: Further Details", 89),
    (6, "Processes", 113),
    (7, "Memory Allocation", 139),
    (8, "Users and Groups", 153),
    (9, "Process Credentials", 167),
    (10, "Time", 185),
    (11, "System Limits and Options", 211),
    (12, "System and Process Information", 223),
    (13, "File I/O Buffering", 233),
    (14, "File Systems", 251),
    (15, "File Attributes", 279),
    (16, "Extended Attributes", 311),
    (17, "Access Control Lists", 319),
    (18, "Directories and Links", 339),
    (19, "Monitoring File Events", 375),
    (20, "Signals: Fundamental Concepts", 387),
    (21, "Signals: Signal Handlers", 421),
    (22, "Signals: Advanced Features", 447),
    (23, "Timers and Sleeping", 479),
    (24, "Process Creation", 513),
    (25, "Process Termination", 531),
    (26, "Monitoring Child Processes", 541),
    (27, "Program Execution", 563),
    (28, "Process Creation and Program Execution in More Detail", 591),
    (29, "Threads: Introduction", 617),
    (30, "Threads: Thread Synchronization", 631),
    (31, "Threads: Thread Safety and Per-Thread Storage", 655),
    (32, "Threads: Thread Cancellation", 671),
    (33, "Threads: Further Details", 681),
    (34, "Process Groups, Sessions, and Job Control", 699),
    (35, "Process Priorities and Scheduling", 733),
    (36, "Process Resources", 753),
    (37, "Daemons", 767),
    (38, "Writing Secure Privileged Programs", 783),
    (39, "Capabilities", 797),
    (40, "Login Accounting", 817),
    (41, "Fundamentals of Shared Libraries", 833),
    (42, "Advanced Features of Shared Libraries", 859),
    (43, "Interprocess Communication Overview", 877),
    (44, "Pipes and FIFOs", 889),
    (45, "Introduction to System V IPC", 921),
    (46, "System V Message Queues", 937),
    (47, "System V Semaphores", 965),
    (48, "System V Shared Memory", 997),
    (49, "Memory Mappings", 1017),
    (50, "Virtual Memory Operations", 1045),
    (51, "Introduction to POSIX IPC", 1057),
    (52, "POSIX Message Queues", 1063),
    (53, "POSIX Semaphores", 1089),
    (54, "POSIX Shared Memory", 1107),
    (55, "File Locking", 1117),
    (56, "Sockets: Introduction", 1149),
    (57, "Sockets: UNIX Domain", 1165),
    (58, "Sockets: Fundamentals of TCP/IP Networks", 1179),
    (59, "Sockets: Internet Domains", 1197),
    (60, "Sockets: Server Design", 1239),
    (61, "Sockets: Advanced Topics", 1253),
    (62, "Terminals", 1289),
    (63, "Alternative I/O Models", 1325),
    (64, "Pseudoterminals", 1375),
]

# Chapters most relevant to ShellGenius (prioritized for indexing)
PRIORITY_CHAPTERS = {
    4, 5,           # File I/O (fd model — the foundation)
    6,              # Processes
    20, 21, 22,     # Signals
    24, 25, 26, 27, 28,  # Process creation/termination/execution
    34,             # Process Groups, Sessions, Job Control
    43, 44,         # IPC Overview, Pipes and FIFOs
    49,             # Memory Mappings (mmap)
    55,             # File Locking (flock)
    56, 57,         # Sockets intro + UNIX Domain
    61,             # Sockets: Advanced
    62,             # Terminals
    63,             # Alternative I/O (select, poll, epoll)
    64,             # Pseudoterminals
}


# ---------------------------------------------------------------------------
# Text extraction
# ---------------------------------------------------------------------------

@dataclass
class TextChunk:
    """A chunk of text from the book with metadata."""
    text: str
    chapter_num: int
    chapter_title: str
    page_num: int          # book page number
    chunk_id: int = 0
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "chapter": self.chapter_num,
            "title": self.chapter_title,
            "page": self.page_num,
            "chunk_id": self.chunk_id,
            "tags": self.tags,
        }


def extract_pdf_text(pdf_path: str | Path, *, page_offset: int = 30) -> list[TextChunk]:
    """
    Extract text from the TLPI PDF, organized by chapter.

    Args:
        pdf_path: Path to the PDF file.
        page_offset: Number of front-matter pages before page 1 of content.

    Returns:
        List of TextChunks, one per chunk (several per chapter).
    """
    fitz = _get_fitz()
    doc = fitz.open(str(pdf_path))
    total_pages = len(doc)
    chunks: list[TextChunk] = []
    chunk_id = 0

    for i, (ch_num, ch_title, book_page) in enumerate(CHAPTERS):
        # Calculate PDF page range for this chapter
        pdf_start = book_page + page_offset
        if i + 1 < len(CHAPTERS):
            pdf_end = CHAPTERS[i + 1][2] + page_offset
        else:
            pdf_end = min(total_pages, book_page + page_offset + 40)

        # Clamp to actual document
        pdf_start = min(pdf_start, total_pages - 1)
        pdf_end = min(pdf_end, total_pages)

        # Extract text from all pages in this chapter
        chapter_text = ""
        for page_idx in range(pdf_start, pdf_end):
            try:
                page = doc[page_idx]
                chapter_text += page.get_text() + "\n"
            except (IndexError, RuntimeError):
                continue

        if not chapter_text.strip():
            continue

        # Chunk the chapter text
        chapter_chunks = _chunk_text(
            chapter_text,
            chapter_num=ch_num,
            chapter_title=ch_title,
            book_page_start=book_page,
            base_chunk_id=chunk_id,
        )
        chunks.extend(chapter_chunks)
        chunk_id += len(chapter_chunks)

    doc.close()
    return chunks


def _chunk_text(
    text: str,
    *,
    chapter_num: int,
    chapter_title: str,
    book_page_start: int,
    base_chunk_id: int,
    chunk_size: int = 512,
    overlap: int = 64,
) -> list[TextChunk]:
    """
    Split text into overlapping chunks of ~chunk_size words.

    Uses paragraph boundaries when possible to avoid splitting mid-sentence.
    """
    # Clean the text
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = re.sub(r'[ \t]+', ' ', text)

    # Split on paragraph boundaries
    paragraphs = re.split(r'\n\n+', text)

    chunks: list[TextChunk] = []
    current_words: list[str] = []
    current_para_start = 0

    for para in paragraphs:
        words = para.split()
        if not words:
            continue

        # If adding this paragraph would exceed chunk_size, flush
        if len(current_words) + len(words) > chunk_size and current_words:
            chunk_text = " ".join(current_words)
            if len(chunk_text.strip()) > 50:  # skip tiny chunks
                # Auto-tag based on content
                tags = _auto_tag(chunk_text)
                chunks.append(TextChunk(
                    text=chunk_text,
                    chapter_num=chapter_num,
                    chapter_title=chapter_title,
                    page_num=book_page_start,
                    chunk_id=base_chunk_id + len(chunks),
                    tags=tags,
                ))
            # Keep overlap words for context continuity
            current_words = current_words[-overlap:] if overlap else []

        current_words.extend(words)

    # Flush remaining
    if current_words:
        chunk_text = " ".join(current_words)
        if len(chunk_text.strip()) > 50:
            tags = _auto_tag(chunk_text)
            chunks.append(TextChunk(
                text=chunk_text,
                chapter_num=chapter_num,
                chapter_title=chapter_title,
                page_num=book_page_start,
                chunk_id=base_chunk_id + len(chunks),
                tags=tags,
            ))

    return chunks


def _auto_tag(text: str) -> list[str]:
    """Auto-tag a chunk based on keywords."""
    tags = []
    lower = text.lower()
    tag_patterns = {
        "pipe": ["pipe", "fifo", "mkfifo", "popen"],
        "socket": ["socket", "bind", "listen", "accept", "connect", "unix domain"],
        "signal": ["signal", "sigaction", "sigset", "kill(", "raise("],
        "process": ["fork", "exec", "wait", "pid", "process group"],
        "fd": ["file descriptor", "dup", "dup2", "fcntl", "open(", "close("],
        "ipc": ["ipc", "message queue", "semaphore", "shared memory", "mmap"],
        "terminal": ["terminal", "tty", "pty", "pseudoterminal", "termios"],
        "thread": ["thread", "pthread", "mutex", "condition variable"],
        "io": ["read(", "write(", "select", "poll", "epoll", "i/o model"],
        "lock": ["flock", "fcntl lock", "file lock", "advisory lock"],
        "cgroup": ["cgroup", "namespace", "capability", "seccomp"],
        "job_control": ["job control", "session", "process group", "foreground", "background"],
    }
    for tag, patterns in tag_patterns.items():
        if any(p in lower for p in patterns):
            tags.append(tag)
    return tags


# ---------------------------------------------------------------------------
# FAISS index building
# ---------------------------------------------------------------------------

@dataclass
class FaissKnowledgeBase:
    """
    FAISS-backed knowledge base for The Linux Programming Interface.

    Usage:
        kb = FaissKnowledgeBase.build("data/tlpi.pdf")
        results = kb.query("how do named pipes work?", top_k=5)
        for chunk, score in results:
            print(f"[Ch.{chunk.chapter_num}] {chunk.chapter_title} (score: {score:.3f})")
            print(chunk.text[:200])
    """
    index: object  # faiss.Index
    chunks: list[TextChunk]
    embedding_dim: int = 384
    model_name: str = "all-MiniLM-L6-v2"

    def query(self, question: str, *, top_k: int = 5,
              chapter_filter: Optional[set[int]] = None,
              tag_filter: Optional[str] = None) -> list[tuple[TextChunk, float]]:
        """
        Search the knowledge base.

        Args:
            question: Natural language query.
            top_k: Number of results to return.
            chapter_filter: Only return results from these chapters.
            tag_filter: Only return results with this tag.

        Returns:
            List of (chunk, score) tuples, sorted by relevance.
        """
        import numpy as np
        faiss = _get_faiss()
        model = _get_model()

        # Embed the query
        query_vec = model.encode([question], normalize_embeddings=True)
        query_vec = np.array(query_vec, dtype=np.float32)

        # Search more than needed if we're filtering
        search_k = top_k * 5 if (chapter_filter or tag_filter) else top_k

        distances, indices = self.index.search(query_vec, min(search_k, len(self.chunks)))

        results = []
        for dist, idx in zip(distances[0], indices[0]):
            if idx < 0 or idx >= len(self.chunks):
                continue
            chunk = self.chunks[idx]

            # Apply filters
            if chapter_filter and chunk.chapter_num not in chapter_filter:
                continue
            if tag_filter and tag_filter not in chunk.tags:
                continue

            results.append((chunk, float(dist)))
            if len(results) >= top_k:
                break

        return results

    def query_by_tag(self, tag: str, *, top_k: int = 10) -> list[TextChunk]:
        """Get all chunks with a specific tag."""
        return [c for c in self.chunks if tag in c.tags][:top_k]

    def stats(self) -> dict:
        """Return index statistics."""
        chapters_covered = set(c.chapter_num for c in self.chunks)
        tags = {}
        for c in self.chunks:
            for t in c.tags:
                tags[t] = tags.get(t, 0) + 1
        return {
            "total_chunks": len(self.chunks),
            "chapters_covered": len(chapters_covered),
            "embedding_dim": self.embedding_dim,
            "model": self.model_name,
            "top_tags": dict(sorted(tags.items(), key=lambda x: -x[1])[:15]),
        }

    def save(self, directory: str | Path) -> None:
        """Save index and metadata to disk."""
        faiss = _get_faiss()
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)

        # Save FAISS index
        faiss.write_index(self.index, str(directory / "tlpi.faiss"))

        # Save chunk metadata
        metadata = [c.to_dict() for c in self.chunks]
        (directory / "tlpi_chunks.json").write_text(json.dumps(metadata, indent=2))

        # Save stats
        (directory / "tlpi_stats.json").write_text(json.dumps(self.stats(), indent=2))

    @classmethod
    def load(cls, directory: str | Path) -> "FaissKnowledgeBase":
        """Load a saved index from disk."""
        faiss = _get_faiss()
        directory = Path(directory)

        index = faiss.read_index(str(directory / "tlpi.faiss"))

        metadata = json.loads((directory / "tlpi_chunks.json").read_text())
        chunks = [
            TextChunk(
                text=m["text"],
                chapter_num=m["chapter"],
                chapter_title=m["title"],
                page_num=m["page"],
                chunk_id=m["chunk_id"],
                tags=m.get("tags", []),
            )
            for m in metadata
        ]

        return cls(index=index, chunks=chunks)

    @classmethod
    def build(cls, pdf_path: str | Path, *,
              priority_only: bool = False,
              show_progress: bool = True) -> "FaissKnowledgeBase":
        """
        Build the FAISS index from the TLPI PDF.

        Args:
            pdf_path: Path to The Linux Programming Interface PDF.
            priority_only: If True, only index chapters most relevant to shell work.
            show_progress: Print progress during indexing.

        Returns:
            FaissKnowledgeBase ready for queries.
        """
        import numpy as np
        faiss = _get_faiss()
        model = _get_model()

        if show_progress:
            print(f"Extracting text from {pdf_path}...")

        chunks = extract_pdf_text(pdf_path)

        if priority_only:
            chunks = [c for c in chunks if c.chapter_num in PRIORITY_CHAPTERS]
            if show_progress:
                print(f"  Filtered to priority chapters: {len(chunks)} chunks")

        if show_progress:
            print(f"  Total chunks: {len(chunks)}")
            print(f"  Chapters: {len(set(c.chapter_num for c in chunks))}")

        # Embed all chunks
        if show_progress:
            print(f"Embedding {len(chunks)} chunks with {model.get_sentence_embedding_dimension()}-dim model...")

        texts = [c.text for c in chunks]
        embeddings = model.encode(
            texts,
            normalize_embeddings=True,
            show_progress_bar=show_progress,
            batch_size=64,
        )
        embeddings = np.array(embeddings, dtype=np.float32)

        if show_progress:
            print(f"Building FAISS index...")

        # Build index — use IndexFlatIP for cosine similarity (vectors are normalized)
        dim = embeddings.shape[1]
        index = faiss.IndexFlatIP(dim)
        index.add(embeddings)

        if show_progress:
            print(f"Done. Index has {index.ntotal} vectors of dimension {dim}.")

        return cls(index=index, chunks=chunks, embedding_dim=dim)


# ---------------------------------------------------------------------------
# CLI interface
# ---------------------------------------------------------------------------

def build_and_save(pdf_path: str = "data/tlpi.pdf",
                   output_dir: str = "data/faiss_index",
                   priority_only: bool = False) -> FaissKnowledgeBase:
    """Build the index and save to disk."""
    kb = FaissKnowledgeBase.build(pdf_path, priority_only=priority_only)
    kb.save(output_dir)
    print(f"\nSaved to {output_dir}/")
    stats = kb.stats()
    print(f"  Chunks: {stats['total_chunks']}")
    print(f"  Chapters: {stats['chapters_covered']}")
    print(f"  Top tags: {', '.join(f'{k}({v})' for k, v in list(stats['top_tags'].items())[:8])}")
    return kb


if __name__ == "__main__":
    import sys
    pdf = sys.argv[1] if len(sys.argv) > 1 else "data/tlpi.pdf"
    out = sys.argv[2] if len(sys.argv) > 2 else "data/faiss_index"
    priority = "--priority" in sys.argv
    build_and_save(pdf, out, priority_only=priority)
