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
