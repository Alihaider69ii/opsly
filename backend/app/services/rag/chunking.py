"""Deterministic text chunking.

Input is the structured text produced by the extractor: blocks separated by blank lines, headings
as ``# Heading``. Rules:

* Order is preserved and nothing is dropped (except exact duplicate chunks).
* Blocks are packed greedily up to ``chunk_size`` characters, a HARD upper bound on the final
  chunk text (including any carried heading and overlap), so no chunk is ever oversized.
* A new section (heading) starts a new chunk once the current one has at least ``min_chunk_size``
  characters; a tiny section is merged into its neighbour instead of becoming a useless chunk.
* When a chunk ends because it is full (mid-section), the next one begins with the section heading
  and the last ``overlap`` characters of the previous chunk, so a sentence that straddles a
  boundary is still retrievable and every chunk says what it is about. At section boundaries no
  overlap is added: the topic changes.
* Blocks too large for one chunk are split at line, then sentence, then word, then character
  boundaries (in that order of preference).
"""

import re
from dataclasses import dataclass

_HEADING_RE = re.compile(r"^#{1,6} \S")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
_BLANK_LINES_RE = re.compile(r"\n{2,}")
_WS_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class Chunk:
    index: int
    content: str


@dataclass(frozen=True)
class _Segment:
    text: str
    is_heading: bool


def _split_long(text: str, max_len: int, level: int = 0) -> list[str]:
    """Split text into pieces of at most max_len, preferring natural boundaries."""
    if len(text) <= max_len:
        return [text]
    if level == 0:
        parts, joiner = text.split("\n"), "\n"
    elif level == 1:
        parts, joiner = _SENTENCE_SPLIT_RE.split(text), " "
    elif level == 2:
        parts, joiner = text.split(" "), " "
    else:
        return [text[i : i + max_len] for i in range(0, len(text), max_len)]

    pieces: list[str] = []
    current = ""
    for part in parts:
        if not part:
            continue
        for sub in _split_long(part, max_len, level + 1) if len(part) > max_len else [part]:
            if not current:
                current = sub
            elif len(current) + len(joiner) + len(sub) <= max_len:
                current += joiner + sub
            else:
                pieces.append(current)
                current = sub
    if current:
        pieces.append(current)
    return pieces


def _segments(text: str, piece_max: int) -> list[_Segment]:
    segments: list[_Segment] = []
    for block in _BLANK_LINES_RE.split(text):
        block = block.strip()
        if not block:
            continue
        if "\n" not in block and _HEADING_RE.match(block) and len(block) <= piece_max:
            segments.append(_Segment(block, True))
            continue
        segments.extend(_Segment(piece, False) for piece in _split_long(block, piece_max))
    return segments


def _overlap_tail(text: str, overlap: int) -> str:
    if overlap <= 0 or not text:
        return ""
    tail = text[-overlap:]
    if len(text) > overlap:  # we cut mid-text: skip the partial word at the start
        match = re.search(r"\s", tail)
        tail = tail[match.end() :] if match else ""
    return tail.strip()


def _carry(heading: str, limit: int) -> str:
    if not heading:
        return ""
    line = (
        heading if len(heading) <= limit else heading[:limit].rsplit(" ", 1)[0] or heading[:limit]
    )
    return line.strip()


def chunk_text(
    text: str,
    *,
    chunk_size: int = 1200,
    overlap: int = 150,
    min_chunk_size: int | None = None,
) -> list[Chunk]:
    if overlap < 0 or chunk_size < 2 * max(overlap, 1):
        raise ValueError("overlap must be non-negative and at most half of chunk_size")
    text = text.replace("\r\n", "\n").strip()
    if not text:
        return []

    min_chunk = chunk_size // 4 if min_chunk_size is None else min_chunk_size
    carry_max = min(100, chunk_size // 8)
    # Largest single piece such that carried heading + overlap + piece still fits in chunk_size.
    piece_max = chunk_size - carry_max - 1 - overlap - 4

    texts: list[str] = []
    body: list[str] = []
    body_len = 0
    prefix = ""
    heading = ""

    def flush(next_prefix_from_size_split: bool) -> None:
        nonlocal body, body_len, prefix
        body_text = "\n\n".join(body)
        texts.append(prefix + body_text)
        prefix = ""
        if next_prefix_from_size_split:
            carried = _carry(heading, carry_max)
            tail = _overlap_tail(body_text, overlap)
            prefix = (carried + "\n" if carried else "") + (tail + "\n\n" if tail else "")
        body, body_len = [], 0

    for segment in _segments(text, piece_max):
        added = len(segment.text) + (2 if body else 0)
        overflow = bool(body) and len(prefix) + body_len + added > chunk_size
        if segment.is_heading:
            if body and (body_len >= min_chunk or overflow):
                flush(next_prefix_from_size_split=False)
            heading = segment.text
        elif overflow:
            flush(next_prefix_from_size_split=True)
        body.append(segment.text)
        body_len += len(segment.text) + (2 if len(body) > 1 else 0)
    if body:
        flush(next_prefix_from_size_split=False)

    unique: list[str] = []
    seen: set[str] = set()
    for chunk in texts:
        key = _WS_RE.sub(" ", chunk).strip().lower()
        if key and key not in seen:
            seen.add(key)
            unique.append(chunk.strip())
    return [Chunk(i, content) for i, content in enumerate(unique)]
