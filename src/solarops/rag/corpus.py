"""Load the Markdown knowledge base and split it into section-level chunks.

Each `## Section` of each document becomes one chunk, because operators ask about
one topic at a time and a section is the unit a citation should point to. Sections
longer than MAX_CHARS are split on paragraph boundaries so no chunk swamps a prompt.
"""

import hashlib
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

KNOWLEDGE_DIR = Path(__file__).resolve().parent.parent / "knowledge"
MAX_CHARS = 1200


@dataclass(frozen=True)
class Chunk:
    id: str  # "<doc>#<section-slug>", stable across runs: evals and citations use it
    doc: str
    title: str
    section: str
    text: str

    @property
    def heading(self) -> str:
        return f"{self.title} > {self.section}"


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _split_long(text: str) -> list[str]:
    if len(text) <= MAX_CHARS:
        return [text]
    parts, current = [], ""
    for para in text.split("\n\n"):
        if current and len(current) + len(para) + 2 > MAX_CHARS:
            parts.append(current)
            current = para
        else:
            current = f"{current}\n\n{para}" if current else para
    if current:
        parts.append(current)
    return parts


def chunk_markdown(doc: str, markdown: str) -> list[Chunk]:
    title, sections, heading, lines = doc, [], "Overview", []
    for line in markdown.splitlines():
        if line.startswith("# "):
            title = line[2:].strip()
        elif line.startswith("## "):
            sections.append((heading, lines))
            heading, lines = line[3:].strip(), []
        else:
            lines.append(line)
    sections.append((heading, lines))

    chunks = []
    for heading, body in sections:
        text = "\n".join(body).strip()
        if not text:
            continue
        for i, part in enumerate(_split_long(text), start=1):
            suffix = "" if i == 1 else f"-{i}"
            chunks.append(Chunk(f"{doc}#{slug(heading)}{suffix}", doc, title, heading, part))
    return chunks


@lru_cache(maxsize=4)
def load_corpus(directory: Path | str = KNOWLEDGE_DIR) -> tuple[Chunk, ...]:
    chunks: list[Chunk] = []
    for path in sorted(Path(directory).glob("*.md")):
        if path.name.lower() == "readme.md":
            continue
        chunks.extend(chunk_markdown(path.stem, path.read_text(encoding="utf-8")))
    ids = [c.id for c in chunks]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate section headings in the knowledge base")
    return tuple(chunks)


def corpus_version(chunks: tuple[Chunk, ...]) -> str:
    """Short content hash, reported by the API so answers can be traced to a corpus."""
    digest = hashlib.sha256()
    for c in chunks:
        digest.update(f"{c.id}\n{c.text}\n".encode())
    return digest.hexdigest()[:12]
