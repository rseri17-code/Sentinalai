"""Ingest raw files from sentinel_wiki/raw/ into sentinel_wiki/wiki/.

Idempotent: skips files whose hash has not changed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from sentinel_wiki import note_generator, indexer
from sentinel_wiki.vector_index import WikiVectorIndex

_SUPPORTED = {".md", ".txt", ".json", ".yaml", ".yml", ".csv"}


@dataclass
class IngestResult:
    ingested: list[str] = field(default_factory=list)    # notes created/updated
    skipped: list[str] = field(default_factory=list)     # unchanged files
    errors: list[str] = field(default_factory=list)


def ingest(base_path: str = "sentinel_wiki") -> IngestResult:
    """Scan raw/ and generate/update wiki notes for all supported files.

    Args:
        base_path: Root sentinel_wiki directory.

    Returns:
        IngestResult with counts of ingested/skipped/errored files.
    """
    root = Path(base_path)
    raw_dir = root / "raw"
    wiki_dir = root / "wiki"
    indexes_dir = root / "indexes"

    wiki_dir.mkdir(parents=True, exist_ok=True)
    indexes_dir.mkdir(parents=True, exist_ok=True)

    result = IngestResult()
    now = datetime.now(timezone.utc).isoformat()

    # Load all indexes once
    src_idx = indexer.load_source_index(indexes_dir)
    ent_idx = indexer.load_entity_index(indexes_dir)
    tag_idx = indexer.load_tag_index(indexes_dir)
    lnk_idx = indexer.load_link_index(indexes_dir)
    vec_idx = WikiVectorIndex(str(root))

    for raw_file in sorted(raw_dir.rglob("*")):
        if not raw_file.is_file():
            continue
        if raw_file.suffix.lower() not in _SUPPORTED:
            continue
        if raw_file.name == "README.md":  # bootstrap artifact, not user content
            continue

        source_rel = raw_file.relative_to(root).as_posix()  # e.g. raw/foo.json
        note_name = raw_file.stem + ".md"
        note_path = wiki_dir / note_name
        note_rel = note_path.relative_to(root).as_posix()   # e.g. wiki/foo.md

        try:
            current_hash = note_generator.file_hash(raw_file)
            existing_entry = indexer.get_source_entry(src_idx, source_rel)

            if (
                existing_entry
                and existing_entry.get("source_hash") == f"sha256:{current_hash}"
                and note_path.exists()
            ):
                result.skipped.append(source_rel)
                continue

            # Read existing note for created_at preservation
            existing_note = note_path.read_text() if note_path.exists() else None

            note_content = note_generator.generate_note(
                source_path=raw_file,
                source_rel=source_rel,
                existing_note=existing_note,
            )
            note_path.write_text(note_content)

            # Extract metadata for indexes
            nid = note_generator.note_id_for(source_rel)
            tags = _extract_front_matter_list(note_content, "tags")
            entities = _extract_entities_from_body(note_content)
            outbound = _extract_wikilinks(note_content)

            src_idx = indexer.upsert_source_entry(
                src_idx, source_rel,
                f"sha256:{current_hash}", note_rel, nid, now,
            )
            ent_idx = indexer.upsert_entities(ent_idx, note_rel, entities)
            tag_idx = indexer.upsert_tags(tag_idx, note_rel, tags)
            lnk_idx = indexer.upsert_links(lnk_idx, note_rel, outbound)
            vec_idx.index_note(note_rel, note_content)

            result.ingested.append(source_rel)

        except Exception as exc:
            result.errors.append(f"{source_rel}: {exc}")

    # Persist all indexes atomically
    indexer.save_source_index(indexes_dir, src_idx)
    indexer.save_entity_index(indexes_dir, ent_idx)
    indexer.save_tag_index(indexes_dir, tag_idx)
    indexer.save_link_index(indexes_dir, lnk_idx)
    vec_idx.save()

    return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_front_matter_list(note: str, key: str) -> list[str]:
    m = re.search(rf"^{key}:\s*(\[.*?\])", note, re.MULTILINE)
    if m:
        try:
            import json
            return json.loads(m.group(1))
        except Exception:
            pass
    return []


def _extract_entities_from_body(note: str) -> list[str]:
    entities: list[str] = []
    in_entities = False
    for line in note.splitlines():
        if line.strip() == "## Entities":
            in_entities = True
            continue
        if in_entities:
            if line.startswith("## "):
                break
            m = re.match(r"^- (.+)", line.strip())
            if m:
                entities.append(m.group(1))
    return entities


def _extract_wikilinks(note: str) -> list[str]:
    return re.findall(r"\[\[([^\]]+)\]\]", note)
