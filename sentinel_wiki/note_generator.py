"""Generate Markdown wiki notes from raw source files.

Supports: .md, .txt, .json, .yaml, .yml, .csv
No LLM calls — all summaries are structurally derived from content.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import yaml as _yaml
    _YAML_OK = True
except ImportError:
    _YAML_OK = False


def note_id_for(source_rel_path: str) -> str:
    """Derive a stable 12-char hex note ID from the source file path."""
    return hashlib.sha256(source_rel_path.encode()).hexdigest()[:12]


def file_hash(path: Path) -> str:
    """Return SHA-256 hex digest of file content."""
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def generate_note(
    source_path: Path,
    source_rel: str,
    existing_note: str | None = None,
    created_at: str = "",
) -> str:
    """Generate a Markdown wiki note for a raw source file.

    Args:
        source_path:   Absolute path to the raw file.
        source_rel:    Relative path from sentinel_wiki root (e.g. raw/foo.json).
        existing_note: Content of the existing note if it exists (for created_at).
        created_at:    ISO-8601 creation timestamp (defaults to now).

    Returns:
        Full Markdown string for the wiki note.
    """
    now = datetime.now(timezone.utc).isoformat()
    if not created_at:
        # Try to extract from existing note
        if existing_note:
            m = re.search(r"created_at:\s*(.+)", existing_note)
            created_at = m.group(1).strip() if m else now
        else:
            created_at = now

    suffix = source_path.suffix.lower().lstrip(".")
    nid = note_id_for(source_rel)
    sha = file_hash(source_path)
    title = source_path.stem.replace("_", " ").replace("-", " ").title()

    parsed = _parse(source_path, suffix)
    tags = _derive_tags(suffix, parsed)
    entities = _derive_entities(suffix, parsed)
    signals = _derive_signals(suffix, parsed)
    summary = _derive_summary(title, suffix, parsed)
    key_facts = _derive_key_facts(suffix, parsed, source_path)

    front_matter = (
        f"---\n"
        f"note_id: {nid}\n"
        f"source_file: {source_rel}\n"
        f"source_hash: sha256:{sha}\n"
        f"created_at: {created_at}\n"
        f"updated_at: {now}\n"
        f"content_type: {suffix or 'unknown'}\n"
        f"tags: {json.dumps(tags)}\n"
        f"related_notes: []\n"
        f"confidence: inferred\n"
        f"status: auto-generated\n"
        f"---\n"
    )

    body = f"""# {title}

## Summary
{summary}

## Key Facts
{_bullet(key_facts)}

## Operational Relevance
<!-- How this connects to SentinalAI incident response — annotate manually -->

## Entities
{_bullet(entities) if entities else "_None extracted_"}

## Signals
{_bullet(signals) if signals else "_None extracted_"}

## Relationships
<!-- Links to other notes, patterns, or receipts — annotate manually -->

## Open Questions
<!-- Gaps, ambiguities, things to investigate — annotate manually -->

## Source Reference
File: {source_rel}
SHA-256: {sha}
Ingested: {now}
"""
    return front_matter + "\n" + body


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------

def _parse(path: Path, suffix: str) -> dict[str, Any]:
    """Parse file into a normalised dict for downstream extraction."""
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return {"text": "", "error": "unreadable"}

    if suffix == "json":
        try:
            return {"data": json.loads(text), "text": text}
        except json.JSONDecodeError:
            return {"text": text}

    if suffix in ("yaml", "yml"):
        if _YAML_OK:
            try:
                data = _yaml.safe_load(text)
                return {"data": data, "text": text}
            except Exception:
                pass
        return {"text": text}

    if suffix == "csv":
        rows = list(csv.reader(io.StringIO(text)))
        columns = rows[0] if rows else []
        return {"columns": columns, "row_count": len(rows) - 1, "text": text}

    # md, txt, fallback
    return {"text": text}


# ---------------------------------------------------------------------------
# Derivation helpers
# ---------------------------------------------------------------------------

def _derive_tags(suffix: str, parsed: dict) -> list[str]:
    tags = [suffix] if suffix else []
    data = parsed.get("data")
    if isinstance(data, dict):
        for key in ("tags", "tag", "labels", "categories"):
            val = data.get(key)
            if isinstance(val, list):
                tags.extend(str(t) for t in val if t)
            elif isinstance(val, str) and val:
                tags.append(val)
    text = parsed.get("text", "")
    # Markdown #tags on their own line
    for m in re.finditer(r"(?:^|\s)#([a-zA-Z][a-zA-Z0-9_-]+)", text):
        tags.append(m.group(1))
    return sorted(set(t.lower() for t in tags if t))[:20]


def _derive_entities(suffix: str, parsed: dict) -> list[str]:
    entities: set[str] = set()
    data = parsed.get("data")

    if isinstance(data, dict):
        entities.update(k for k in data.keys() if isinstance(k, str))
    elif isinstance(data, list) and data and isinstance(data[0], dict):
        entities.update(k for k in data[0].keys() if isinstance(k, str))

    cols = parsed.get("columns", [])
    entities.update(cols)

    text = parsed.get("text", "")
    # [[wikilinks]]
    for m in re.finditer(r"\[\[([^\]]+)\]\]", text):
        entities.add(m.group(1))
    # Level-2 markdown headers
    for m in re.finditer(r"^## (.+)", text, re.MULTILINE):
        entities.add(m.group(1).strip())

    return sorted(e for e in entities if e and len(e) < 60)[:30]


def _derive_signals(suffix: str, parsed: dict) -> list[str]:
    signals: list[str] = []
    text = parsed.get("text", "")
    _signal_patterns = [
        (r"\b(error|exception|timeout|latency|spike|crash|oom|saturation|failure)\b", "anomaly: {}"),
        (r"\b(\d+(?:\.\d+)?)\s*%", "percentage: {}%"),
        (r"\b(\d+(?:\.\d+)?)\s*(ms|rps|rpm|qps|req/s)", "metric: {} {}"),
    ]
    seen: set[str] = set()
    for pattern, fmt in _signal_patterns:
        for m in re.finditer(pattern, text, re.IGNORECASE):
            sig = m.group(1).lower()
            if sig not in seen:
                seen.add(sig)
                signals.append(sig)
    return signals[:20]


def _derive_summary(title: str, suffix: str, parsed: dict) -> str:
    data = parsed.get("data")

    if suffix == "csv":
        cols = parsed.get("columns", [])
        rows = parsed.get("row_count", 0)
        col_str = ", ".join(cols[:8]) + ("..." if len(cols) > 8 else "")
        return f"CSV document with {len(cols)} column(s) and {rows} row(s). Columns: {col_str}."

    if suffix == "json" and isinstance(data, dict):
        keys = list(data.keys())[:8]
        extra = "..." if len(data) > 8 else ""
        return f"JSON document with {len(data)} top-level key(s): {', '.join(keys)}{extra}."

    if suffix == "json" and isinstance(data, list):
        sample = data[0] if data else {}
        keys = list(sample.keys())[:6] if isinstance(sample, dict) else []
        return f"JSON array with {len(data)} record(s). Sample keys: {', '.join(keys)}." if keys else f"JSON array with {len(data)} record(s)."

    if suffix in ("yaml", "yml") and isinstance(data, dict):
        keys = list(data.keys())[:8]
        return f"YAML document with {len(data)} top-level key(s): {', '.join(keys)}."

    text = parsed.get("text", "")
    # First non-empty non-header paragraph
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and not line.startswith("---"):
            return line[:300]

    return f"{suffix.upper() or 'Unknown'} file: {title}."


def _derive_key_facts(suffix: str, parsed: dict, path: Path) -> list[str]:
    facts: list[str] = []
    size = path.stat().st_size
    facts.append(f"File size: {size:,} bytes")

    data = parsed.get("data")
    if suffix == "csv":
        cols = parsed.get("columns", [])
        facts.append(f"Columns ({len(cols)}): {', '.join(cols[:10])}")
        facts.append(f"Row count: {parsed.get('row_count', 0)}")

    elif isinstance(data, dict):
        facts.append(f"Top-level keys ({len(data)}): {', '.join(list(data.keys())[:10])}")
        # Look for common count fields
        for k, v in data.items():
            if isinstance(v, (list, dict)):
                facts.append(f"`{k}` has {len(v)} entries")
                if len(facts) > 8:
                    break

    elif isinstance(data, list):
        facts.append(f"Array length: {len(data)}")
        if data and isinstance(data[0], dict):
            facts.append(f"Record keys: {', '.join(list(data[0].keys())[:8])}")

    elif suffix in ("md", "txt"):
        text = parsed.get("text", "")
        lines = [l for l in text.splitlines() if l.strip()]
        facts.append(f"Line count: {len(lines)}")
        headers = [l.lstrip("#").strip() for l in text.splitlines() if l.startswith("#")]
        if headers:
            facts.append(f"Sections: {', '.join(headers[:8])}")

    return facts


def _bullet(items: list[str]) -> str:
    if not items:
        return "_None_"
    return "\n".join(f"- {item}" for item in items)
