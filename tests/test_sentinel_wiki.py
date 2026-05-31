"""Tests for the sentinel_wiki Phase 1 + Phase 2 implementation."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from sentinel_wiki.bootstrap import bootstrap
from sentinel_wiki.ingester import ingest
from sentinel_wiki.note_generator import file_hash, generate_note, note_id_for
from sentinel_wiki import indexer
from sentinel_wiki.searcher import search, status


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _raw(tmp: Path, name: str, content: str) -> Path:
    raw_dir = tmp / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    p = raw_dir / name
    p.write_text(content)
    return p


# ---------------------------------------------------------------------------
# 1. Bootstrap creates all directories
# ---------------------------------------------------------------------------

class TestBootstrap:
    def test_creates_all_subdirectories(self, tmp_path):
        bootstrap(str(tmp_path / "wiki_root"))
        root = tmp_path / "wiki_root"
        expected = ["raw", "wiki", "patterns", "receipts", "decisions",
                    "topology", "queries", "evals", "indexes", "instructions"]
        for d in expected:
            assert (root / d).is_dir(), f"Missing dir: {d}"

    def test_creates_readme_in_each_subdir(self, tmp_path):
        root = tmp_path / "wiki_root"
        bootstrap(str(root))
        for d in ["raw", "wiki", "patterns", "indexes"]:
            assert (root / d / "README.md").exists()

    def test_creates_instruction_files(self, tmp_path):
        root = tmp_path / "wiki_root"
        bootstrap(str(root))
        expected = [
            "wiki_note_template.md", "pattern_template.yaml",
            "receipt_schema.json", "decision_schema.json",
            "ingestion_rules.md", "update_rules.md",
        ]
        for f in expected:
            assert (root / "instructions" / f).exists(), f"Missing: {f}"

    def test_idempotent(self, tmp_path):
        root = tmp_path / "wiki_root"
        bootstrap(str(root))
        # Write sentinel content to a README
        readme = root / "raw" / "README.md"
        readme.write_text("sentinel")
        # Second bootstrap must not overwrite
        bootstrap(str(root))
        assert readme.read_text() == "sentinel"


# ---------------------------------------------------------------------------
# 2-4. Ingestion of txt / markdown / json
# ---------------------------------------------------------------------------

class TestIngestTxt:
    def test_creates_wiki_note(self, tmp_path):
        _raw(tmp_path, "alert.txt", "CPU usage exceeded 90% on payment-service")
        result = ingest(str(tmp_path))
        assert "raw/alert.txt" in result.ingested
        assert (tmp_path / "wiki" / "alert.md").exists()

    def test_note_has_front_matter(self, tmp_path):
        _raw(tmp_path, "alert.txt", "CPU usage exceeded 90%")
        ingest(str(tmp_path))
        note = (tmp_path / "wiki" / "alert.md").read_text()
        assert "note_id:" in note
        assert "source_file: raw/alert.txt" in note
        assert "source_hash: sha256:" in note


class TestIngestMarkdown:
    def test_creates_wiki_note(self, tmp_path):
        _raw(tmp_path, "runbook.md", "# Runbook\n\n## Steps\n\n1. Check logs")
        result = ingest(str(tmp_path))
        assert "raw/runbook.md" in result.ingested
        assert (tmp_path / "wiki" / "runbook.md").exists()

    def test_note_has_key_facts(self, tmp_path):
        _raw(tmp_path, "runbook.md", "# Runbook\n\n## Steps\n\n1. Check logs")
        ingest(str(tmp_path))
        note = (tmp_path / "wiki" / "runbook.md").read_text()
        assert "## Key Facts" in note

    def test_sections_extracted(self, tmp_path):
        _raw(tmp_path, "doc.md", "# Doc\n\n## Steps\n\n## Notes\n\nsome text")
        ingest(str(tmp_path))
        note = (tmp_path / "wiki" / "doc.md").read_text()
        assert "Sections:" in note or "Line count:" in note


class TestIngestJson:
    def test_creates_wiki_note(self, tmp_path):
        data = {"service": "api", "errors": [{"code": 500}], "threshold": 0.9}
        _raw(tmp_path, "config.json", json.dumps(data))
        result = ingest(str(tmp_path))
        assert "raw/config.json" in result.ingested
        assert (tmp_path / "wiki" / "config.md").exists()

    def test_top_level_keys_in_summary(self, tmp_path):
        data = {"service": "api", "errors": [], "threshold": 0.9}
        _raw(tmp_path, "config.json", json.dumps(data))
        ingest(str(tmp_path))
        note = (tmp_path / "wiki" / "config.md").read_text()
        assert "service" in note
        assert "threshold" in note

    def test_entities_extracted(self, tmp_path):
        data = {"service": "api", "region": "us-east-1"}
        _raw(tmp_path, "svc.json", json.dumps(data))
        ingest(str(tmp_path))
        note = (tmp_path / "wiki" / "svc.md").read_text()
        assert "service" in note or "region" in note


# ---------------------------------------------------------------------------
# 5. Hash-based idempotency
# ---------------------------------------------------------------------------

class TestIdempotency:
    def test_unchanged_file_skipped(self, tmp_path):
        _raw(tmp_path, "data.txt", "stable content")
        ingest(str(tmp_path))
        result2 = ingest(str(tmp_path))
        assert "raw/data.txt" in result2.skipped
        assert "raw/data.txt" not in result2.ingested

    def test_changed_file_re_ingested(self, tmp_path):
        p = _raw(tmp_path, "data.txt", "v1 content")
        ingest(str(tmp_path))
        p.write_text("v2 content — changed")
        result2 = ingest(str(tmp_path))
        assert "raw/data.txt" in result2.ingested

    def test_created_at_preserved_on_update(self, tmp_path):
        p = _raw(tmp_path, "data.txt", "v1")
        ingest(str(tmp_path))
        note_v1 = (tmp_path / "wiki" / "data.md").read_text()
        import re
        ca1 = re.search(r"created_at: (.+)", note_v1).group(1).strip()

        p.write_text("v2 changed")
        ingest(str(tmp_path))
        note_v2 = (tmp_path / "wiki" / "data.md").read_text()
        ca2 = re.search(r"created_at: (.+)", note_v2).group(1).strip()
        assert ca1 == ca2


# ---------------------------------------------------------------------------
# 6. Source index updated
# ---------------------------------------------------------------------------

class TestSourceIndex:
    def test_source_index_populated(self, tmp_path):
        _raw(tmp_path, "events.json", json.dumps({"type": "alert"}))
        ingest(str(tmp_path))
        idx = indexer.load_source_index(tmp_path / "indexes")
        assert any(e["source_file"] == "raw/events.json" for e in idx)

    def test_source_index_has_hash(self, tmp_path):
        _raw(tmp_path, "events.json", json.dumps({"type": "alert"}))
        ingest(str(tmp_path))
        idx = indexer.load_source_index(tmp_path / "indexes")
        entry = next(e for e in idx if e["source_file"] == "raw/events.json")
        assert entry["source_hash"].startswith("sha256:")

    def test_source_index_updated_on_change(self, tmp_path):
        p = _raw(tmp_path, "events.json", json.dumps({"type": "alert"}))
        ingest(str(tmp_path))
        h1 = indexer.get_source_entry(
            indexer.load_source_index(tmp_path / "indexes"), "raw/events.json"
        )["source_hash"]

        p.write_text(json.dumps({"type": "critical"}))
        ingest(str(tmp_path))
        h2 = indexer.get_source_entry(
            indexer.load_source_index(tmp_path / "indexes"), "raw/events.json"
        )["source_hash"]

        assert h1 != h2


# ---------------------------------------------------------------------------
# 7. Tag and entity extraction
# ---------------------------------------------------------------------------

class TestTagExtraction:
    def test_json_extension_becomes_tag(self, tmp_path):
        _raw(tmp_path, "data.json", json.dumps({"x": 1}))
        ingest(str(tmp_path))
        tag_idx = indexer.load_tag_index(tmp_path / "indexes")
        assert "json" in tag_idx

    def test_explicit_tags_in_json_extracted(self, tmp_path):
        _raw(tmp_path, "svc.json", json.dumps({"tags": ["sre", "incident"], "x": 1}))
        ingest(str(tmp_path))
        tag_idx = indexer.load_tag_index(tmp_path / "indexes")
        assert "sre" in tag_idx or "incident" in tag_idx


class TestEntityExtraction:
    def test_json_top_level_keys_as_entities(self, tmp_path):
        _raw(tmp_path, "cfg.json", json.dumps({"service": "api", "region": "us-east"}))
        ingest(str(tmp_path))
        ent_idx = indexer.load_entity_index(tmp_path / "indexes")
        assert "service" in ent_idx or "region" in ent_idx

    def test_csv_columns_as_entities(self, tmp_path):
        _raw(tmp_path, "metrics.csv", "timestamp,service,error_rate\n2026-01-01,api,0.05")
        ingest(str(tmp_path))
        ent_idx = indexer.load_entity_index(tmp_path / "indexes")
        assert any(e in ent_idx for e in ["timestamp", "service", "error_rate"])


# ---------------------------------------------------------------------------
# 8. Search
# ---------------------------------------------------------------------------

class TestSearch:
    def test_search_by_filename(self, tmp_path):
        _raw(tmp_path, "payment_alerts.txt", "Payment service is down")
        ingest(str(tmp_path))
        hits = search("payment", base_path=str(tmp_path))
        assert any("payment" in h.note_path.lower() for h in hits)

    def test_search_by_text(self, tmp_path):
        _raw(tmp_path, "runbook.txt", "restart the payment-service pod")
        ingest(str(tmp_path))
        hits = search("restart", base_path=str(tmp_path))
        assert len(hits) > 0
        assert any(h.match_type == "text" for h in hits)

    def test_search_by_tag(self, tmp_path):
        _raw(tmp_path, "data.json", json.dumps({"tags": ["sre"], "value": 1}))
        ingest(str(tmp_path))
        hits = search("sre", base_path=str(tmp_path))
        assert any(h.match_type == "tag" for h in hits)

    def test_search_by_entity(self, tmp_path):
        _raw(tmp_path, "cfg.json", json.dumps({"service": "api-gateway"}))
        ingest(str(tmp_path))
        hits = search("service", base_path=str(tmp_path))
        assert any(h.match_type in ("entity", "text") for h in hits)

    def test_empty_query_returns_empty(self, tmp_path):
        _raw(tmp_path, "x.txt", "hello")
        ingest(str(tmp_path))
        hits = search("", base_path=str(tmp_path))
        assert hits == []

    def test_no_results_for_unknown_query(self, tmp_path):
        _raw(tmp_path, "x.txt", "hello world")
        ingest(str(tmp_path))
        hits = search("zzznomatch999", base_path=str(tmp_path))
        assert hits == []


# ---------------------------------------------------------------------------
# 9. Status
# ---------------------------------------------------------------------------

class TestStatus:
    def test_status_returns_counts(self, tmp_path):
        _raw(tmp_path, "a.txt", "alpha")
        _raw(tmp_path, "b.json", json.dumps({"x": 1}))
        ingest(str(tmp_path))
        s = status(str(tmp_path))
        assert s["raw_files"] == 2
        assert s["wiki_notes"] == 2
        assert s["source_index_entries"] == 2

    def test_status_empty_wiki(self, tmp_path):
        bootstrap(str(tmp_path))
        s = status(str(tmp_path))
        assert s["raw_files"] == 0
        assert s["wiki_notes"] == 0


# ---------------------------------------------------------------------------
# 10. note_id stability
# ---------------------------------------------------------------------------

class TestNoteId:
    def test_same_path_same_id(self):
        assert note_id_for("raw/foo.json") == note_id_for("raw/foo.json")

    def test_different_paths_different_ids(self):
        assert note_id_for("raw/foo.json") != note_id_for("raw/bar.json")

    def test_id_is_12_chars(self):
        assert len(note_id_for("raw/foo.json")) == 12


# ===========================================================================
# Phase 2: Receipts, Pattern Promotion, Wiki Context
# ===========================================================================

from sentinel_wiki.receipt_writer import write_receipt, root_cause_hash
from sentinel_wiki.pattern_promoter import promote, PROMOTE_THRESHOLD
from sentinel_wiki.wiki_context import get_context


def _make_result(
    root_cause: str = "memory leak in payment-service",
    confidence: int = 82,
    quality: float = 0.74,
    incident_type: str = "error_spike",
    service: str = "payment-service",
) -> dict:
    return {
        "root_cause": root_cause,
        "confidence": confidence,
        "online_quality_score": quality,
        "incident_type": incident_type,
        "service": service,
        "evidence_keys": ["check_golden_signals", "search_error_logs"],
        "reasoning": "Error rate spiked after v2.3.1 deployment.",
    }


# ---------------------------------------------------------------------------
# receipt_writer
# ---------------------------------------------------------------------------

class TestReceiptWriter:
    def test_write_receipt_creates_file(self, tmp_path):
        path = write_receipt("INC001", "api", "error_spike", _make_result(), base_path=str(tmp_path))
        assert path is not None
        assert (tmp_path / "receipts").exists()
        # A receipt file was created
        receipts = list((tmp_path / "receipts").glob("INC001*.md"))
        assert len(receipts) == 1

    def test_receipt_has_front_matter(self, tmp_path):
        write_receipt("INC002", "api", "timeout", _make_result(), base_path=str(tmp_path))
        content = list((tmp_path / "receipts").glob("INC002*.md"))[0].read_text()
        assert "incident_id: INC002" in content
        assert "root_cause_hash:" in content
        assert "quality_score:" in content

    def test_receipt_has_root_cause_section(self, tmp_path):
        result = _make_result(root_cause="OOM kill on worker pod")
        write_receipt("INC003", "worker", "oomkill", result, base_path=str(tmp_path))
        content = list((tmp_path / "receipts").glob("INC003*.md"))[0].read_text()
        assert "OOM kill on worker pod" in content

    def test_root_cause_hash_stable(self):
        h1 = root_cause_hash("Memory leak in payment-service")
        h2 = root_cause_hash("memory leak in payment-service")
        assert h1 == h2  # case-insensitive

    def test_root_cause_hash_different_causes(self):
        assert root_cause_hash("memory leak") != root_cause_hash("cpu spike")

    def test_write_receipt_never_raises_on_bad_input(self, tmp_path):
        # Should return None gracefully, not raise
        result = write_receipt("", "", "", {}, base_path="/nonexistent/path/xyz")
        # Either None or a path — should not raise
        assert result is None or isinstance(result, str)

    def test_receipt_tags_include_incident_type(self, tmp_path):
        write_receipt("INC004", "svc", "latency", _make_result(), base_path=str(tmp_path))
        content = list((tmp_path / "receipts").glob("INC004*.md"))[0].read_text()
        assert "latency" in content

    def test_high_quality_tag_applied(self, tmp_path):
        result = _make_result(quality=0.85)
        write_receipt("INC005", "svc", "error_spike", result, base_path=str(tmp_path))
        content = list((tmp_path / "receipts").glob("INC005*.md"))[0].read_text()
        assert "high-quality" in content


# ---------------------------------------------------------------------------
# pattern_promoter
# ---------------------------------------------------------------------------

class TestPatternPromoter:
    def _write_n_receipts(self, tmp_path, n: int, root_cause: str = "deployment regression") -> None:
        for i in range(n):
            write_receipt(
                f"INC{i:04d}", "payment-service", "error_spike",
                _make_result(root_cause=root_cause),
                base_path=str(tmp_path),
            )

    def test_no_pattern_below_threshold(self, tmp_path):
        self._write_n_receipts(tmp_path, PROMOTE_THRESHOLD - 1)
        promoted = promote(str(tmp_path))
        assert len(promoted) == 0

    def test_pattern_created_at_threshold(self, tmp_path):
        self._write_n_receipts(tmp_path, PROMOTE_THRESHOLD)
        promoted = promote(str(tmp_path))
        assert len(promoted) == 1

    def test_pattern_file_exists_after_promotion(self, tmp_path):
        self._write_n_receipts(tmp_path, PROMOTE_THRESHOLD)
        promote(str(tmp_path))
        patterns = list((tmp_path / "patterns").glob("*.yaml"))
        assert len(patterns) == 1

    def test_pattern_contains_service(self, tmp_path):
        self._write_n_receipts(tmp_path, PROMOTE_THRESHOLD)
        promote(str(tmp_path))
        import yaml
        pattern_file = list((tmp_path / "patterns").glob("*.yaml"))[0]
        data = yaml.safe_load(pattern_file.read_text())
        assert "payment-service" in data.get("services", [])

    def test_pattern_observation_count(self, tmp_path):
        n = PROMOTE_THRESHOLD + 2
        self._write_n_receipts(tmp_path, n)
        promote(str(tmp_path))
        import yaml
        pattern_file = list((tmp_path / "patterns").glob("*.yaml"))[0]
        data = yaml.safe_load(pattern_file.read_text())
        assert data["observation_count"] == n

    def test_promote_idempotent(self, tmp_path):
        self._write_n_receipts(tmp_path, PROMOTE_THRESHOLD)
        promote(str(tmp_path))
        promote(str(tmp_path))  # second call
        patterns = list((tmp_path / "patterns").glob("*.yaml"))
        assert len(patterns) == 1

    def test_different_root_causes_separate_patterns(self, tmp_path):
        # Use non-overlapping incident ID ranges to avoid filename collisions
        for i in range(PROMOTE_THRESHOLD):
            write_receipt(
                f"INC_A{i:04d}", "svc", "error_spike",
                _make_result(root_cause="memory leak"),
                base_path=str(tmp_path),
            )
        for i in range(PROMOTE_THRESHOLD):
            write_receipt(
                f"INC_B{i:04d}", "svc", "error_spike",
                _make_result(root_cause="disk full on /var"),
                base_path=str(tmp_path),
            )
        promote(str(tmp_path))
        patterns = list((tmp_path / "patterns").glob("*.yaml"))
        assert len(patterns) == 2

    def test_promote_never_raises_on_empty(self, tmp_path):
        result = promote(str(tmp_path))
        assert result == []


# ---------------------------------------------------------------------------
# wiki_context
# ---------------------------------------------------------------------------

class TestWikiContext:
    def _setup(self, tmp_path, n_receipts: int = 3) -> None:
        for i in range(n_receipts):
            write_receipt(
                f"INC{i:04d}", "api-gateway", "timeout",
                _make_result(service="api-gateway", incident_type="timeout"),
                base_path=str(tmp_path),
            )
        promote(str(tmp_path))

    def test_get_context_returns_dict(self, tmp_path):
        self._setup(tmp_path)
        ctx = get_context("api-gateway", "timeout", base_path=str(tmp_path))
        assert isinstance(ctx, dict)
        assert "recent_receipts" in ctx
        assert "matching_patterns" in ctx
        assert "context_summary" in ctx

    def test_recent_receipts_for_service(self, tmp_path):
        self._setup(tmp_path, n_receipts=4)
        ctx = get_context("api-gateway", "timeout", base_path=str(tmp_path))
        assert ctx["receipt_count"] > 0
        assert all(
            r["service"] == "api-gateway" or r["incident_type"] == "timeout"
            for r in ctx["recent_receipts"]
        )

    def test_matching_patterns_returned(self, tmp_path):
        self._setup(tmp_path, n_receipts=PROMOTE_THRESHOLD)
        ctx = get_context("api-gateway", "timeout", base_path=str(tmp_path))
        assert ctx["pattern_count"] > 0

    def test_context_summary_not_empty_when_receipts_exist(self, tmp_path):
        self._setup(tmp_path, n_receipts=2)
        ctx = get_context("api-gateway", "timeout", base_path=str(tmp_path))
        assert ctx["context_summary"] != ""

    def test_unknown_service_returns_empty(self, tmp_path):
        ctx = get_context("unknown-service-xyz", "unknown-type", base_path=str(tmp_path))
        assert ctx["receipt_count"] == 0
        assert ctx["pattern_count"] == 0

    def test_get_context_never_raises(self, tmp_path):
        # Even with missing directories, should not raise
        ctx = get_context("svc", "type", base_path=str(tmp_path / "nonexistent"))
        assert isinstance(ctx, dict)
