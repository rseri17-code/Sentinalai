"""Lightweight local search across sentinel_wiki/wiki/ notes.

Supports: filename, tag, entity, and full-text search.
No vector database — pure string matching.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from sentinel_wiki import indexer
from sentinel_wiki.ingester import _SUPPORTED
from sentinel_wiki.vector_index import WikiVectorIndex


@dataclass
class SearchHit:
    note_path: str
    score: int                          # number of matches
    snippet: str = ""                   # first matching line
    match_type: str = ""                # filename|tag|entity|text


def search(
    query: str,
    base_path: str = "sentinel_wiki",
    limit: int = 20,
) -> list[SearchHit]:
    """Search wiki notes by filename, tag, entity, and text.

    Returns hits sorted by score (most matches first).
    """
    if not query:
        return []

    root = Path(base_path)
    wiki_dir = root / "wiki"
    indexes_dir = root / "indexes"
    q = query.lower().strip()

    hits: dict[str, SearchHit] = {}

    def _add(note: str, match_type: str, snippet: str = "") -> None:
        if note not in hits:
            hits[note] = SearchHit(note_path=note, score=0, match_type=match_type)
        hits[note].score += 1
        if not hits[note].snippet and snippet:
            hits[note].snippet = snippet[:200]

    # --- Filename search ---
    for note_file in wiki_dir.glob("*.md"):
        if q in note_file.stem.lower():
            _add(note_file.name, "filename", note_file.stem)

    # --- Tag search ---
    tag_idx = indexer.load_tag_index(indexes_dir)
    for tag, notes in tag_idx.items():
        if q in tag.lower():
            for note in notes:
                _add(Path(note).name, "tag", f"tag: {tag}")

    # --- Entity search ---
    ent_idx = indexer.load_entity_index(indexes_dir)
    for entity, data in ent_idx.items():
        if q in entity.lower():
            for note in data.get("notes", []):
                _add(Path(note).name, "entity", f"entity: {entity}")

    # --- Full-text search ---
    for note_file in wiki_dir.glob("*.md"):
        try:
            text = note_file.read_text(errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            if q in line.lower():
                _add(note_file.name, "text", line.strip())
                break  # one text hit per file

    # --- Semantic (TF-IDF cosine) search ---
    try:
        vec_idx = WikiVectorIndex(base_path)
        for hit in vec_idx.search(query, top_k=limit):
            note_name = Path(hit["note_path"]).name
            if note_name not in hits:
                hits[note_name] = SearchHit(
                    note_path=note_name,
                    score=0,
                    match_type="semantic",
                    snippet=f"similarity={hit['score']:.3f}",
                )
            hits[note_name].score += 1
    except Exception:
        pass

    results = sorted(hits.values(), key=lambda h: h.score, reverse=True)
    return results[:limit]


def status(base_path: str = "sentinel_wiki") -> dict:
    """Return counts of notes, raw files, and index entries."""
    root = Path(base_path)
    indexes_dir = root / "indexes"

    src_idx = indexer.load_source_index(indexes_dir)
    tag_idx = indexer.load_tag_index(indexes_dir)
    ent_idx = indexer.load_entity_index(indexes_dir)

    wiki_count = sum(
        1 for f in (root / "wiki").glob("*.md") if f.name != "README.md"
    ) if (root / "wiki").exists() else 0
    raw_count = sum(
        1 for f in (root / "raw").rglob("*")
        if f.is_file() and f.suffix.lower() in _SUPPORTED and f.name != "README.md"
    ) if (root / "raw").exists() else 0

    return {
        "raw_files": raw_count,
        "wiki_notes": wiki_count,
        "source_index_entries": len(src_idx),
        "unique_tags": len(tag_idx),
        "unique_entities": len(ent_idx),
    }
