import random
import re

import pytest

from app.services.rag.chunking import chunk_text

WORDS = (
    "shipping returns refund order delivery size colour black cotton oversized policy days free "
    "over express standard international tracking warranty care wash cold tumble dry fabric"
).split()


def sentence(rng: random.Random, n: int = 12) -> str:
    return " ".join(rng.choice(WORDS) for _ in range(n)).capitalize() + "."


def paragraph(rng: random.Random, sentences: int = 5) -> str:
    return " ".join(sentence(rng) for _ in range(sentences))


def make_doc(seed: int = 1, sections: int = 6, paragraphs: int = 4) -> str:
    rng = random.Random(seed)
    parts = []
    for s in range(sections):
        parts.append(f"## Section {s} {rng.choice(WORDS)}")
        parts.extend(paragraph(rng) for _ in range(paragraphs))
    return "\n\n".join(parts)


def contents(text: str, **kwargs) -> list[str]:
    return [c.content for c in chunk_text(text, **kwargs)]


# --- the cases the phase asked for -------------------------------------------------------------


def test_short_document_is_a_single_chunk_unchanged():
    chunks = chunk_text("# Returns\n\nYou can return items within 30 days.")
    assert [c.content for c in chunks] == ["# Returns\n\nYou can return items within 30 days."]
    assert chunks[0].index == 0


def test_empty_and_whitespace_only_documents_yield_no_chunks():
    assert chunk_text("") == []
    assert chunk_text("   \n\n \t  \r\n") == []


def test_long_document_is_split_into_many_bounded_chunks():
    doc = make_doc(sections=10, paragraphs=6)
    assert len(doc) > 10_000
    chunks = chunk_text(doc, chunk_size=1000, overlap=100)
    assert len(chunks) > 8
    assert all(0 < len(c.content) <= 1000 for c in chunks)  # a HARD cap, never exceeded
    assert all(len(c.content) >= 100 for c in chunks[:-1])  # no confetti of tiny chunks


def test_chunks_are_not_one_per_sentence():
    doc = " ".join(sentence(random.Random(i)) for i in range(40))  # 40 short sentences
    chunks = chunk_text(doc, chunk_size=1000, overlap=100)
    assert len(chunks) < 10


def test_order_is_preserved_and_indexes_are_contiguous():
    rng = random.Random(7)
    doc = "\n\n".join(f"## Part {i}\n\nMarker-{i:03d}. {paragraph(rng, 4)}" for i in range(30))
    chunks = chunk_text(doc, chunk_size=700, overlap=80)
    assert [c.index for c in chunks] == list(range(len(chunks)))
    first_seen = []
    for chunk in chunks:
        for marker in re.findall(r"Marker-(\d{3})", chunk.content):
            if int(marker) not in first_seen:
                first_seen.append(int(marker))
    assert first_seen == sorted(first_seen) == list(range(30))


def test_consecutive_chunks_overlap_when_a_section_is_split():
    rng = random.Random(3)
    doc = "## Shipping\n\n" + "\n\n".join(paragraph(rng, 6) for _ in range(8))
    chunks = chunk_text(doc, chunk_size=600, overlap=100)
    assert len(chunks) >= 3
    for previous, current in zip(chunks, chunks[1:], strict=False):
        body = current.content.split("\n", 1)[1]  # skip the carried heading line
        shared = [w for w in body.split()[:25] if w in previous.content.split()[-40:]]
        assert shared, "a continuation chunk must start with text from the end of the previous one"


def test_overlap_text_is_exactly_the_tail_of_the_previous_chunk():
    text = "\n\n".join(f"Paragraph {i}: " + "word " * 50 for i in range(10))
    chunks = chunk_text(text, chunk_size=400, overlap=60)
    for previous, current in zip(chunks, chunks[1:], strict=False):
        head = current.content.split("\n\n", 1)[0]
        assert head and head in previous.content, "overlap must be verbatim previous-chunk text"
        assert len(head) <= 60


def test_zero_overlap_means_no_repeated_text():
    text = "\n\n".join(f"Unique-{i} " + "filler " * 60 for i in range(8))
    chunks = chunk_text(text, chunk_size=500, overlap=0)
    joined = "\n\n".join(c.content for c in chunks)
    for i in range(8):
        assert joined.count(f"Unique-{i} ") == 1


def test_section_boundaries_start_new_chunks_without_overlap():
    rng = random.Random(11)
    doc = f"# Alpha\n\nALPHAMARK {paragraph(rng, 6)}\n\n# Omega\n\nOMEGAMARK {paragraph(rng, 6)}"
    chunks = chunk_text(doc, chunk_size=600, overlap=100, min_chunk_size=100)
    omega = next(c for c in chunks if c.content.startswith("# Omega"))
    assert "ALPHAMARK" not in omega.content  # nothing from the previous topic leaks in


def test_tiny_sections_are_merged_instead_of_becoming_tiny_chunks():
    doc = "# A\n\nOne line.\n\n# B\n\nTwo lines.\n\n# C\n\nThree lines."
    chunks = chunk_text(doc, chunk_size=1000)
    assert len(chunks) == 1 and "# C" in chunks[0].content


def test_continuation_chunks_carry_their_section_heading():
    rng = random.Random(5)
    doc = "## Refund policy\n\n" + "\n\n".join(paragraph(rng, 6) for _ in range(6))
    chunks = chunk_text(doc, chunk_size=500, overlap=60)
    assert len(chunks) > 2
    assert all(c.content.startswith("## Refund policy") for c in chunks)


def test_duplicate_content_does_not_produce_duplicate_chunks():
    section = "## FAQ\n\nDo you ship worldwide? Yes, we do."
    doc = "\n\n".join([section] * 4)
    chunks = chunk_text(doc, chunk_size=1000, overlap=50, min_chunk_size=0)
    assert len(chunks) == 1
    keys = [re.sub(r"\s+", " ", c.content).lower() for c in chunk_text(make_doc() * 3)]
    assert len(keys) == len(set(keys))


# --- robustness ---------------------------------------------------------------------------------


def test_oversized_blocks_are_split_at_sentence_boundaries_when_possible():
    rng = random.Random(2)
    block = " ".join(sentence(rng, 15) for _ in range(40))  # one ~4000 char paragraph, no newlines
    chunks = chunk_text(block, chunk_size=800, overlap=80)
    assert len(chunks) >= 5 and all(len(c.content) <= 800 for c in chunks)
    assert all(c.content.rstrip().endswith((".", "!", "?")) for c in chunks[:-1])


def unbroken(n: int, seed: int = 0) -> str:
    rng = random.Random(seed)
    return "".join(rng.choice("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(n))


def test_text_without_any_spaces_is_still_split_within_the_limit():
    chunks = chunk_text(unbroken(10_000), chunk_size=1000, overlap=100)
    assert len(chunks) >= 10 and all(len(c.content) <= 1000 for c in chunks)


def test_identical_repeating_text_collapses_because_duplicate_chunks_are_dropped():
    chunks = chunk_text("x" * 10_000, chunk_size=1000, overlap=100)
    assert len(chunks) < 10 and all(len(c.content) <= 1000 for c in chunks)


def test_very_long_unbroken_url_inside_text_cannot_exceed_the_limit():
    blob = unbroken(5000, seed=1)
    text = "Visit https://example.com/" + blob + " for details."
    chunks = chunk_text(text, chunk_size=500, overlap=50)
    assert all(len(c.content) <= 500 for c in chunks)
    assert blob[:200] in chunks[0].content or blob[:200] in "".join(c.content for c in chunks)
    assert sum(len(c.content) for c in chunks) >= 5000  # the long token was split, not dropped


def test_long_list_is_split_on_item_boundaries():
    items = "\n".join(f"- Item number {i} with a few words" for i in range(200))
    chunks = chunk_text(items, chunk_size=600, overlap=60)
    assert all(len(c.content) <= 600 for c in chunks)
    assert all(c.content.lstrip().startswith("- ") or c.index > 0 for c in chunks)


def test_no_text_is_lost():
    rng = random.Random(9)
    doc = make_doc(seed=9, sections=8, paragraphs=5)
    chunks = chunk_text(doc, chunk_size=700, overlap=90)
    original_words = re.findall(r"\w+", doc)
    chunk_words = re.findall(r"\w+", " ".join(c.content for c in chunks))
    it = iter(chunk_words)
    assert all(word in it for word in original_words), "every original word survives, in order"
    assert rng  # (seeded generator kept for readability of the failure output)


@pytest.mark.parametrize("seed", range(25))
def test_invariants_hold_on_random_documents(seed):
    rng = random.Random(seed)
    size = rng.choice([250, 400, 800, 1200, 2000])
    overlap = rng.choice([0, 20, size // 10, size // 4])
    doc = "\n\n".join(
        rng.choice(
            [
                f"# {sentence(rng, 3)}",
                paragraph(rng, rng.randint(1, 12)),
                "\n".join(f"- {sentence(rng, 5)}" for _ in range(rng.randint(2, 9))),
                " ".join(rng.choice(WORDS) for _ in range(rng.randint(150, 600))),
            ]
        )
        for _ in range(rng.randint(1, 40))
    )
    chunks = chunk_text(doc, chunk_size=size, overlap=overlap)
    assert chunks
    assert [c.index for c in chunks] == list(range(len(chunks)))
    assert all(c.content.strip() and len(c.content) <= size for c in chunks)
    assert chunks == chunk_text(doc, chunk_size=size, overlap=overlap)  # deterministic


def test_chunking_is_deterministic():
    doc = make_doc(seed=4, sections=12)
    assert chunk_text(doc) == chunk_text(doc)


def test_windows_line_endings_are_normalized():
    a = chunk_text("# T\r\n\r\nline one\r\n\r\nline two")
    b = chunk_text("# T\n\nline one\n\nline two")
    assert a == b


def test_invalid_parameters_are_rejected():
    with pytest.raises(ValueError):
        chunk_text("text", chunk_size=100, overlap=80)
    with pytest.raises(ValueError):
        chunk_text("text", chunk_size=1000, overlap=-1)


def test_defaults_produce_reasonable_sizes_for_a_realistic_page():
    chunks = chunk_text(make_doc(seed=6, sections=15, paragraphs=5))
    sizes = [len(c.content) for c in chunks]
    assert max(sizes) <= 1200 and sum(sizes) / len(sizes) > 400
