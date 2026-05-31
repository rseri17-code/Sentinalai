"""Bootstrap the sentinel_wiki folder structure.

Creates directories and README files. Safe to call repeatedly.
"""

from __future__ import annotations

import os
from pathlib import Path

# Data subdirectories and their purpose descriptions
_DIRS: dict[str, str] = {
    "raw": "Source files to be ingested. Place .md, .txt, .json, .yaml, .csv files here.",
    "wiki": "Auto-generated Markdown notes. One note per raw file. Obsidian-compatible.",
    "patterns": "Recurring failure patterns extracted from incidents. YAML format.",
    "receipts": "RCA investigation receipts — what happened, what was found, what was fixed.",
    "decisions": "Architectural and operational decisions with rationale and outcomes.",
    "topology": "Service topology snapshots — dependencies, blast radius maps.",
    "queries": "Reusable query records — what was searched, what evidence was found.",
    "evals": "Evaluation results — quality scores, accuracy metrics per incident type.",
    "indexes": "JSON indexes for fast lookup by source, entity, tag, and link.",
    "instructions": "Templates and schemas for wiki notes, patterns, receipts, and decisions.",
}

_INSTRUCTION_FILES: dict[str, str] = {
    "wiki_note_template.md": """\
---
note_id: <stable 12-char hex id>
source_file: raw/<filename>
source_hash: sha256:<hex>
created_at: <ISO-8601>
updated_at: <ISO-8601>
content_type: <md|txt|json|yaml|csv>
tags: []
related_notes: []
confidence: inferred
status: auto-generated
---

# <Title>

## Summary
<!-- One paragraph: what this file contains and why it matters -->

## Key Facts
<!-- Bullet list of the most important structured facts -->

## Operational Relevance
<!-- How this connects to SentinalAI incident response -->

## Entities
<!-- Services, components, thresholds, metric names extracted from content -->

## Signals
<!-- Anomaly indicators, error patterns, metric names found in content -->

## Relationships
<!-- Links to other notes, patterns, or receipts -->

## Open Questions
<!-- Gaps, ambiguities, things to investigate -->

## Source Reference
File: raw/<filename>
SHA-256: <hex>
Ingested: <ISO-8601>
""",
    "pattern_template.yaml": """\
pattern_id: ""
name: ""
description: ""
incident_types: []
services: []
root_cause_template: ""
evidence_signals: []
resolution_steps: []
confidence: 0.0
observation_count: 0
first_seen: ""
last_seen: ""
related_notes: []
status: active
""",
    "receipt_schema.json": """\
{
  "receipt_id": "",
  "incident_id": "",
  "service": "",
  "incident_type": "",
  "timestamp": "",
  "root_cause": "",
  "evidence_keys": [],
  "confidence": 0.0,
  "resolution": "",
  "verified": false,
  "quality_score": 0.0,
  "related_patterns": [],
  "related_decisions": [],
  "notes": ""
}
""",
    "decision_schema.json": """\
{
  "decision_id": "",
  "title": "",
  "context": "",
  "decision": "",
  "rationale": "",
  "alternatives": [],
  "consequences": [],
  "status": "active",
  "made_at": "",
  "made_by": "",
  "reviewed_at": "",
  "related_notes": [],
  "related_patterns": []
}
""",
    "query_record_schema.json": """\
{
  "query_id": "",
  "incident_id": "",
  "service": "",
  "tool_name": "",
  "query_text": "",
  "result_summary": "",
  "evidence_keys": [],
  "timestamp": "",
  "quality_signal": 0.0,
  "reuse_count": 0
}
""",
    "topology_schema.yaml": """\
snapshot_id: ""
timestamp: ""
service: ""
dependencies: []
dependents: []
blast_radius_tier1: []
blast_radius_tier2: []
co_failure_partners: []
notes: ""
""",
    "ingestion_rules.md": """\
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
""",
    "update_rules.md": """\
# Update Rules

## When to update a wiki note
1. The source file's SHA-256 hash changes.
2. Manual edit: set `status: manual` in front matter to prevent auto-overwrite.

## What is preserved on update
- `note_id` — never changes for a given source file path.
- `created_at` — never changes.
- Manual sections marked with `<!-- manual -->` are preserved.

## What is replaced on update
- `source_hash`, `updated_at`, `tags`, `related_notes`.
- All auto-generated body sections.

## Index behavior
- All four indexes are updated atomically on each ingest run.
- Old entries are removed if a source file is deleted.
""",
}


def bootstrap(base_path: str = "sentinel_wiki") -> list[str]:
    """Create the sentinel_wiki folder structure.

    Args:
        base_path: Root directory to bootstrap (default: sentinel_wiki).

    Returns:
        List of paths created.
    """
    created: list[str] = []
    root = Path(base_path)

    for subdir, description in _DIRS.items():
        dirpath = root / subdir
        dirpath.mkdir(parents=True, exist_ok=True)
        readme = dirpath / "README.md"
        if not readme.exists():
            readme.write_text(f"# {subdir}/\n\n{description}\n")
            created.append(str(readme))
        created.append(str(dirpath))

    for filename, content in _INSTRUCTION_FILES.items():
        fpath = root / "instructions" / filename
        if not fpath.exists():
            fpath.write_text(content)
            created.append(str(fpath))

    return created
