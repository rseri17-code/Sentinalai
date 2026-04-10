"""Evidence Citation Engine — grounds every RCA claim in a source.

Karpathy principle: "No hallucinations. Every claim must be backed by data."

This module post-processes the RCA result and annotates each claim in
`reasoning` and `root_cause` with a citation pointer to the actual
evidence entry (log line, metric reading, change record) that supports it.

Output format (added to result dict):
    result["citations"] = [
        {
            "claim":   "Connection pool exhausted (observed 1024/1024 connections)",
            "source":  "splunk",
            "evidence": "2024-01-15T14:02:11 ERROR pool.exhausted connections=1024/1024",
            "timestamp": "2024-01-15T14:02:11",
            "confidence": 0.92,
        },
        ...
    ]

    result["cited_root_cause"] = "Connection pool exhaustion [splunk:1]"
    result["citation_coverage"] = 0.85  # fraction of claims that have citations

Usage:
    from supervisor.evidence_citation import annotate_citations
    result = annotate_citations(result, evidence)
"""
from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger("sentinalai.evidence_citation")

# Minimum keyword overlap to count a citation as matched
_MIN_OVERLAP = 2


def annotate_citations(result: dict[str, Any], evidence: dict[str, Any]) -> dict[str, Any]:
    """Annotate the RCA result with evidence citations.

    Mutates *result* in-place (adds 'citations', 'cited_root_cause',
    'citation_coverage') and returns it.

    Never raises — if citation fails the result is returned unmodified.
    """
    try:
        citations = _build_citations(result, evidence)
        result["citations"] = citations
        result["citation_coverage"] = _coverage(result, citations)
        result["cited_root_cause"] = _cite_root_cause(
            result.get("root_cause", ""), citations
        )
    except Exception as exc:
        logger.warning("Citation annotation failed (non-critical): %s", exc)
    return result


# ---------------------------------------------------------------------------
# Internal
# ---------------------------------------------------------------------------

def _build_citations(result: dict, evidence: dict) -> list[dict]:
    """Match RCA claims against the evidence corpus."""
    claims = _extract_claims(result)
    corpus = _build_evidence_corpus(evidence)
    citations: list[dict] = []

    for claim in claims:
        best = _best_match(claim, corpus)
        if best:
            citations.append({
                "claim": claim,
                "source": best["source"],
                "evidence": best["text"][:300],
                "timestamp": best.get("timestamp", ""),
                "confidence": best["score"],
                "citation_id": f"{best['source']}:{len(citations) + 1}",
            })

    return citations


def _extract_claims(result: dict) -> list[str]:
    """Split reasoning + root_cause into individual claim sentences."""
    text = " ".join(filter(None, [
        result.get("root_cause", ""),
        result.get("reasoning", ""),
    ]))
    # Split on sentence boundaries
    sentences = re.split(r"[.!?]\s+", text)
    # Filter to non-trivial sentences (> 10 chars)
    return [s.strip() for s in sentences if len(s.strip()) > 10]


def _build_evidence_corpus(evidence: dict) -> list[dict]:
    """Flatten all evidence into a list of searchable text entries."""
    corpus: list[dict] = []

    # Splunk logs
    for key in ("logs", "log_data", "change_data"):
        log_block = evidence.get(key, {})
        logs = (log_block.get("logs") or log_block.get("results") or []) if isinstance(log_block, dict) else []
        for entry in logs[:50]:
            text = entry.get("_raw") or entry.get("message") or str(entry)
            ts = entry.get("_time") or entry.get("timestamp") or ""
            corpus.append({"source": "splunk", "text": text, "timestamp": ts})

    # Metrics / signals
    metrics_block = evidence.get("metrics", evidence.get("metric_data", {}))
    if isinstance(metrics_block, dict):
        signals = metrics_block.get("signals", {})
        for signal_name, value in signals.items():
            corpus.append({
                "source": "sysdig",
                "text": f"{signal_name}={value}",
                "timestamp": "",
            })
        events = metrics_block.get("events", [])
        for ev in events[:20]:
            corpus.append({
                "source": "sysdig",
                "text": str(ev.get("name") or ev.get("message") or ev),
                "timestamp": ev.get("timestamp", ""),
            })

    # ITSM change records
    itsm_block = evidence.get("itsm_context", {})
    if isinstance(itsm_block, dict):
        for change in itsm_block.get("change_records", [])[:10]:
            corpus.append({
                "source": "servicenow",
                "text": f"Change {change.get('number','')} {change.get('short_description','')} by {change.get('requested_by','')}",
                "timestamp": change.get("end_date", change.get("start_date", "")),
            })

    # CMDB blast radius
    cmdb = evidence.get("cmdb_blast_radius", {})
    if isinstance(cmdb, dict):
        for ci_name, changes in cmdb.get("blast_radius", {}).items():
            for ch in changes:
                corpus.append({
                    "source": "cmdb",
                    "text": f"{ci_name}: {ch.get('short_description','')} (risk={ch.get('risk','')})",
                    "timestamp": ch.get("end_date", ""),
                })

    # APM errors
    apm = evidence.get("apm_data", evidence.get("apm", {}))
    if isinstance(apm, dict):
        for err in apm.get("errors", apm.get("error_samples", []))[:10]:
            corpus.append({
                "source": "dynatrace",
                "text": err.get("message") or err.get("exception") or str(err),
                "timestamp": err.get("timestamp", ""),
            })

    # Diff analysis
    diff = evidence.get("diff_analysis", {})
    if isinstance(diff, dict) and diff.get("culprit_file"):
        corpus.append({
            "source": "github",
            "text": (
                f"Code change in {diff['culprit_file']}:{diff.get('culprit_line','')} — "
                f"{diff.get('culprit_snippet','')[:100]}"
            ),
            "timestamp": "",
        })

    return corpus


def _best_match(claim: str, corpus: list[dict]) -> dict | None:
    """Find the corpus entry with the highest keyword overlap with the claim."""
    claim_words = set(_tokenize(claim))
    if not claim_words:
        return None

    best_score = 0.0
    best_entry: dict | None = None

    for entry in corpus:
        entry_words = set(_tokenize(entry["text"]))
        if not entry_words:
            continue
        overlap = len(claim_words & entry_words)
        if overlap < _MIN_OVERLAP:
            continue
        # Jaccard similarity
        score = overlap / len(claim_words | entry_words)
        if score > best_score:
            best_score = score
            best_entry = {**entry, "score": round(score, 3)}

    return best_entry


def _coverage(result: dict, citations: list[dict]) -> float:
    """Fraction of RCA claims that have at least one citation."""
    claims = _extract_claims(result)
    if not claims:
        return 0.0
    cited_claims = {c["claim"] for c in citations}
    matched = sum(1 for claim in claims if claim in cited_claims)
    return round(matched / len(claims), 3)


def _cite_root_cause(root_cause: str, citations: list[dict]) -> str:
    """Append citation IDs to the root cause string."""
    if not citations:
        return root_cause
    root_words = set(_tokenize(root_cause))
    refs: list[str] = []
    for citation in citations:
        overlap = len(root_words & set(_tokenize(citation["claim"])))
        if overlap >= _MIN_OVERLAP:
            refs.append(f"[{citation['citation_id']}]")
    if refs:
        return f"{root_cause} {' '.join(refs)}"
    return root_cause


def _tokenize(text: str) -> list[str]:
    """Simple whitespace+punctuation tokenizer, lowercase, min 3 chars."""
    words = re.findall(r"[a-z0-9_\-]{3,}", text.lower())
    # Remove common stop words that add noise
    _STOP = {"the", "and", "for", "was", "are", "has", "that", "this",
              "with", "from", "have", "been", "not", "but"}
    return [w for w in words if w not in _STOP]
