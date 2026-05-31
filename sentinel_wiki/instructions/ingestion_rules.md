# Ingestion Rules

## Supported File Types
- `.md` — Markdown documents
- `.txt` — Plain text files
- `.json` — JSON data files
- `.yaml` / `.yml` — YAML configuration or data files
- `.csv` — Tabular data files

## Idempotency
- Each raw file is hashed (SHA-256) on ingest.
- If the hash matches the stored hash, the note is NOT regenerated.
- If the hash differs, the note is updated and the index is refreshed.

## Note IDs
- Stable: derived from SHA-256 of the relative source file path.
- Will not change even if file content changes.

## Naming Convention
- Wiki notes are named after the source file: `raw/foo.json` → `wiki/foo.md`

## Tags
- Derived from: file extension, directory name, explicit `tags:` fields in YAML/JSON front matter.

## Entities
- JSON/YAML: top-level keys are extracted as entities.
- Markdown: `[[wikilinks]]` and level-2 headers extracted as entities.
- CSV: column names extracted as entities.
