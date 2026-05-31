"""Manage the four JSON indexes for sentinel_wiki.

All writes are atomic via tmp-swap.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def _load(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _save(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# source_index.json — one entry per ingested file
# ---------------------------------------------------------------------------

def load_source_index(indexes_dir: Path) -> list[dict]:
    return _load(indexes_dir / "source_index.json") or []


def save_source_index(indexes_dir: Path, index: list[dict]) -> None:
    _save(indexes_dir / "source_index.json", index)


def upsert_source_entry(
    index: list[dict],
    source_file: str,
    source_hash: str,
    generated_note: str,
    note_id: str,
    last_ingested: str,
) -> list[dict]:
    for entry in index:
        if entry.get("source_file") == source_file:
            entry.update({
                "source_hash": source_hash,
                "generated_note": generated_note,
                "note_id": note_id,
                "last_ingested": last_ingested,
            })
            return index
    index.append({
        "source_file": source_file,
        "source_hash": source_hash,
        "generated_note": generated_note,
        "note_id": note_id,
        "last_ingested": last_ingested,
    })
    return index


def get_source_entry(index: list[dict], source_file: str) -> dict | None:
    for entry in index:
        if entry.get("source_file") == source_file:
            return entry
    return None


# ---------------------------------------------------------------------------
# entity_index.json — entity → notes/patterns/receipts
# ---------------------------------------------------------------------------

def load_entity_index(indexes_dir: Path) -> dict:
    return _load(indexes_dir / "entity_index.json") or {}


def save_entity_index(indexes_dir: Path, index: dict) -> None:
    _save(indexes_dir / "entity_index.json", index)


def upsert_entities(index: dict, note_path: str, entities: list[str]) -> dict:
    # Remove stale entries for this note
    for ent_data in index.values():
        notes = ent_data.get("notes", [])
        if note_path in notes:
            notes.remove(note_path)

    for entity in entities:
        if entity not in index:
            index[entity] = {"notes": [], "patterns": [], "receipts": []}
        if note_path not in index[entity]["notes"]:
            index[entity]["notes"].append(note_path)
    return index


# ---------------------------------------------------------------------------
# tag_index.json — tag → [note_paths]
# ---------------------------------------------------------------------------

def load_tag_index(indexes_dir: Path) -> dict:
    return _load(indexes_dir / "tag_index.json") or {}


def save_tag_index(indexes_dir: Path, index: dict) -> None:
    _save(indexes_dir / "tag_index.json", index)


def upsert_tags(index: dict, note_path: str, tags: list[str]) -> dict:
    # Remove stale entries for this note
    for tag_notes in index.values():
        if note_path in tag_notes:
            tag_notes.remove(note_path)

    for tag in tags:
        if tag not in index:
            index[tag] = []
        if note_path not in index[tag]:
            index[tag].append(note_path)
    return index


# ---------------------------------------------------------------------------
# link_index.json — note → {outbound, inbound}
# ---------------------------------------------------------------------------

def load_link_index(indexes_dir: Path) -> dict:
    return _load(indexes_dir / "link_index.json") or {}


def save_link_index(indexes_dir: Path, index: dict) -> None:
    _save(indexes_dir / "link_index.json", index)


def upsert_links(index: dict, note_path: str, outbound: list[str]) -> dict:
    # Remove stale inbound links that pointed to this note
    for entry in index.values():
        inbound = entry.get("inbound_links", [])
        if note_path in inbound:
            inbound.remove(note_path)

    index[note_path] = {
        "outbound_links": outbound,
        "inbound_links": index.get(note_path, {}).get("inbound_links", []),
    }

    # Register inbound links on targets
    for target in outbound:
        if target not in index:
            index[target] = {"outbound_links": [], "inbound_links": []}
        if note_path not in index[target]["inbound_links"]:
            index[target]["inbound_links"].append(note_path)

    return index
